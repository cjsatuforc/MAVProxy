#!/usr/bin/env python
'''
mavproxy - a MAVLink proxy program

Copyright Andrew Tridgell 2011
Released under the GNU GPL version 3 or later

'''

import sys, os, struct, math, time, socket
import fnmatch, errno, threading
import serial, Queue, select

# find the mavlink.py module
for d in [ 'pymavlink',
           os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'pymavlink') ]:
    if os.path.exists(d):
        sys.path.insert(0, d)
        if os.name == 'nt':
            try:
                # broken python compilation of mavlink.py on windows!
                os.unlink(os.path.join(d, 'mavlink.pyc'))
            except:
                pass

import select


class MPSettings(object):
    def __init__(self):
        self.vars = [ ('link', int),
                      ('altreadout', int),
                      ('distreadout', int),
                      ('battreadout', int),
                      ('basealtitude', int),
                      ('heartbeat', int),
                      ('numcells', int),
                      ('speech', int),
                      ('mavfwd', int),
                      ('streamrate', int),
                      ('streamrate2', int),
                      ('heartbeatreport', int),
                      ('radiosetup', int),
                      ('rc1mul', int),
                      ('rc2mul', int),
                      ('rc4mul', int)]
        self.link = 1
        self.altreadout = 10
        self.distreadout = 200
        self.battreadout = 1
        self.basealtitude = -1
        self.heartbeat = 1
        self.numcells = 0
        self.mavfwd = 1
        self.speech = 0
        self.streamrate = 4
        self.streamrate2 = 4
        self.radiosetup = 0
        self.heartbeatreport = 1
        self.rc1mul = 1
        self.rc2mul = 1
        self.rc4mul = 1

    def set(self, vname, value):
        '''set a setting'''
        for (v,t) in sorted(self.vars):
            if v == vname:
                try:
                    value = t(value)
                except:
                    print("Unable to convert %s to type %s" % (value, t))
                    return
                setattr(self, vname, value)
                return

    def show(self, v):
        '''show settings'''
        print("%20s %s" % (v, getattr(self, v)))

    def show_all(self):
        '''show all settings'''
        for (v,t) in sorted(self.vars):
            self.show(v)

class MPStatus(object):
    '''hold status information about the mavproxy'''
    def __init__(self):
        if opts.quadcopter:
            self.rc_throttle = [ 0.0, 0.0, 0.0, 0.0 ]
        else:
            self.rc_aileron  = 0
            self.rc_elevator = 0
            self.rc_throttle = 0
            self.rc_rudder   = 0
        self.gps	 = None
        self.msgs = {}
        self.msg_count = {}
        self.counters = {'MasterIn' : [], 'MasterOut' : 0, 'FGearIn' : 0, 'FGearOut' : 0, 'Slave' : 0}
        self.setup_mode = opts.setup
        self.wp_op = None
        self.wp_save_filename = None
        self.wploader = mavwp.MAVWPLoader()
        self.loading_waypoints = False
        self.loading_waypoint_lasttime = time.time()
        self.mav_error = 0
        self.target_system = -1
        self.target_component = -1
        self.speech = None
        self.last_altitude_announce = 0.0
        self.last_distance_announce = 0.0
        self.last_battery_announce = 0
        self.last_avionics_battery_announce = 0
        self.battery_level = -1
        self.avionics_battery_level = -1
        self.last_waypoint = 0
        self.exit = False
        self.override = [ 0 ] * 8
        self.flightmode = 'MAV'
        self.logdir = None
        self.last_heartbeat = 0
        self.heartbeat_error = False
        self.last_apm_msg = None
        self.highest_usec = 0

    def show(self, f, pattern=None):
        '''write status to status.txt'''
        if pattern is None:
            f.write('Counters: ')
            for c in self.counters:
                f.write('%s:%s ' % (c, self.counters[c]))
            f.write('\n')
            f.write('MAV Errors: %u\n' % self.mav_error)
            f.write(str(self.gps)+'\n')
        for m in sorted(self.msgs.keys()):
            if pattern is not None and not fnmatch.fnmatch(str(m).upper(), pattern.upper()):
                continue
            f.write("%u: %s\n" % (self.msg_count[m], str(self.msgs[m])))

    def write(self):
        '''write status to status.txt'''
        f = open('status.txt', mode='w')
        self.show(f)
        f.close()


class MPState(object):
    '''holds state of mavproxy'''
    def __init__(self):
        self.settings = MPSettings()
        self.status = MPStatus()

        # master mavlink device
        self.mav_master = None

        # mavlink outputs
        self.mav_outputs = []

        # SITL output
        self.sitl_output = None

        self.mav_param = {}

    def master(self):
        '''return the currently chosen mavlink master object'''
        if self.settings.link > len(self.mav_master):
            self.settings.link = 1

        # try to use one with no link error
        if not self.mav_master[self.settings.link-1].linkerror:
            return self.mav_master[self.settings.link-1]
        for m in self.mav_master:
            if not m.linkerror:
                return m
        return self.mav_master[self.settings.link-1]


def get_usec():
    '''time since 1970 in microseconds'''
    return int(time.time() * 1.0e6)

class rline(object):
    '''async readline abstraction'''
    def __init__(self, prompt):
        import threading
        self.prompt = prompt
        self.line = None
        try:
            import readline
        except Exception:
            pass

    def set_prompt(self, prompt):
        if prompt != self.prompt:
            self.prompt = prompt
            sys.stdout.write(prompt)
            
def say(text, priority='important'):
    '''speak some text'''
    ''' http://cvs.freebsoft.org/doc/speechd/ssip.html see 4.3.1 for priorities'''
    print(text)
    if mpstate.settings.speech:
        import speechd
        mpstate.status.speech = speechd.SSIPClient('MAVProxy%u' % os.getpid())
        mpstate.status.speech.set_output_module('festival')
        mpstate.status.speech.set_language('en')
        mpstate.status.speech.set_priority(priority)
        mpstate.status.speech.set_punctuation(speechd.PunctuationMode.SOME)
        mpstate.status.speech.speak(text)
        mpstate.status.speech.close()

