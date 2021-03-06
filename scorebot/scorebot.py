#!/usr/bin/env python

import argparse
import base64
import copy
import datetime
import json
import logging
import os
import random
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib
from multiprocessing import Array, Event, Process, Value

from settings import DB_HOST, DB_SECRET

SUDO = '/usr/bin/sudo'

# Sandbox python while running submitted scripts
# For now use the default python
SANDBOX_PYTHON_PATH = '/usr/bin/python'
SANDBOX_RUNNER_PATH = os.path.join(os.getcwd(), 'sandbox_run.py')

EXPLOIT_RUNNER = os.path.join(os.getcwd(), 'invoke_container.py')

TEAM_LOCAL_USER_FORMAT = 'ctf-sandbox-team-%d'


LOG_PATH = '/tmp/scheduler.log'
STATUS_PATH = '/tmp/scheduler.status.json'
G_V = False

SIGMA_FACTOR = 10.0/100  # (10 percent of the script call interval)
SCRIPT_TIMEOUT = 300
SETUP_SLEEP = 5  # seconds
STATE_CHECK_INTERVAL = 2
STATE_EXPIRE_MIN = 5  # seconds
SET_GET_FLAG_TIME_DIFFERENCE_MIN = 3.0

SERVICE_DOWN = 0
SERVICE_NONFUNC = 1
SERVICE_UP = 2

ERROR_SCRIPT_EXECUTION = (0xA003, "Script execution failed.")
ERROR_WRONG_FLAG = (0x1000, "Error wrong flag.")
ERROR_DB = (0x9000, "DB error.")
ERROR_SCRIPT_KILLED = (0xB000, "Script was killed by the scheduler.")

try:
    logdir = os.path.dirname(LOG_PATH)
    if not os.path.exists(logdir):
        os.makedirs(logdir)
except Exception as e:
    print e

logging.basicConfig(filename=LOG_PATH, level=logging.WARNING,
                    format='%(asctime)s, %(name)s, %(levelname)s, %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S')

TEST = False
TEST_ATTACK_USER = 'dhilung'
TEST_GETSTATE = {"state_id": 1,
                 "services": [{"service_id": 1, "service_name": 'srv1', "is_up": 1,
                               "port": 8000}],
                 "scripts": [{"script_id": 1, "should_run": 1, "type": "exploit",
                              "service_id": 1},
                             {"script_id": 2, "should_run": 1, "type": "setflag",
                              "service_id": 1},
                             {"script_id": 3, "should_run": 1, "type": "getflag",
                              "service_id": 1},
                             {"script_id": 4, "should_run": 1, "type": "benign",
                              "service_id": 1},
                             ],
                 "run_scripts": [{"team_id": 1, "run_list": [1, 2, 3, 4, 2, 3]}],
                 "state_expire": STATE_EXPIRE_MIN+10,
                 }
TMP_SCRIPT_PATH = '/tmp/scripts'