def get_mav_param(param, default=None):
    '''return a EEPROM parameter value'''
    if not param in mpstate.mav_param:
        return default
    return mpstate.mav_param[param]


def send_rc_override():
    '''send RC override packet'''
    if mpstate.sitl_output:
        buf = struct.pack('<HHHHHHHH',
                          *mpstate.status.override)
        mpstate.sitl_output.write(buf)
    else:
        mpstate.master().mav.rc_channels_override_send(mpstate.status.target_system,
                                                         mpstate.status.target_component,
                                                         *mpstate.status.override)

def cmd_switch(args, rl):
    '''handle RC switch changes'''
    mapping = [ 0, 1165, 1295, 1425, 1555, 1685, 1815 ]
    if len(args) != 1:
        print("Usage: switch <pwmvalue>")
        return
    value = int(args[0])
    if value < 0 or value > 6:
        print("Invalid switch value. Use 1-6 for flight modes, '0' to disable")
        return
    if opts.quadcopter:
        default_channel = 5
    else:
        default_channel = 8
    flite_mode_ch_parm = int(get_mav_param("FLTMODE_CH", default_channel))
    mpstate.status.override[flite_mode_ch_parm-1] = mapping[value]
    send_rc_override()
    if value == 0:
        print("Disabled RC switch override")
    else:
        print("Set RC switch override to %u (PWM=%u)" % (value, mapping[value]))

def cmd_trim(args, rl):
    '''trim aileron, elevator and rudder to current values'''
    if not 'RC_CHANNELS_RAW' in mpstate.status.msgs:
        print("No RC_CHANNELS_RAW to trim with")
        return
    m = mpstate.status.msgs['RC_CHANNELS_RAW']

    mpstate.master().param_set_send('ROLL_TRIM',  m.chan1_raw)
    mpstate.master().param_set_send('PITCH_TRIM', m.chan2_raw)
    mpstate.master().param_set_send('YAW_TRIM',   m.chan4_raw)
    print("Trimmed to aileron=%u elevator=%u rudder=%u" % (
        m.chan1_raw, m.chan2_raw, m.chan4_raw))
    

def cmd_rc(args, rl):
    '''handle RC value override'''
    if len(args) != 2:
        print("Usage: rc <channel> <pwmvalue>")
        return
    channel = int(args[0])
    value   = int(args[1])
    if value == -1:
        value = 65535
    if channel < 1 or channel > 8:
        print("Channel must be between 1 and 8")
        return
    mpstate.status.override[channel-1] = value
    send_rc_override()

def cmd_loiter(args, rl):
    '''set LOITER mode'''
    MAV_ACTION_LOITER = 27
    mpstate.master().mav.action_send(mpstate.status.target_system, mpstate.status.target_component, MAV_ACTION_LOITER)

def cmd_auto(args, rl):
    '''set AUTO mode'''
    MAV_ACTION_SET_AUTO = 13
    mpstate.master().mav.action_send(mpstate.status.target_system, mpstate.status.target_component, MAV_ACTION_SET_AUTO)

def cmd_ground(args, rl):
    '''do a ground start mode'''
    MAV_ACTION_CALIBRATE_GYRO = 17
    mpstate.master().mav.action_send(mpstate.status.target_system, mpstate.status.target_component, MAV_ACTION_CALIBRATE_GYRO)

def cmd_rtl(args, rl):
    '''set RTL mode'''
    MAV_ACTION_RETURN = 3
    mpstate.master().mav.action_send(mpstate.status.target_system, mpstate.status.target_component, MAV_ACTION_RETURN)

def cmd_manual(args, rl):
    '''set MANUAL mode'''
    MAV_ACTION_SET_MANUAL = 12
    mpstate.master().mav.action_send(mpstate.status.target_system, mpstate.status.target_component, MAV_ACTION_SET_MANUAL)

def process_waypoint_request(m, master):
    '''process a waypoint request from the master'''
    if (not mpstate.status.loading_waypoints or
        time.time() > mpstate.status.loading_waypoint_lasttime + 10.0):
        mpstate.status.loading_waypoints = False
        print("not loading waypoints")
        return
    if m.seq >= mpstate.status.wploader.count():
        print("Request for bad waypoint %u (max %u)" % (m.seq, mpstate.status.wploader.count()))
        return
    master.mav.send(mpstate.status.wploader.wp(m.seq))
    mpstate.status.loading_waypoint_lasttime = time.time()
    print("Sent waypoint %u : %s" % (m.seq, mpstate.status.wploader.wp(m.seq)))
    if m.seq == mpstate.status.wploader.count() - 1:
        mpstate.status.loading_waypoints = False
        print("Sent all %u waypoints" % mpstate.status.wploader.count())

def load_waypoints(filename):
    '''load waypoints from a file'''
    mpstate.status.wploader.target_system = mpstate.status.target_system
    mpstate.status.wploader.target_component = mpstate.status.target_component
    try:
        mpstate.status.wploader.load(filename)
    except Exception, msg:
        print("Unable to load %s - %s" % (filename, msg))
        return
    print("Loaded %u waypoints from %s" % (mpstate.status.wploader.count(), filename))

    mpstate.master().waypoint_clear_all_send()
    if mpstate.status.wploader.count() == 0:
        return

    mpstate.status.loading_waypoints = True
    mpstate.status.loading_waypoint_lasttime = time.time()
    mpstate.master().waypoint_count_send(mpstate.status.wploader.count())

def save_waypoints(filename):
    '''save waypoints to a file'''
    try:
        mpstate.status.wploader.save(filename)
    except Exception, msg:
        print("Failed to save %s - %s" % (filename, msg))
        return
    print("Saved %u waypoints to %s" % (mpstate.status.wploader.count(), filename))
             

def cmd_wp(args, rl):
    '''waypoint commands'''
    if len(args) < 1:
        print("usage: wp <list|load|save|set|clear>")
        return

    if args[0] == "load":
        if len(args) != 2:
            print("usage: wp load <filename>")
            return
        load_waypoints(args[1])
    elif args[0] == "list":
        mpstate.status.wp_op = "list"
        mpstate.master().waypoint_request_list_send()
    elif args[0] == "save":
        if len(args) != 2:
            print("usage: wp save <filename>")
            return
        mpstate.status.wp_save_filename = args[1]
        mpstate.status.wp_op = "save"
        mpstate.master().waypoint_request_list_send()
    elif args[0] == "set":
        if len(args) != 2:
            print("usage: wp set <wpindex>")
            return
        mpstate.master().waypoint_set_current_send(int(args[1]))
    elif args[0] == "clear":
        mpstate.master().waypoint_clear_all_send()
    else:
        print("Usage: wp <list|load|save|set|clear>")


def param_set(name, value, retries=3):
    '''set a parameter'''
    got_ack = False
    while retries > 0 and not got_ack:
        retries -= 1
        mpstate.master().param_set_send(name, float(value))
        tstart = time.time()
        while time.time() - tstart < 1:
            ack = mpstate.master().recv_match(type='PARAM_VALUE', blocking=False)
            if ack == None:
                time.sleep(0.1)
                continue
            if str(name) == str(ack.param_id):
                got_ack = True
                break
    if not got_ack:
        print("timeout setting %s to %f" % (name, float(value)))
        return False
    return True


def param_save(filename, wildcard):
    '''save parameters to a file'''
    f = open(filename, mode='w')
    k = mpstate.mav_param.keys()
    k.sort()
    count = 0
    for p in k:
        if p and fnmatch.fnmatch(str(p).upper(), wildcard.upper()):
            f.write("%-15.15s %f\n" % (p, mpstate.mav_param[p]))
            count += 1
    f.close()
    print("Saved %u parameters to %s" % (count, filename))


def param_load_file(filename, wildcard):
    '''load parameters from a file'''
    try:
        f = open(filename, mode='r')
    except:
        print("Failed to open file '%s'" % filename)
        return
    count = 0
    changed = 0
    for line in f:
        line = line.strip()
        if not line or line[0] == "#":
            continue
        a = line.split()
        if len(a) != 2:
            print("Invalid line: %s" % line)
            continue
        if a[0] in ['SYSID_SW_MREV', 'SYS_NUM_RESETS', 'ARSPD_OFFSET', 'GND_ABS_PRESS', 'GND_TEMP' ]:
            continue
        if not fnmatch.fnmatch(a[0].upper(), wildcard.upper()):
            continue
        if a[0] not in mpstate.mav_param:
            print("Unknown parameter %s" % a[0])
            continue
        old_value = mpstate.mav_param[a[0]]
        if math.fabs(old_value - float(a[1])) > 0.000001:
            if param_set(a[0], a[1]):
                print("changed %s from %f to %f" % (a[0], old_value, float(a[1])))
            changed += 1
        count += 1
    f.close()
    print("Loaded %u parameters from %s (changed %u)" % (count, filename, changed))
    

param_wildcard = "*"

def cmd_param(args, rl):
    '''control parameters'''
    if len(args) < 1:
        print("usage: param <fetch|edit|set|show|store>")
        return
    if args[0] == "fetch":
        mpstate.master().param_fetch_all()
        print("Requested parameter list")
    elif args[0] == "save":
        if len(args) < 2:
            print("usage: param save <filename> [wildcard]")
            return
        if len(args) > 2:
            param_wildcard = args[2]
        else:
            param_wildcard = "*"
        param_save(args[1], param_wildcard)
    elif args[0] == "set":
        if len(args) != 3:
            print("Usage: param set PARMNAME VALUE")
            return
        param = args[1]
        value = args[2]
        if not param in mpstate.mav_param:
            print("Warning: Unable to find parameter '%s'" % param)
        param_set(param, value)
    elif args[0] == "load":
        if len(args) < 2:
            print("Usage: param load <filename> [wildcard]")
            return
        if len(args) > 2:
            param_wildcard = args[2]
        else:
            param_wildcard = "*"
        param_load_file(args[1], param_wildcard);
    elif args[0] == "show":
        if len(args) > 1:
            pattern = args[1]
        else:
            pattern = "*"
        k = sorted(mpstate.mav_param.keys())
        for p in k:
            if fnmatch.fnmatch(str(p).upper(), pattern.upper()):
                print("%-15.15s %f" % (str(p), mpstate.mav_param[p]))
    elif args[0] == "store":
        MAV_ACTION_STORAGE_WRITE = 15
        mpstate.master().mav.action_send(mpstate.status.target_system, mpstate.status.target_component, MAV_ACTION_STORAGE_WRITE)
    else:
        print("Unknown subcommand '%s' (try 'fetch', 'save', 'set', 'show', 'load' or 'store')" % args[0]);

def cmd_set(args, rl):
    '''control mavproxy options'''
    if len(args) == 0:
        mpstate.settings.show_all()
        return

    if getattr(mpstate.settings, args[0], None) is None:
        print("Unknown setting '%s'" % args[0])
        return
    if len(args) == 1:
        mpstate.settings.show(args[0])
    else:
        mpstate.settings.set(args[0], args[1])

def cmd_status(args, rl):
    '''show status'''
    if len(args) == 0:
        mpstate.status.show(sys.stdout, pattern=None)
    else:
        for pattern in args:
            mpstate.status.show(sys.stdout, pattern=pattern)

def cmd_bat(args, rl):
    '''show battery levels'''
    print("Flight battery:   %u%%" % mpstate.status.battery_level)
    print("Avionics battery: %u%%" % mpstate.status.avionics_battery_level)


def cmd_up(args, rl):
    '''adjust TRIM_PITCH_CD up by 5 degrees'''
    if len(args) == 0:
        adjust = 5.0
    else:
        adjust = float(args[0])
    old_trim = get_mav_param('TRIM_PITCH_CD', None)
    if old_trim is None:
        print("Existing trim value unknown!")
        return
    new_trim = int(old_trim + (adjust*100))
    if math.fabs(new_trim - old_trim) > 1000:
        print("Adjustment by %d too large (from %d to %d)" % (adjust*100, old_trim, new_trim))
        return
    print("Adjusting TRIM_PITCH_CD from %d to %d" % (old_trim, new_trim))
    param_set('TRIM_PITCH_CD', new_trim)