class DBClient:
    def __init__(self, host=DB_HOST, pwd=DB_SECRET):
        self.host = host
        self.pwd = pwd
        self.log = logging.getLogger('__DBClient__')

    def get_state(self):
        r = ''
        if TEST:
            return TEST_GETSTATE
        try:
            url = 'http://%s/state?secret=%s' % (self.host, self.pwd)
            r = urllib.urlopen(url).read()
            ret = json.loads(r)
            self.log.info('Game state returned: %s' % (str(ret)))
            return ret
        except Exception as e:
            self.log.error(ERROR_DB[1]+' get_state(): Exception: '+str(e)+'. Response: '+r)
            self.log.error(traceback.format_exc())

    def get_teams(self):
        try:
            r = None
            url = 'http://%s/teams?secret=%s' % (self.host, self.pwd)
            r = urllib.urlopen(url).read()
            ret = json.loads(r)
            self.log.info('Teams returned: %s' % (str(ret)))
            return ret
        except Exception as e:
            self.log.error(ERROR_DB[1] + ' get_teams(): Exception: ' + str(e) + '. Response: ' + r)
            self.log.error(traceback.format_exc())

    def get_services(self):
        try:
            r = None
            url = 'http://%s/services?secret=%s' % (self.host, self.pwd)
            r = urllib.urlopen(url).read()
            ret = json.loads(r)
            self.log.info('Services returned: %s' % (str(ret)))
            return ret
        except Exception as e:
            self.log.error(ERROR_DB[1] + ' get_services(): Exception: ' + str(e) +
                           '. Response: ' + r)
            self.log.error(traceback.format_exc())

    def get_flag(self, team_id, service_id):
        if TEST:
            return 'this_is_a_flag' + str(service_id)

        try:
            # /newflag/<teamid>/<serviceid>?secret=<secret>
            url = 'http://%s/newflag/%d/%d?secret=%s' % \
                  (self.host, int(team_id), int(service_id), self.pwd)
            ret = json.loads(urllib.urlopen(url).read())['flag']
            self.log.info('get_flag(team_id=%d, service_id=%d)=%s' %
                          (team_id, service_id, ret))
            return ret

        except Exception as e:
            self.log.error(ERROR_DB[1] + ' get_flag(): Exception: ' + str(e))
            self.log.error(traceback.format_exc())
            return None

    def set_cookie(self, flag, flag_id, cookie):
        if TEST:
            return True

        try:
            # /setcookieandflagid/<flag>/?flag_id=<flagid>&cookie=<cookie>&secret=<secret>
            param = urllib.urlencode({'secret': self.pwd, 'flag_id': flag_id,
                                      'cookie': cookie})
            url = 'http://%s/setcookieandflagid/%s?%s' % (self.host, flag, param)
            ret = json.loads(urllib.urlopen(url).read())
            self.log.info('set_cookie(flag=%s, flag_id=%s, cookie=%s)=%s' %
                          (flag, str(flag_id), str(cookie), str(ret)))
            return ret
        except Exception as e:
            self.log.error(ERROR_DB[1] + ' set_cookies(). Exception: ' + str(e))
            self.log.error(traceback.format_exc())
            return None

    def get_current_flag(self, team_id, service_id):
        if TEST:
            return 'this_is_a_flag', 23, 'this_is_a_cookie'

        try:
            # /getlatestflagandcookie/<teamid>/<serviceid>?secret=<secret>
            param = urllib.urlencode({'secret': self.pwd})
            url = 'http://%s/getlatestflagandcookie/%d/%d?%s' % \
                  (self.host, int(team_id), int(service_id), param)
            o = urllib.urlopen(url).read()
            ret = json.loads(o)
            if ret is None:
                self.log.info('No flag found for service %d.' % int(service_id))
                return None, None, None

            self.log.info('%s:%s' % (url, str(ret)))
            return ret['flag'], ret['flag_id'], ret['cookie']
        except Exception as e:
            self.log.error(ERROR_DB[1] + " get_current_flag(%d,%d): Exception: %s" %
                           (team_id, service_id, str(e)))
            self.log.error(traceback.format_exc())
            return None, None, None

    def push_result(self, team_id, script_id, result):
        if TEST:
            self.log.info('TEST: push_result:' + str(result))
            return None

        try:
            self.log.info('Script %d returned: %s' %
                          (script_id, str((result['ERROR'], result['ERROR_MSG']))))
            param = {'secret': self.pwd,
                     'team_id': team_id,
                     'error': result['ERROR'],
                     'error_msg': result['ERROR_MSG']}

            url = 'http://%s/ranscript/%d?%s' % (self.host, script_id, urllib.urlencode(param))
            ret = json.loads(urllib.urlopen(url).read())
            self.log.info('push_result returned %s.' % (json.dumps(ret)))
            return ret
        except Exception as e:
            self.log.error((ERROR_DB[1] + "push_result(team_id:%d,script_id:%d," +
                            "result:%s):Exception:%s ") %
                           (team_id, script_id, str(result), str(e)))
            self.log.error(traceback.format_exc())
            return None

    def get_script(self, script_id):
        if TEST:
            print 'get_script() Test'
            return 1, 1, open('test/bundle.tgz', 'r').read()
        try:
            sid = int(script_id)
            url = 'http://%s/script/%d?secret=%s' % (self.host, sid, self.pwd)
            ret = json.loads(urllib.urlopen(url).read())
            src = base64.b64decode(ret['payload'])
            return ret['service_id'], ret['name'], src
        except Exception as e:
            msg = ERROR_DB[1] + " get_script(%d): Excpetion: %s. DB response: %s" % \
                (script_id, str(e), ret)
            self.log.error(msg)
            self.log.error(traceback.format_exc())

    def update_status_db(self, team_id, service_id, status, reason):
        # Make a request to /setservicestate/<teamid>/<serviceid>
        # ?secret=<secret>&status=(2|1|0)&reason=<why service is up (2),
        # #up but non functional (1), or down (0)(you choose)>
        if TEST:
            return {'scripts': [{'id': 3, 'type': 'benign', 'service_id': 1,
                    'call_interval': 5, 'call_interval_sd': 3}]}
        try:
            param = urllib.urlencode({'secret': self.pwd, 'status': status,
                                      'reason': reason})
            url = 'http://%s/setservicestate/%d/%d?%s' % \
                  (self.host, team_id, service_id, param)
            ret = json.loads(urllib.urlopen(url).read())
            self.log.info('update_status_db(%d, %d, %d, %s) returned %s.' %
                          (team_id, service_id, status, reason, str(ret)))
            return ret
        except Exception as e:
            self.log.error((ERROR_DB[1] + " update_status_db(%s): Exception: %s") %
                           (str((team_id, service_id, status, reason)), str(e)))
            self.log.error(traceback.format_exc())
            return None


def handler(signum, frame):
    # print 'Signal handler called with signal', signum, ' pid ',os.getpid()
    exit(0)