def cmd_setup(args, rl):
    mpstate.status.setup_mode = True
    rl.set_prompt("")


def cmd_reset(args, rl):
    print("Resetting master")
    mpstate.master().reset()

def cmd_link(args, rl):
    for master in mpstate.mav_master:
        linkdelay = (mpstate.status.highest_usec - master.highest_usec)*1e-6
        if master.linkerror:
            print("link %u down" % (master.linknum+1))
        elif master.link_delayed:
            print("link %u delayed by %.2f seconds" % (master.linknum+1, linkdelay))
        else:
            print("link %u OK (%u packets, %.2fs delay)" % (master.linknum+1,
                                                            mpstate.status.counters['MasterIn'][master.linknum],
                                                            linkdelay))


command_map = {
    'switch'  : (cmd_switch,   'set RC switch (1-5), 0 disables'),
    'rc'      : (cmd_rc,       'override a RC channel value'),
    'wp'      : (cmd_wp,       'waypoint management'),
    'param'   : (cmd_param,    'manage APM parameters'),
    'setup'   : (cmd_setup,    'go into setup mode'),
    'reset'   : (cmd_reset,    'reopen the connection to the MAVLink master'),
    'status'  : (cmd_status,   'show status'),
    'trim'    : (cmd_trim,     'trim aileron, elevator and rudder to current values'),
    'auto'    : (cmd_auto,     'set AUTO mode'),
    'ground'  : (cmd_ground,   'do a ground start'),
    'loiter'  : (cmd_loiter,   'set LOITER mode'),
    'rtl'     : (cmd_rtl,      'set RTL mode'),
    'manual'  : (cmd_manual,   'set MANUAL mode'),
    'set'     : (cmd_set,      'mavproxy settings'),
    'bat'     : (cmd_bat,      'show battery levels'),
    'link'    : (cmd_link,     'show link status'),
    'up'      : (cmd_up,       'adjust TRIM_PITCH_CD up by 5 degrees'),
    };

def process_stdin(rl, line):
    '''handle commands from user'''
    if line is None:
        sys.exit(0)
    line = line.strip()

    if mpstate.status.setup_mode:
        # in setup mode we send strings straight to the master
        if line == '.':
            mpstate.status.setup_mode = False
            rl.set_prompt("MAV> ")
            return
        mpstate.master().write(line + '\r')
        return

    if not line:
        return

    args = line.split(" ")
    cmd = args[0]
    if cmd == 'help':
        k = command_map.keys()
        k.sort()
        for cmd in k:
            (fn, help) = command_map[cmd]
            print("%-15s : %s" % (cmd, help))
        return
    if not cmd in command_map:
        print("Unknown command '%s'" % line)
        return
    (fn, help) = command_map[cmd]
    try:
        fn(args[1:], rl)
    except Exception as e:
        print("ERROR in command: %s" % str(e))


def scale_rc(servo, min, max, param):
    '''scale a PWM value'''
    # default to servo range of 1000 to 2000
    min_pwm  = get_mav_param('%s_MIN'  % param, 0)
    max_pwm  = get_mav_param('%s_MAX'  % param, 0)
    if min_pwm == 0 or max_pwm == 0:
        return 0
    if max_pwm == min_pwm:
        p = 0.0
    else:
        p = (servo-min_pwm) / float(max_pwm-min_pwm)
    v = min + p*(max-min)
    if v < min:
        v = min
    if v > max:
        v = max
    return v


def system_check():
    '''check that the system is ready to fly'''
    ok = True

    if mavlink.WIRE_PROTOCOL_VERSION == '1.0':
        if not 'GPS_RAW_INT' in mpstate.status.msgs:
            say("WARNING no GPS status")
            return
        if mpstate.status.msgs['GPS_RAW_INT'].fix_type != 2:
            say("WARNING no GPS lock")
            ok = False
    else:
        if not 'GPS_RAW' in mpstate.status.msgs and not 'GPS_RAW_INT' in mpstate.status.msgs:
            say("WARNING no GPS status")
            return
        if mpstate.status.msgs['GPS_RAW'].fix_type != 2:
            say("WARNING no GPS lock")
            ok = False

    if not 'PITCH_MIN' in mpstate.mav_param:
        say("WARNING no pitch parameter available")
        return
        
    if int(mpstate.mav_param['PITCH_MIN']) > 1300:
        say("WARNING PITCH MINIMUM not set")
        ok = False

    if not 'ATTITUDE' in mpstate.status.msgs:
        say("WARNING no attitude recorded")
        return

    if math.fabs(mpstate.status.msgs['ATTITUDE'].pitch) > math.radians(5):
        say("WARNING pitch is %u degrees" % math.degrees(mpstate.status.msgs['ATTITUDE'].pitch))
        ok = False

    if math.fabs(mpstate.status.msgs['ATTITUDE'].roll) > math.radians(5):
        say("WARNING roll is %u degrees" % math.degrees(mpstate.status.msgs['ATTITUDE'].roll))
        ok = False

    if ok:
        say("All OK SYSTEM READY TO FLY")


def beep():
    f = open("/dev/tty", mode="w")
    f.write(chr(7))
    f.close()

def vcell_to_battery_percent(vcell):
    '''convert a cell voltage to a percentage battery level'''
    if vcell > 4.1:
        # above 4.1 is 100% battery
        return 100.0
    elif vcell > 3.81:
        # 3.81 is 17% remaining, from flight logs
        return 17.0 + 83.0 * (vcell - 3.81) / (4.1 - 3.81)
    elif vcell > 3.81:
        # below 3.2 it degrades fast. It's dead at 3.2
        return 0.0 + 17.0 * (vcell - 3.20) / (3.81 - 3.20)
    # it's dead or disconnected
    return 0.0


def battery_update(SYS_STATUS):
    '''update battery level'''

    # main flight battery
    mpstate.status.battery_level = SYS_STATUS.battery_remaining/10.0

    # avionics battery
    if not 'AP_ADC' in mpstate.status.msgs:
        return
    rawvalue = float(mpstate.status.msgs['AP_ADC'].adc2)
    INPUT_VOLTAGE = 4.68
    VOLT_DIV_RATIO = 3.56
    voltage = rawvalue*(INPUT_VOLTAGE/1024.0)*VOLT_DIV_RATIO
    vcell = voltage / mpstate.settings.numcells

    avionics_battery_level = vcell_to_battery_percent(vcell)

    if mpstate.status.avionics_battery_level == -1 or abs(avionics_battery_level-mpstate.status.avionics_battery_level) > 70:
        mpstate.status.avionics_battery_level = avionics_battery_level
    else:
        mpstate.status.avionics_battery_level = (95*mpstate.status.avionics_battery_level + 5*avionics_battery_level)/100



def battery_report():
    '''report battery level'''
    if int(mpstate.settings.battreadout) == 0:
        return

    rbattery_level = int((mpstate.status.battery_level+5)/10)*10;

    if rbattery_level != mpstate.status.last_battery_announce:
        say("Flight battery %u percent" % rbattery_level,priority='notification')
        mpstate.status.last_battery_announce = rbattery_level
    if rbattery_level <= 20:
        say("Flight battery warning")

    # avionics battery reporting disabled for now
    return
    avionics_rbattery_level = int((mpstate.status.avionics_battery_level+5)/10)*10;

    if avionics_rbattery_level != mpstate.status.last_avionics_battery_announce:
        say("Avionics Battery %u percent" % avionics_rbattery_level,priority='notification')
        mpstate.status.last_avionics_battery_announce = avionics_rbattery_level
    if avionics_rbattery_level <= 20:
        say("Avionics battery warning")


def handle_usec_timestamp(m, master):
    '''special handling for MAVLink packets with a usec field'''
    usec = m.usec
    if usec + 6.0e7 < master.highest_usec:
        say('Time has wrapped')
        print("usec %u highest_usec %u" % (usec, master.highest_usec))
        mpstate.status.highest_usec = usec
        for mm in mpstate.mav_master:
            mm.link_delayed = False
            mm.highest_usec = usec
        return

    # we want to detect when a link has significant buffering, causing us to receive
    # old packets. If we get packets that are more than 1 second old, then mark the link
    # as being delayed. We will not act on packets from this link until it has caught up
    master.highest_usec = usec
    if usec > mpstate.status.highest_usec:
        mpstate.status.highest_usec = usec
    if usec + 1e6 < mpstate.status.highest_usec and not master.link_delayed:
        master.link_delayed = True
        say("link %u delayed" % (master.linknum+1))
    elif usec + 0.5e6 > mpstate.status.highest_usec and master.link_delayed:
        master.link_delayed = False
        say("link %u OK" % (master.linknum+1))
    