class ScriptExec(Process):
    def __init__(self, slocks, team_id, sandbox_user, script_id, service_id,
                 service_name, timeout, script_type, script_path, ip, port, delay=0):
        self.delay = delay
        self.result = {'ERROR': 0, 'ERROR_MSG': 'Init'}
        # thread-safe replica of self.result, which is readable from parent process.
        self.out_status = (Value('i', 0), Array('c', '\0'*1024))
        self.team_id = int(team_id)
        self.script_id = int(script_id)
        self.service_id = int(service_id)
        self.service_name = service_name
        self.timeout = timeout
        self.script_type = script_type
        self.script_path = script_path
        self.ip = ip
        self.port = int(port)
        self.log = logging.getLogger('__ScriptExec__')
        self.db = DBClient()
        self.stop = False
        self.args = None
        self.flags = {}
        self.user = sandbox_user
        self.slocks = slocks

        super(ScriptExec, self).__init__()
        self.log.info('ScriptExec Init')

    def update_current_flag(self):
            self.log.info('Getting current flag info for service %d' % self.service_id)
            flag, flag_id, cookie = self.db.get_current_flag(self.team_id, self.service_id)
            self.flags['flag'] = flag
            self.flags['flag_id'] = flag_id
            self.flags['cookie'] = cookie
            self.log.info('Flags received: ' + str((flag, flag_id, cookie)))

    def get_args(self):
        try:
            if self.script_type == 'benign':
                # self.update_current_flag()
                self.flags['flag_id'] = ''
                self.flags['cookie'] = ''
                self.args = [SANDBOX_PYTHON_PATH, SANDBOX_RUNNER_PATH,
                             str(self.script_id), str(self.timeout),
                             str(self.script_type), str(self.script_path),
                             str(self.ip), str(self.port),
                             str(self.flags['flag_id']), str(self.flags['cookie'])]

                # self.args = ['sudo','-u',self.user,
                #         SANDBOX_PYTHON_PATH,SANDBOX_RUNNER_PATH,str(self.script_id),
                #         str(self.timeout),str(self.script_type),str(self.script_path),str(self.ip),str(self.port)]

            elif self.script_type == 'exploit':
                self.update_current_flag()

                if self.flags['flag'] is None:
                    raise Exception('No flag is set')
                if self.flags['flag_id'] is None:
                    raise Exception('No flag_id is set')

                self.args = [SANDBOX_PYTHON_PATH, SANDBOX_RUNNER_PATH,
                             str(self.script_id), str(self.timeout),
                             str(self.script_type), str(self.script_path),
                             str(self.ip), str(self.port),
                             str(self.flags['flag_id'])]
            elif self.script_type == 'setflag':
                # generate flag
                self.log.info('Getting new flag for service %d' % self.service_id)
                self.flags['flag'] = self.db.get_flag(self.team_id, self.service_id)

                self.args = [SANDBOX_PYTHON_PATH, SANDBOX_RUNNER_PATH,
                             str(self.script_id), str(self.timeout),
                             str(self.script_type), str(self.script_path),
                             str(self.ip), str(self.port), str(self.flags['flag'])]

            elif self.script_type == 'getflag':
                self.update_current_flag()

                if self.flags['flag'] is None:
                    raise Exception('No flag is set')
                if self.flags['flag_id'] is None:
                    raise Exception('No flag_id is set')

                self.args = [SANDBOX_PYTHON_PATH, SANDBOX_RUNNER_PATH,
                             str(self.script_id), str(self.timeout),
                             str(self.script_type), str(self.script_path),
                             str(self.ip), str(self.port),
                             str(self.flags['flag_id']), str(self.flags['cookie'])]
        except Exception as e:
            msg = ERROR_SCRIPT_EXECUTION[1] + " Exception at ScriptExec.get_args(): " \
                + str(e) + ' | Script Object:' + str(self.get_status())
            self.log.error(msg)
            self.log.error(traceback.format_exc())
            self.result = {'ERROR': ERROR_SCRIPT_EXECUTION[0],
                           'ERROR_MSG': msg}
            self.args = None

        return self.args

    def push_result(self, result):
        try:
            if result['ERROR'] == 0:
                if self.script_type == 'setflag':
                    self.db.set_cookie(self.flags['flag'], result['FLAG_ID'], result['TOKEN'])
                elif self.script_type == 'getflag':
                    if self.flags['flag'] is not None:
                        if self.flags['flag'] != result['FLAG']:
                            # wrong flag
                            error_msg = ('Getflag(script_id:%d) received WRONG ' +
                                         'FLAG! true_flag:%s returned_flag: %s') % \
                                (self.script_id, str(self.flags['flag']),
                                 str(result['FLAG']))
                            result['ERROR'] = ERROR_WRONG_FLAG[0]
                            result['ERROR_MSG'] = ERROR_WRONG_FLAG[1] + error_msg

                elif self.script_type == 'exploit':
                    if self.flags['flag'] is not None:
                        if self.flags['flag'] != result['FLAG']:
                            # wrong flag
                            error_msg = ('Exploit(script_id:%d) received a WRONG ' +
                                         'FLAG! true_flag:%s returned_flag:%s') % \
                                        (self.script_id, str(self.flags['flag']),
                                         str(result['FLAG']))
                            result['ERROR'] = ERROR_WRONG_FLAG[0]
                            result['ERROR_MSG'] = ERROR_WRONG_FLAG[1]+error_msg
        except Exception as e:
            self.log.error('Exception:' + str(e))
            self.log.error(traceback.format_exc())
            result = {'ERROR': ERROR_SCRIPT_EXECUTION[0],
                      'ERROR_MSG': ERROR_SCRIPT_EXECUTION[1] + " Exception at " +
                      "ScriptExec.push_result(): " + str(e) + ' | Script Object:' +
                      str(self.get_status())}

        self.result = result
        self.out_status[0].value = result['ERROR']
        self.out_status[1].value = result['ERROR_MSG'][:1023]
        if self.result['ERROR'] != 0:
            self.log.error(result['ERROR_MSG'])
        else:
            self.log.info(result['ERROR_MSG'])
        self.db.push_result(self.team_id, self.script_id, result)

    def get_status(self):
        s = {'pid': self.pid, 'exitcode': self.exitcode, 'error': int(self.out_status[0].value),
             'error_msg': str(self.out_status[1].value), 'team_id': self.team_id,
             'script_id': self.script_id, 'script_type': self.script_type,
             'dest_ip': self.ip, 'dest_port': self.port, 'delay': self.delay,
             'service': self.service_name}
        return s

    def run(self):
        signal.signal(signal.SIGTERM, handler)
        if self.script_type == 'setflag':
            for lock in self.slocks:
                lock.clear()

        self.log.info(('Running script_id:%d, type:%s, dest_ip:%s, dest_port:%d,' +
                      'delay:%.2f secs.') % (self.script_id, self.script_type,
                                             self.ip, self.port, self.delay))
        time.sleep(self.delay)
        if self.script_type == 'setflag':
            for lock in self.slocks:
                lock.clear()
        elif self.script_type != 'benign':
            self.log.info('Waiting on exploit locks. ' + str(self.get_status()))
            for lock in self.slocks:
                lock.wait()
        if TEST:
            self.args = self.get_args()
            self.log.info('Starting sandbox with args: ' + str(self.args))
            self.result = {'ERROR': 0, 'ERROR_MSG': 'Success', 'FLAG':
                           'this_is_a_flag', 'FLAG_ID': 23, 'TOKEN':
                           'this_is_a_cookies'}
            self.push_result(self.result)
            return sys.exit(0)

        while True:
            if self.stop:
                return sys.exit(0)

            output, err = ('', '')
            try:
                self.args = self.get_args()
                if self.args:
                    self.log.info('Starting sandbox with args: ' + str(self.args))
                    # print str(self.args)
                    # return
                    self.process = subprocess.Popen(self.args,
                                                    stdout=subprocess.PIPE,
                                                    stderr=subprocess.PIPE)
                    output, err = self.process.communicate()
                    self.log.info('Sandbox run returned: ' + str(self.get_status()))
                    if output is None:
                        raise Exception('No output from sandbox run.')
                    self.result = json.loads(output.split('\n')[-2])
                    if self.result['ERROR'] != 0:
                        self.result["ERROR_MSG"] = self.result["ERROR_MSG"] + \
                            ' | Script Object:' + str(self.get_status()) + \
                            " | Script output:" + output + err
            except Exception as e:
                if err is None:
                    err = ''
                if output is None:
                    output = ''
                self.result = {'ERROR': ERROR_SCRIPT_EXECUTION[0],
                               'ERROR_MSG': ERROR_SCRIPT_EXECUTION[1] +
                               " Exception at ScriptExec.run(): " + str(e) +
                               ' | Script Object:' + str(self.get_status()) +
                               " | Script output:" + output + err}

            # Push result before releasing lock else exploit container won't find
            # flag_id and not execute correctly
            self.push_result(self.result)

            if self.script_type == 'setflag':
                for lock in self.slocks:
                    lock.set()

                self.log.info('Setting setflag event. '+str(self.get_status()))

            return sys.exit(self.result['ERROR'])