def master_callback(m, master):
    '''process mavlink message m on master, sending any messages to recipients'''

    if getattr(m, '_timestamp', None) is None:
        master.post_message(m)
    mpstate.status.counters['MasterIn'][master.linknum] += 1

    if getattr(m, 'usec', None) is not None:
        # update link_delayed attribute
        handle_usec_timestamp(m, master)

    mtype = m.get_type()

    # and log them
    if mtype != 'BAD_DATA' and mpstate.logqueue:
        # put link number in bottom 2 bits, so we can analyse packet
        # delay in saved logs
        usec = get_usec()
        usec = (usec & ~3) | master.linknum
        mpstate.logqueue.put(str(struct.pack('>Q', usec) + m.get_msgbuf().tostring()))

    if master.link_delayed:
        # don't process delayed packets 
        print("skip delayed: %s" % m)
        return
    
    if mtype == 'HEARTBEAT':
        if (mpstate.status.target_system != m.get_srcSystem() or
            mpstate.status.target_component != m.get_srcComponent()):
            mpstate.status.target_system = m.get_srcSystem()
            mpstate.status.target_component = m.get_srcComponent()
            say("online system %u component %u" % (mpstate.status.target_system, mpstate.status.target_component),'message')
        if mpstate.status.heartbeat_error:
            mpstate.status.heartbeat_error = False
            say("heartbeat OK")
        if master.linkerror:
            master.linkerror = False
            say("link %u OK" % (master.linknum+1))
            
        mpstate.status.last_heartbeat = time.time()
        master.last_heartbeat = mpstate.status.last_heartbeat
    elif mtype == 'STATUSTEXT':
        if m.text != mpstate.status.last_apm_msg:
            print("APM: %s" % m.text)
            mpstate.status.last_apm_msg = m.text
    elif mtype == 'PARAM_VALUE':
        mpstate.mav_param[str(m.param_id)] = m.param_value
        if m.param_index+1 == m.param_count:
            print("Received %u parameters" % m.param_count)
            if mpstate.status.logdir != None:
                param_save(os.path.join(mpstate.status.logdir, 'mav.parm'), '*')

    elif mtype == 'SERVO_OUTPUT_RAW':
        if opts.quadcopter:
            mpstate.status.rc_throttle[0] = scale_rc(m.servo1_raw, 0.0, 1.0, param='RC3')
            mpstate.status.rc_throttle[1] = scale_rc(m.servo2_raw, 0.0, 1.0, param='RC3')
            mpstate.status.rc_throttle[2] = scale_rc(m.servo3_raw, 0.0, 1.0, param='RC3')
            mpstate.status.rc_throttle[3] = scale_rc(m.servo4_raw, 0.0, 1.0, param='RC3')
        else:
            mpstate.status.rc_aileron  = scale_rc(m.servo1_raw, -1.0, 1.0, param='RC1') * mpstate.settings.rc1mul
            mpstate.status.rc_elevator = scale_rc(m.servo2_raw, -1.0, 1.0, param='RC2') * mpstate.settings.rc2mul
            mpstate.status.rc_throttle = scale_rc(m.servo3_raw, 0.0, 1.0, param='RC3')
            mpstate.status.rc_rudder   = scale_rc(m.servo4_raw, -1.0, 1.0, param='RC4') * mpstate.settings.rc4mul
            if mpstate.status.rc_throttle < 0.1:
                mpstate.status.rc_throttle = 0

    elif mtype in ['WAYPOINT_COUNT','MISSION_COUNT']:
        if mpstate.status.wp_op is None:
            print("No waypoint load started")
        else:
            mpstate.status.wploader.clear()
            mpstate.status.wploader.expected_count = m.count
            print("Requesting %u waypoints t=%s now=%s" % (m.count,
                                                           time.asctime(time.localtime(m._timestamp)),
                                                           time.asctime()))
            master.waypoint_request_send(0)

    elif mtype in ['WAYPOINT', 'MISSION_ITEM'] and mpstate.status.wp_op != None:
        if m.seq > mpstate.status.wploader.count():
            print("Unexpected waypoint number %u - expected %u" % (m.seq, mpstate.status.wploader.count()))
        elif m.seq < mpstate.status.wploader.count():
            # a duplicate
            pass
        else:
            mpstate.status.wploader.add(m)
        if m.seq+1 < mpstate.status.wploader.expected_count:
            master.waypoint_request_send(m.seq+1)
        else:
            if mpstate.status.wp_op == 'list':
                for i in range(mpstate.status.wploader.count()):
                    w = mpstate.status.wploader.wp(i)
                    print("%u %u %.10f %.10f %f p1=%.1f p2=%.1f p3=%.1f p4=%.1f cur=%u auto=%u" % (
                        w.command, w.frame, w.x, w.y, w.z,
                        w.param1, w.param2, w.param3, w.param4,
                        w.current, w.autocontinue))
            elif mpstate.status.wp_op == "save":
                save_waypoints(mpstate.status.wp_save_filename)
            mpstate.status.wp_op = None

    elif mtype in ["WAYPOINT_REQUEST", "MISSION_REQUEST"]:
        process_waypoint_request(m, master)

    elif mtype in ["WAYPOINT_CURRENT", "MISSION_CURRENT"]:
        if m.seq != mpstate.status.last_waypoint:
            mpstate.status.last_waypoint = m.seq
            say("waypoint %u" % m.seq,priority='message')

    elif mtype == "SYS_STATUS":
        battery_update(m)
        if master.flightmode != mpstate.status.flightmode:
            mpstate.status.flightmode = master.flightmode
            rl.set_prompt(mpstate.status.flightmode + "> ")
            say("Mode " + mpstate.status.flightmode)

    elif mtype == "VFR_HUD":
        have_gps_fix = False
        if 'GPS_RAW' in mpstate.status.msgs and mpstate.status.msgs['GPS_RAW'].fix_type == 2:
            have_gps_fix = True
        if 'GPS_RAW_INT' in mpstate.status.msgs and mpstate.status.msgs['GPS_RAW_INT'].fix_type == 2:
            have_gps_fix = True
        if have_gps_fix and m.alt != 0.0:
            if mpstate.settings.basealtitude == -1:
                mpstate.settings.basealtitude = m.alt
                mpstate.status.last_altitude_announce = 0.0
                say("GPS lock at %u meters" % m.alt, priority='notification')
            else:
                if m.alt < mpstate.settings.basealtitude:
                    mpstate.settings.basealtitude = m.alt
                    mpstate.status.last_altitude_announce = m.alt
                if (int(mpstate.settings.altreadout) > 0 and
                    math.fabs(m.alt - mpstate.status.last_altitude_announce) >= int(mpstate.settings.altreadout)):
                    mpstate.status.last_altitude_announce = m.alt
                    rounded_alt = int(mpstate.settings.altreadout) * ((5+int(m.alt - mpstate.settings.basealtitude)) / int(mpstate.settings.altreadout))
                    say("height %u" % rounded_alt, priority='notification')

    elif mtype == "RC_CHANNELS_RAW":
        if (m.chan7_raw > 1700 and mpstate.status.flightmode == "MANUAL"):
            system_check()
        if mpstate.settings.radiosetup:
            for i in range(1,9):
                v = getattr(m, 'chan%u_raw' % i)
                rcmin = get_mav_param('RC%u_MIN' % i, 0)
                if rcmin > v:
                    if param_set('RC%u_MIN' % i, v):
                        print("Set RC%u_MIN=%u" % (i, v))
                rcmax = get_mav_param('RC%u_MAX' % i, 0)
                if rcmax < v:
                    if param_set('RC%u_MAX' % i, v):
                        print("Set RC%u_MAX=%u" % (i, v))

    elif mtype == "NAV_CONTROLLER_OUTPUT" and mpstate.status.flightmode == "AUTO" and mpstate.settings.distreadout:
        rounded_dist = int(m.wp_dist/mpstate.settings.distreadout)*mpstate.settings.distreadout
        if math.fabs(rounded_dist - mpstate.status.last_distance_announce) >= mpstate.settings.distreadout:
            if rounded_dist != 0:
                say("%u" % rounded_dist, priority="progress")
            mpstate.status.last_distance_announce = rounded_dist

    elif mtype == "BAD_DATA":
        if mavutil.all_printable(m.data):
            sys.stdout.write(m.data)
            sys.stdout.flush()
    elif mtype in [ 'HEARTBEAT', 'GLOBAL_POSITION', 'RC_CHANNELS_SCALED',
                    'ATTITUDE', 'RC_CHANNELS_RAW', 'GPS_STATUS', 'WAYPOINT_CURRENT',
                    'SERVO_OUTPUT_RAW', 'VFR_HUD',
                    'GLOBAL_POSITION_INT', 'RAW_PRESSURE', 'RAW_IMU',
                    'WAYPOINT_ACK', 'MISSION_ACK',
                    'NAV_CONTROLLER_OUTPUT', 'GPS_RAW', 'GPS_RAW_INT', 'WAYPOINT',
                    'SCALED_PRESSURE', 'SENSOR_OFFSETS', 'MEMINFO', 'AP_ADC' ]:
        pass
    else:
        print("Got MAVLink msg: %s" % m)

    # keep the last message of each type around
    mpstate.status.msgs[m.get_type()] = m
    if not m.get_type() in mpstate.status.msg_count:
        mpstate.status.msg_count[m.get_type()] = 0
    mpstate.status.msg_count[m.get_type()] += 1

    # don't pass along bad data
    if mtype != "BAD_DATA":
        # pass messages along to listeners
        for r in mpstate.mav_outputs:
            r.write(m.get_msgbuf().tostring())


def process_master(m):
    '''process packets from the MAVLink master'''
    try:
        s = m.recv()
    except Exception:
        return
    if mpstate.logqueue_raw:
        mpstate.logqueue_raw.put(str(s))

    if mpstate.status.setup_mode:
        sys.stdout.write(str(s))
        sys.stdout.flush()
        return

    msgs = m.mav.parse_buffer(s)
    if msgs:
        for msg in msgs:
            m.post_message(msg)
            if msg.get_type() == "BAD_DATA":
                if opts.show_errors:
                    print("MAV error: %s" % msg)
                mpstate.status.mav_error += 1

    

def process_mavlink(slave):
    '''process packets from MAVLink slaves, forwarding to the master'''
    try:
        buf = slave.recv()
    except socket.error:
        return
    try:
        m = slave.mav.decode(buf)
    except mavlink.MAVError as e:
        print("Bad MAVLink slave message from %s: %s" % (slave.address, e.message))
        return
    if mpstate.settings.mavfwd and not mpstate.status.setup_mode:
        mpstate.master().write(m.get_msgbuf())
    mpstate.status.counters['Slave'] += 1


def mkdir_p(dir):
    '''like mkdir -p'''
    if not dir:
        return
    if dir.endswith("/"):
        mkdir_p(dir[:-1])
        return
    if os.path.isdir(dir):
        return
    mkdir_p(os.path.dirname(dir))
    os.mkdir(dir)

def log_writer():
    '''log writing thread'''
    while True:
        mpstate.logfile_raw.write(mpstate.logqueue_raw.get())
        while not mpstate.logqueue_raw.empty():
            mpstate.logfile_raw.write(mpstate.logqueue_raw.get())
        while not mpstate.logqueue.empty():
            mpstate.logfile.write(mpstate.logqueue.get())
        mpstate.logfile.flush()
        mpstate.logfile_raw.flush()
        if status_period.trigger():
            mpstate.status.write()

def open_logs():
    '''open log files'''
    if opts.append_log:
        mode = 'a'
    else:
        mode = 'w'
    logfile = opts.logfile
    if opts.aircraft is not None:
        dirname = "%s/logs/%s" % (opts.aircraft, time.strftime("%Y-%m-%d"))
        mkdir_p(dirname)
        for i in range(1, 10000):
            fdir = os.path.join(dirname, 'flight%u' % i)
            if not os.path.exists(fdir):
                break
        if os.path.exists(fdir):
            print("Flight logs full")
            sys.exit(1)
        mkdir_p(fdir)
        print(fdir)
        logfile = os.path.join(fdir, logfile)
        mpstate.status.logdir = fdir
    print("Logging to %s" % logfile)
    mpstate.logfile = open(logfile, mode=mode)
    mpstate.logfile_raw = open(logfile+'.raw', mode=mode)

    # queues for logging
    mpstate.logqueue = Queue.Queue()
    mpstate.logqueue_raw = Queue.Queue()

    # use a separate thread for writing to the logfile to prevent
    # delays during disk writes (important as delays can be long if camera
    # app is running)
    t = threading.Thread(target=log_writer)
    t.daemon = True
    t.start()

def set_stream_rates():
    '''set mavlink stream rates'''
    for master in mpstate.mav_master:
        if master.linknum == 0:
            rate = mpstate.settings.streamrate
        else:
            rate = mpstate.settings.streamrate2
        master.mav.request_data_stream_send(mpstate.status.target_system, mpstate.status.target_component,
                                            mavlink.MAV_DATA_STREAM_ALL,
                                            rate, 1)

def check_link_status():
    '''check status of master links'''
    tnow = time.time()
    if mpstate.status.last_heartbeat != 0 and tnow > mpstate.status.last_heartbeat + 5:
        say("no heartbeat")
        mpstate.status.heartbeat_error = True
    for master in mpstate.mav_master:
        if not master.linkerror and tnow > master.last_heartbeat + 5:
            say("link %u down" % (master.linknum+1))
            master.linkerror = True

def periodic_tasks():
    '''run periodic checks'''
    if (mpstate.status.setup_mode or
        mpstate.status.target_system == -1 or
        mpstate.status.target_component == -1):
        return

    if heartbeat_period.trigger() and mpstate.settings.heartbeat != 0:
        mpstate.status.counters['MasterOut'] += 1
        for master in mpstate.mav_master:
            if mavlink.WIRE_PROTOCOL_VERSION == '1.0':
                master.mav.heartbeat_send(mavlink.MAV_TYPE_GCS, mavlink.MAV_AUTOPILOT_INVALID,
                                          0, 0, 0)
            else:
                MAV_GROUND = 5
                MAV_AUTOPILOT_NONE = 4
                master.mav.heartbeat_send(MAV_GROUND, MAV_AUTOPILOT_NONE)

    if heartbeat_check_period.trigger():
        check_link_status()

    if msg_period.trigger():
        set_stream_rates()

    for master in mpstate.mav_master:
        if not master.param_fetch_complete and master.time_since('PARAM_VALUE') > 2:
            master.param_fetch_all()
 
    if battery_period.trigger():
        battery_report()

    if override_period.trigger():
        if mpstate.status.override != [ 0 ] * 8:
            send_rc_override()


def main_loop():
    '''main processing loop'''
    if not mpstate.status.setup_mode:
        for master in mpstate.mav_master:
            master.wait_heartbeat()
            master.param_fetch_all()
        set_stream_rates()

    while True:
        if mpstate.status.exit:
            return
        if rl.line is not None:
            process_stdin(rl, rl.line)
            rl.line = None

        for master in mpstate.mav_master:
            if master.fd is None:
                if master.port.inWaiting() > 0:
                    process_master(master)

        periodic_tasks()
    
        rin = []
        for master in mpstate.mav_master:
            if master.fd is not None:
                rin.append(master.fd)
        for m in mpstate.mav_outputs:
            rin.append(m.fd)
        if rin == []:
            time.sleep(0.001)
            continue
        try:
            (rin, win, xin) = select.select(rin, [], [], 0.001)
        except select.error:
            continue

        for fd in rin:
            for master in mpstate.mav_master:
                if fd == master.fd:
                    process_master(master)
            for m in mpstate.mav_outputs:
                if fd == m.fd:
                    process_mavlink(m)