class ExploitContainerExec(Process):
    def __init__(self, setflag_lock, exploit_lock, host, namespace, image,
                 attacker_id, attacker_name, service_id, service_name, delay,
                 interval):
        self.attacker_name = attacker_name
        self.attacker_team_id = attacker_id
        self.delay = delay
        self.exploit_lock = exploit_lock
        self.flag_ids = {}
        self.container_host = host
        self.image = image
        self.interval = interval
        self.is_my_service_up = False
        self.log = logging.getLogger('__ExploitContainerExec__')
        self.namespace = namespace
        self.service_id = service_id
        self.service_name = service_name
        self.setflag_lock = setflag_lock
        self.script_type = "exploit_container"

        super(ExploitContainerExec, self).__init__()
        self.log.info("Acquiring exploit lock in init. Service: %s, team: %s" %
                      (self.service_name, self.attacker_name))
        self.exploit_lock.clear()
        return

    def get_status(self):
        s = {'pid': self.pid, 'exitcode': self.exitcode, 'image': self.image,
             'namespace': self.namespace, 'attacker': self.attacker_team_id,
             'delay': self.delay}
        return s

    def get_targets(self):
        """
        Any team which had a flag stored successfully in last round is a target. We
        find this based on latest flag IDs rather than service states since states
        aren't updated in time.
        """

        param = urllib.urlencode({'secret': DB_SECRET})
        url = 'http://%s/getlatestflagids?%s' % (DB_HOST, param)
        flag_ids = json.loads(urllib.urlopen(url).read())["flag_ids"]
        up_team_ids = []
        if str(self.service_id) not in flag_ids[str(self.attacker_team_id)] or \
           flag_ids[str(self.attacker_team_id)][str(self.service_id)] is None:
            self.is_my_service_up = False
            return []

        for team_id in flag_ids:
            if int(team_id) == self.attacker_team_id:
                continue

            if str(self.service_id) in flag_ids[team_id] and \
               flag_ids[team_id][str(self.service_id)] is not None:
                self.log.info("Team %s is a target for attacker %s, service %s" %
                              (team_id, self.attacker_name, self.service_name))
                up_team_ids.append(int(team_id))
                self.flag_ids[int(team_id)] = flag_ids[team_id][str(self.service_id)]
            else:
                self.log.info(("Team %s not a target for attacker %s, service %s." +
                               "No flag ID not found") %
                              (team_id, self.attacker_name, self.service_name))

        targets = []
        url = 'http://%s/state?%s' % (DB_HOST, param)
        game_state = json.loads(urllib.urlopen(url).read())
        for team_id in up_team_ids:
            for location in game_state['locations']:
                if location["team_id"] == team_id and \
                   location["service_id"] == self.service_id:
                    target = {"team_id": team_id,
                              "ip": location['host_ip'],
                              "port": location["host_port"],
                              "flag_id": self.flag_ids[team_id]}
                    targets.append(target)

        return targets

    def run(self):
        self.log.info("Sleeping for %d seconds" % (self.delay))
        time.sleep(self.delay)
        self.log.info("Waiting on %d setflag locks. Service %s, attacker %s"
                      % (len(self.setflag_lock), self.service_name,
                         self.attacker_name))
        for lock in self.setflag_lock:
            lock.wait()

        self.log.info("Setflag locks released. Fetching current targets.")
        self.targets = self.get_targets()
        self.log.info("Service: %s, attacker: %s, targets: %s." %
                      (self.service_name, self.attacker_name, json.dumps(self.targets)))
        if self.targets != []:
            args = [SANDBOX_PYTHON_PATH, EXPLOIT_RUNNER, self.container_host,
                    self.namespace, self.image, str(self.attacker_team_id),
                    str(self.service_id), json.dumps(self.targets),
                    str(self.interval)]
            self.log.info("Running exploit with args %s" % (args))
            self.process = subprocess.Popen(args, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
            stdout, stderr = self.process.communicate()
            exit_code = self.process.returncode
            if exit_code != 0:
                msg = os.linesep + "stdout: " + stdout + os.linesep + "stderr: " + \
                    stderr
                self.log.error("Container runner returned %d%s" % (exit_code, msg))
            else:
                self.log.info("Container runner returned %d" % (exit_code))
        elif not self.is_my_service_up:
            self.log.warning(("Not running exploits of service %s, attacker %s" +
                              "since corresponding service of attacker is not up.") %
                             (self.service_name, self.attacker_name))
        else:
            self.log.error("No targets found for service %s, attacker %s" %
                           (self.service_name, self.attacker_name))

        self.log.info("Releasing exploit lock. service %s, attacker %s" %
                      (self.service_name, self.attacker_name))
        self.exploit_lock.set()
        return


class Scheduler:
    def __init__(self, status_path=STATUS_PATH):
        global G_V
        # state_id should be 0 at start because 0 is tick value returned before
        # gamebot deploys all containers(which takes time)
        self.state_id = 0
        self.state_changed = False
        self.services = {}
        self.service_locations = {}
        self.exploit_containers = {}
        self.scripts = {}
        self.run_list = {}
        self.teams = {}
        self.state_expire = STATE_EXPIRE_MIN
        self.status_path = status_path
        self.process_list = []
        self.db = DBClient()
        self.setflag_locks = {}
        self.exploit_locks = {}
        self.setflag_delays = {}
        self.getflag_delays = {}
        self.benign_delays = {}

        self.status = {'state_id': 'INIT', 'last_error': None, 'script_err': None,
                       'script_ok': 0, 'script_fail': 0, 'script_tot': 0}
        self.log = logging.getLogger('__Scheduler__')
        if G_V:
            self.log.addHandler(logging.StreamHandler().setLevel(logging.INFO))

        self.teams = self.get_team_list()
        self.services = self.get_services()

        # seed
        random.seed(time.time())
        self.log.info('#'*80)
        self.log.info('#'*80)
        self.log.info('Init')

    def __del__(self):
        self.status['state_id'] = 'EXIT'
        self.update_status()

    def get_team_list(self):
        if TEST:
            return {20: {"team_id": 20, "ip": "127.0.0.20"},
                    14: {"team_id": 14, "ip": "127.0.0.14"}}

        team_list = self.db.get_teams()
        ret = {}
        for team in team_list:
            ret[team["team_id"]] = team

        return ret

    def get_services(self):
        service_list = self.db.get_services()
        ret = {}
        for service in service_list:
            ret[service['service_id']] = service

        return ret

    def update_status(self):
        self.status['teams'] = self.teams.keys()
        self.status['state_expire'] = self.state_expire
        o = open(self.status_path, 'w')
        o.write(json.dumps(self.status))
        o.close()

    def update_state(self, state=None):
        self.log.info('Calling update_state.')
        try:
            if state is None:
                state = self.db.get_state()

            for team_id in self.teams:
                self.service_locations[team_id] = {}
                for service_id in self.services:
                    self.service_locations[team_id][service_id] = None

            for location in state['locations']:
                assert location['service_id'] in self.services
                assert location['team_id'] in self.teams
                self.service_locations[location['team_id']][location['service_id']] = \
                    (location['host_ip'], location['host_port'])

            self.state_expire = state['state_expire']
            if self.state_expire < STATE_EXPIRE_MIN:
                self.state_expire = STATE_EXPIRE_MIN

            for s in state['scripts']:
                self.scripts[int(s['script_id'])] = s

            if state['run_scripts'] is not None:
                self.run_list = state['run_scripts']

            self.status['script_tot'] = sum([len(alist) for alist in
                                             self.run_list.values()])

            for container in state['exploit_containers']:
                self.exploit_containers[container["id"]] = container
                self.exploit_containers[container["id"]]["host"] = \
                    state["exploit_containers_host"]

            if self.state_id == state['state_id']:
                self.state_changed = False
                self.log.info('No change in state.')
#                print '\rNO STATE CHANGE',
#                sys.stdout.flush()
            else:
                self.state_changed = True
                msg = 'State changed from %s to %s. %s' % \
                      (str(self.state_id), str(state['state_id']),
                       str((self.services, self.scripts)))

                self.log.info(msg)
                print msg
                self.state_id = state['state_id']
                self.status['state_id'] = self.state_id
                self.compute_delays()
                self.kill_process()
        except Exception as e:
            msg = 'update_state:Exception:'+str(e)
            self.log.error(msg)
            self.log.error(traceback.format_exc())
            self.status['last_error'] = str(datetime.datetime.now()) + ' : ' + msg
        return

    def get_sandbox_user_name(self, team_id):
        if TEST:
            return TEST_ATTACK_USER
        return TEAM_LOCAL_USER_FORMAT % team_id

    def get_script_repo_path(self, sid):
        sdir = os.path.join(TMP_SCRIPT_PATH, 'repo/%d' % (int(sid)))
        if not os.path.isdir(os.path.dirname(sdir)):
            os.makedirs(os.path.dirname(sdir))
        return sdir

    def update_script_repo(self, script_id):
        script_id = int(script_id)

        try:
            fn = self.get_script_repo_path(script_id)
            service_id, name, src = self.db.get_script(script_id)
            self.scripts[script_id]['service_id'] = service_id
            self.scripts[script_id]['name'] = service_id

            f = open(fn, 'w')
            f.write(src)
            f.close()

            name = self.scripts[script_id]['name']
            self.log.info('Script_id %d updated. (name:%s, service_id:%d) Length %d.'
                          % (script_id, name, service_id, len(src)))
            return fn
        except Exception as e:
            msg = 'update_script_repo:Exception:' + str(e)
            self.log.error(msg)
            self.log.error(traceback.format_exc())
            self.status['last_error'] = str(datetime.datetime.now()) + ' : ' + msg
            return None

    def get_script_path(self, tid, sid, is_bundle=False):
        sdir = os.path.join(TMP_SCRIPT_PATH, '%d/%d' % (int(tid), int(sid)))
        if not os.path.isdir(sdir):
            os.makedirs(sdir)
        if is_bundle:
            fn = os.path.join(sdir, '%d.tgz' % (int(sid)))
        else:
            fn = os.path.join(sdir, '%d.py' % (int(sid)))
        return fn

    def update_script(self, team_id, script_id, is_bundle=False):
        script_id = int(script_id)

        try:
            fn = self.get_script_path(team_id, script_id, is_bundle)
            shutil.copyfile(self.get_script_repo_path(script_id), fn)

            if is_bundle:
                # extract
                self.log.info('Extracting '+fn)
                cmd = ['tar', '-C', os.path.dirname(fn), '-xzf', fn]
                subprocess.call(cmd)
                fn = os.path.join(os.path.dirname(fn), 'exploit.py')
            return fn
        except Exception as e:
            msg = 'update_script:Exception:' + str(e)
            self.log.error(msg)
            self.log.error(traceback.format_exc())
            self.status['last_error'] = str(datetime.datetime.now()) + ' : ' + msg
            return None

    def update_all_scripts(self):
        slist = set()
        for etype in self.run_list:
            for entry in self.run_list[etype]:
                if entry["type"] == 'script':
                    slist.add(entry["id"])

        for sid in slist:
            self.update_script_repo(sid)

    def compute_delays(self):
        script_counts = {}
        for service_id in self.services:
            script_counts[service_id] = {}
            self.setflag_delays[service_id] = {}
            self.getflag_delays[service_id] = {}
            self.benign_delays[service_id] = {}
            for team_id in self.teams:
                script_counts[service_id][team_id] = 0
                self.setflag_delays[service_id][team_id] = None
                self.getflag_delays[service_id][team_id] = None
                self.benign_delays[service_id][team_id] = None

        for entry in [item for alist in self.run_list.values() for item in alist]:
            if entry["type"] == "script":
                sid = entry["id"]
                service_id = self.scripts[sid]["service_id"]
                for team_id in self.teams:
                    entry["exec_queue_position"] = script_counts[service_id][team_id]
                    script_counts[service_id][team_id] += 1
            elif entry["type"] == "exploit_container":
                container_id = entry["id"]
                container = self.exploit_containers[container_id]
                service_id = container["service_id"]
                for team_id in self.teams:
                    # Exploit container won't run against it's owner
                    if team_id == container['team_id']:
                        continue

                    entry["exec_queue_position"] = script_counts[service_id][team_id]
                    script_counts[service_id][team_id] += 1

        for entry in self.run_list['setflag']:
            sid = entry["id"]
            service_id = self.scripts[sid]["service_id"]
            for team_id in self.teams:
                interval = float(self.state_expire-STATE_EXPIRE_MIN) / \
                    (script_counts[service_id][team_id] * 2)
                delay = abs((entry["exec_queue_position"] + 1) * interval -
                            random.gauss(interval, (SIGMA_FACTOR * interval)))
                self.setflag_delays[service_id][team_id] = delay

        for entry in self.run_list['getflag']:
            sid = entry["id"]
            service_id = self.scripts[sid]["service_id"]
            for team_id in self.teams:
                interval = float(self.state_expire-STATE_EXPIRE_MIN) / \
                    (script_counts[service_id][team_id] * 2)
                delay = abs((entry["exec_queue_position"] + 1) * interval -
                            random.gauss(interval, (SIGMA_FACTOR * interval)))
                last_delay = self.setflag_delays[service_id][team_id]
                if (delay - last_delay) < SET_GET_FLAG_TIME_DIFFERENCE_MIN:
                    self.log.info(('delay (%.2f) - last_delay (%.2f) < ' +
                                   'SET_GET_FLAG_TIME_DIFFERENCE_MIN (%.2f)') %
                                  (delay, last_delay,
                                   SET_GET_FLAG_TIME_DIFFERENCE_MIN))
                    delay = last_delay + SET_GET_FLAG_TIME_DIFFERENCE_MIN
                    if delay > self.state_expire:
                        delay = self.state_expire - (2 * STATE_EXPIRE_MIN)

                self.getflag_delays[service_id][team_id] = delay

        for entry in self.run_list['benign-and-exploits']:
            if entry["type"] == "exploit_container":
                container_id = entry["id"]
                container = self.exploit_containers[container_id]
                service_id = container["service_id"]
                delay = 0
                avg_interval_size = 0
                for team_id in self.teams:
                    interval = float(self.state_expire-STATE_EXPIRE_MIN) / \
                        (script_counts[service_id][team_id] * 2)
                    avg_interval_size += abs(interval)
                    delay += abs((entry["exec_queue_position"] + 1) * interval)

                entry["delay"] = abs(delay / len(self.teams.keys()))
                entry["interval"] = abs(4 * avg_interval_size / len(self.teams.keys()))
            elif entry["type"] == "script":
                sid = entry["id"]
                service_id = self.scripts[sid]["service_id"]
                for team_id in self.teams:
                    interval = float(self.state_expire-STATE_EXPIRE_MIN) / \
                        (script_counts[service_id][team_id] * 2)
                    delay = abs((entry["exec_queue_position"] + 1) * interval -
                                random.gauss(interval, (SIGMA_FACTOR * interval)))
                    self.benign_delays[service_id][team_id] = delay
        return

    def runscripts(self):
        self.run_setflag_scripts()
        self.run_benign_scripts_and_exploits()
        self.run_getflag_scripts()
        return

    def run_setflag_scripts(self):
        for entry in self.run_list['setflag']:
            sid = entry["id"]
            s = self.scripts[sid]
            service_id = s['service_id']
            for team_id in self.teams:
                setflag_lock = Event()
                setflag_lock.set()
                if service_id in self.setflag_locks:
                    self.setflag_locks[service_id][team_id] = setflag_lock
                else:
                    self.setflag_locks[service_id] = {team_id: setflag_lock}
                p = self.update_script(team_id, sid, s['is_bundle'])
                if p is None:
                    continue

                ip, port = self.service_locations[team_id][service_id]
                locks = [setflag_lock]
                self.runscript(locks, team_id, sid, service_id, SCRIPT_TIMEOUT,
                               s['type'], p, ip, port,
                               self.setflag_delays[service_id][team_id])

        return

    def run_getflag_scripts(self):
        for entry in self.run_list['getflag']:
            sid = entry["id"]
            s = self.scripts[sid]
            for team_id in self.teams:
                p = self.update_script(team_id, sid, s['is_bundle'])
                if p is None:
                    continue

                ip, port = self.service_locations[team_id][s['service_id']]
                locks = [self.setflag_locks[s['service_id']][team_id]]
                for other_team_id in self.teams:
                    if team_id == other_team_id:
                        continue

                    if s['service_id'] in self.exploit_locks and \
                       other_team_id in self.exploit_locks[s['service_id']]:
                        locks.append(self.exploit_locks[s['service_id']][other_team_id])

                self.runscript(locks, team_id, sid, s['service_id'], SCRIPT_TIMEOUT,
                               s['type'], p, ip, port,
                               self.getflag_delays[s['service_id']][team_id])

        return

    def run_benign_scripts_and_exploits(self):
        for entry in self.run_list["benign-and-exploits"]:
            if entry["type"] == "script":
                sid = entry["id"]
                s = self.scripts[sid]
                for team_id in self.teams:
                    p = self.update_script(team_id, sid, s['is_bundle'])
                    if p is None:
                        continue
                    ip, port = self.service_locations[team_id][s['service_id']]
                    self.runscript([], team_id, sid, s['service_id'],
                                   SCRIPT_TIMEOUT, s['type'], p, ip, port,
                                   self.benign_delays[s['service_id']][team_id])
            elif entry["type"] == "exploit_container":
                container_id = entry["id"]
                container = self.exploit_containers[container_id]
                attacker_id = container["team_id"]
                service_id = container["service_id"]
                exploit_lock = Event()
                exploit_lock.set()
                if service_id in self.exploit_locks:
                    self.exploit_locks[service_id][attacker_id] = exploit_lock
                else:
                    self.exploit_locks[service_id] = {attacker_id: exploit_lock}

                setflag_locks = []
                for team_id in self.teams:
                    if attacker_id == team_id:
                        continue

                    setflag_locks.append(self.setflag_locks[service_id][team_id])

                container_host = container["host"]
                image_name = container["image_name"]
                namespace = container["registry_namespace"]
                self.run_exploit_container(setflag_locks, exploit_lock,
                                           container_host, namespace, image_name,
                                           attacker_id, service_id, entry["delay"],
                                           entry["interval"])

        return

    def runscript(self, slocks, team_id, script_id, service_id, timeout, script_type,
                  script_path, ip, port, delay):

        service_name = self.services[service_id]['service_name']
        sandbox_user = self.get_sandbox_user_name(team_id)
        se = ScriptExec(slocks, team_id, sandbox_user, script_id, service_id,
                        service_name, timeout, script_type, script_path, ip, port,
                        delay)
        self.process_list.append(se)
        se.start()

    def run_exploit_container(self, setflag_lock, exploit_lock, container_host,
                              namespace, image, attacker_id, service_id, delay,
                              interval):
        self.log.info("Running exploit container")
        attacker_name = self.teams[attacker_id]["team_name"]
        service_name = self.services[service_id]["service_name"]
        ce = ExploitContainerExec(setflag_lock, exploit_lock, container_host,
                                  namespace, image, attacker_id, attacker_name,
                                  service_id, service_name, delay, interval)
        self.process_list.append(ce)
        ce.start()
        return

    def clean_process(self):
        self.log.info('Cleaning process.')
        dead = []
        alive = []
        alive_status = []
        for p in self.process_list:
            if p.is_alive():
                alive.append(p)
                alive_status.append(p.get_status())
            else:
                dead.append(p)

        for p in dead:
            self.log.info('Script Exited. ' + str(p.get_status()))
            # update service status
            if p.script_type != 'exploit' and p.script_type != 'exploit_container':
                err, msg = p.out_status
                if err.value == 0:
                    self.db.update_status_db(p.team_id, p.service_id, SERVICE_UP, msg.value)
                elif err.value < 0:
                    self.db.update_status_db(p.team_id, p.service_id, SERVICE_DOWN, msg.value)
                elif err.value > 0:
                    self.db.update_status_db(p.team_id, p.service_id, SERVICE_NONFUNC, msg.value)

                if err.value != 0:
                    self.status['script_err'] = '%d:%s' % (err.value, msg.value)
                    self.status['script_fail'] = self.status['script_fail'] + 1
                else:
                    self.status['script_ok'] = self.status['script_ok'] + 1

            self.process_list.remove(p)
            # cleanup
            if p.script_type != "exploit_container":
                e, m = p.out_status
                del e
                del m

            del p

        del dead

        self.status['running_scripts'] = alive_status

    def kill_process(self):
        self.status['script_ok'] = 0
        self.status['script_fail'] = 0
        self.status['script_tot'] = 0
        self.status['script_err'] = None
        self.status['last_error'] = None

        self.setflag_locks = {}

        for p in self.process_list:
            if p.is_alive():
                cmd = 'pkill -9 -P  %d' % (int(p.pid),)
                self.log.info('Killing child processes. $'+cmd)
                os.system(cmd)
                p.terminate()
                p.out_status[0].value = ERROR_SCRIPT_KILLED[0]
                p.out_status[1].value = ERROR_SCRIPT_KILLED[1]

                # ---------------------
                # DO NOT DELETE THE Process OBJECT here !!!!!!!!!!!
                # It is responsible for safely exiting the child process without leaving zombies.

        # Kill all children. Specially, the user submitted scripts need proper cleanup.
        # cmd  = SUDO+' pkill -9 -P %d'%(int(os.getpid()))
        # self.log.info('Killing child processes. $'+cmd)
        # os.system(cmd)
        # self.process_list = []

    def setuprun(self, team):
        for sid, s in self.scripts.iteritems():
            ip = self.service_locations[team['team_id']][s['service_id']][0]
            port = self.service_locations[team['team_id']][s['service_id']][1]
            if s['type'] == 'setflag':
                p = self.update_script(sid)
                self.runscript(self.status, team['team_id'], sid, s['service_id'],
                               SCRIPT_TIMEOUT, s['type'], p, ip, port)

        time.sleep(SETUP_SLEEP)
        for sid, s in self.scripts.iteritems():
            if s['type'] == 'getflag':
                p = self.update_script(sid)
                self.runscript(self.status, team['team_id'], sid, s['service_id'],
                               SCRIPT_TIMEOUT, s['type'], p, ip, port)
        time.sleep(SETUP_SLEEP)

    def schedule_scripts(self):
        self.log.info('schedule_scripts')
        try:
            self.update_all_scripts()
        except Exception as e:
            msg = 'schedule_scripts:Exception:' + str(e)
            self.log.error(msg)
            self.log.error(traceback.format_exc())
            self.status['last_error'] = str(datetime.datetime.now()) + ' : ' + msg

        try:
            self.runscripts()
        except Exception as e:
            msg = 'schedule_scripts:Exception:' + str(e)
            self.log.error(msg)
            self.log.error(traceback.format_exc())
            self.status['last_error'] = str(datetime.datetime.now()) + ' : ' + msg

        return

    def run(self):
        while True:
            try:
                self.update_state()
                if self.state_changed:
                    self.state_changed = False
                    st = time.time()
                    self.schedule_scripts()
                    lt = time.time()
                    self.log.info('Schedule_scripts time:%f' % (lt-st))
                    print '\rSchedule_scripts time:%f               ' % (lt-st)
                # wait here, in case the last process is not alive yet.
                time.sleep(0.5)
                # clean dead process
                self.clean_process()
            except Exception as e:
                msg = 'run:Exception:' + str(e)
                self.log.error(msg)
                self.log.error(traceback.format_exc())
                self.status['last_error'] = str(datetime.datetime.now()) + ' : ' + msg
            finally:
                self.update_status()
                time.sleep(STATE_CHECK_INTERVAL)


def unit_test():
    global TEST
    TEST = True
    VULBOX_IP = '127.0.0.1'
    TEST_PORT = 5342
    s = Scheduler()
    s.update_status()
#    time.sleep(1)
    s.update_state(TEST_GETSTATE)

    print 'teams'
    print s.teams

    print 'services'
    print s.services

    print 'scripts'
    print s.scripts

    print 'run_list'
    print s.run_list

    s.update_status()
    # time.sleep(1)

    tid = 20
    script_id = 2

    s.teams = {1: {"team_id": 1, "ip": '127.0.12.2'}, tid: {"team_id": tid, "ip": '127.0.13.2'}}

    gs = copy.deepcopy(TEST_GETSTATE)
    gs['state_expire'] = 60
    gs['run_scripts'].append({"team_id": tid, "run_list": [1, script_id, 3]})
    s.update_state(gs)
    print ''
    print s.get_rand_delay(s.run_list[tid])

    tid = 1
    sid = 1
    srvid = 1

    # test script run
    print 'trying to run script'
    p = os.path.join(os.getcwd(), 'test', 'scripts', '20', '2', '2.py')
    try:
        s.runscript(Event(), tid, sid, srvid, 300, 'setflag',
                    p, VULBOX_IP, TEST_PORT, 0.01)
    except Exception as e:
        print 'Exception '+str(e)

    s.update_status()
    # time.sleep(1)

    # test script
    print 'testing bundle script run'
    is_bundle = 1
    fn = s.get_script_repo_path(sid)
    print 'Repo path ' + fn
    if os.path.exists(fn):
        print fn + 'exits'
        os.remove(fn)
    s.update_script_repo(sid)
    assert os.path.exists(fn)
    print 'repo updated'

    fn_tgz = s.get_script_path(tid, sid, is_bundle)
    fn_f = os.path.join(os.path.dirname(fn_tgz), 'exploit.py')

    print 'Script  path ' + str((fn_f, fn_tgz))
    if os.path.exists(fn_f):
        print fn_f+'exits'
        os.remove(fn_f)

    if os.path.exists(fn_tgz):
        print fn_tgz+'exits'
        os.remove(fn_tgz)

    print 'updating script'
    fn = s.update_script(tid, sid, is_bundle)
    assert fn == fn_f
    assert os.path.exists(fn_f)
    assert os.path.exists(fn_tgz)

    print 'Bundled script extraction OK'
    print 'Trying to run'
    try:
        s.runscript(Event(), tid, sid, srvid, 300, ' exploit',
                    fn, '127.0.2.1', TEST_PORT, 0.1)
    except Exception as e:
        print 'Exception '+str(e)

    exit(0)

#    s.schedule_scripts()
    c = 0
    while True:
        s.clean_process()
        s.update_status()
        c += 1
        if c == 5:
            s.kill_process()
        time.sleep(1)

    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", default=False, action='store_true')
    ap.add_argument("-t", "--test", default=False, action='store_true')

    args = ap.parse_args()

    if args.verbose:
        global G_V
        G_V = True

    if args.test:
        return unit_test()

    s = Scheduler()

    s.run()

if __name__ == "__main__":
    main()