def input_loop():
    '''wait for user input'''
    while True:
        while rl.line is not None:
            time.sleep(0.01)
        try:
            line = raw_input(rl.prompt)
        except EOFError:
            mpstate.status.exit = True
            sys.exit(1)
        rl.line = line
            

def run_script(scriptfile):
    '''run a script file'''
    try:
        f = open(scriptfile, mode='r')
    except Exception:
        return
    print("Running script %s" % scriptfile)
    for line in f:
        line = line.strip()
        if line == "":
            continue
        print("-> %s" % line)
        process_stdin(rl, line)
    f.close()
        

if __name__ == '__main__':

    from optparse import OptionParser
    parser = OptionParser("mavproxy.py [options]")

    parser.add_option("--master",dest="master", action='append', help="MAVLink master port", default=[])
    parser.add_option("--baudrate", dest="baudrate", type='int',
                      help="master port baud rate", default=115200)
    parser.add_option("--out",   dest="output", help="MAVLink output port",
                      action='append', default=[])
    parser.add_option("--sitl", dest="sitl",  default=None, help="SITL output port")
    parser.add_option("--streamrate",dest="streamrate", default=4, type='int',
                      help="MAVLink stream rate")
    parser.add_option("--source-system", dest='SOURCE_SYSTEM', type='int',
                      default=255, help='MAVLink source system for this GCS')
    parser.add_option("--target-system", dest='TARGET_SYSTEM', type='int',
                      default=-1, help='MAVLink target master system')
    parser.add_option("--target-component", dest='TARGET_COMPONENT', type='int',
                      default=-1, help='MAVLink target master component')
    parser.add_option("--logfile", dest="logfile", help="MAVLink master logfile",
                      default='mav.log')
    parser.add_option("-a", "--append-log", dest="append_log", help="Append to log files",
                      action='store_true', default=False)
    parser.add_option("--quadcopter", dest="quadcopter", help="use quadcopter controls",
                      action='store_true', default=False)
    parser.add_option("--setup", dest="setup", help="start in setup mode",
                      action='store_true', default=False)
    parser.add_option("--nodtr", dest="nodtr", help="disable DTR drop on close",
                      action='store_true', default=False)
    parser.add_option("--show-errors", dest="show_errors", help="show MAVLink error packets",
                      action='store_true', default=False)
    parser.add_option("--speech", dest="speech", help="use text to speach",
                      action='store_true', default=False)
    parser.add_option("--num-cells", dest="num_cells", help="number of LiPo battery cells",
                      type='int', default=0)
    parser.add_option("--aircraft", dest="aircraft", help="aircraft name", default=None)
    parser.add_option("--mav10", action='store_true', default=False, help="Use MAVLink protocol 1.0")
    
    (opts, args) = parser.parse_args()

    if opts.mav10:
        import mavlinkv10 as mavlink
        os.environ['MAVLINK10'] = '1'
    else:
        import mavlink as mavlink
    import mavutil, mavwp

    # global mavproxy state
    mpstate = MPState()

    if not opts.master:
        serial_list = mavutil.auto_detect_serial(preferred_list=['*FTDI*',"*Arduino_Mega_2560*"])
        if len(serial_list) == 1:
            opts.master = [serial_list[0].device]
        else:
            print('''
Please choose a MAVLink master with --master
For example:
    --master=com14
    --master=/dev/ttyUSB0
    --master=127.0.0.1:14550

Auto-detected serial ports are:
''')
            for port in serial_list:
                print("%s" % port)
            sys.exit(1)

    # container for status information
    mpstate.status.target_system = opts.TARGET_SYSTEM
    mpstate.status.target_component = opts.TARGET_COMPONENT

    mpstate.mav_master = []
    
    # open master link
    for mdev in opts.master:
        if mdev.startswith('tcp:'):
            m = mavutil.mavtcp(mdev[4:])
        elif mdev.find(':') != -1:
            m = mavutil.mavudp(mdev, input=True)
        elif mdev.endswith(".elf"):
            m = mavutil.mavchildexec(mdev)  
        else:
            m = mavutil.mavserial(mdev, baud=opts.baudrate)
        m.mav.set_callback(master_callback, m)
        m.linknum = len(mpstate.mav_master)
        m.linkerror = False
        m.link_delayed = False
        m.last_heartbeat = 0
        m.highest_usec = 0
        mpstate.mav_master.append(m)
        mpstate.status.counters['MasterIn'].append(0)

    # log all packets from the master, for later replay
    open_logs()

    # open any mavlink UDP ports
    for p in opts.output:
        mpstate.mav_outputs.append(mavutil.mavudp(p, input=False))

    if opts.sitl:
        mpstate.sitl_output = mavutil.mavudp(opts.sitl, input=False)

    mpstate.settings.numcells = opts.num_cells
    mpstate.settings.speech = opts.speech
    mpstate.settings.streamrate = opts.streamrate
    mpstate.settings.streamrate2 = opts.streamrate

    status_period = mavutil.periodic_event(1.0)
    msg_period = mavutil.periodic_event(1.0/30)
    heartbeat_period = mavutil.periodic_event(1)
    battery_period = mavutil.periodic_event(0.1)
    if mpstate.sitl_output:
        override_period = mavutil.periodic_event(50)
    else:
        override_period = mavutil.periodic_event(1)
    heartbeat_check_period = mavutil.periodic_event(0.33)

    rl = rline("MAV> ")
    if opts.setup:
        rl.set_prompt("")

    if opts.aircraft is not None:
        start_script = os.path.join(opts.aircraft, "mavinit.scr")
        if os.path.exists(start_script):
            run_script(start_script)

    # run main loop as a thread
    mpstate.status.thread = threading.Thread(target=main_loop)
    mpstate.status.thread.daemon = True
    mpstate.status.thread.start()

    # use main program for input. This ensures the terminal cleans
    # up on exit
    try:
        input_loop()
    except KeyboardInterrupt:
        print("exiting")
        mpstate.status.exit = True
        sys.exit(1)
