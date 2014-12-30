#!/usr/bin/env python

import MySQLdb
import socket
import os.path
import time
import simplejson as json
from sys import exit, stdout, version_info
from optparse import OptionParser
from glob import glob
from os.path import isfile
from os import chdir
from netifaces import interfaces, ifaddresses

### Gotta catch 'em all!
usage = "usage: %prog -t TYPE [-c LIMIT] [-f FLAG]"

parser = OptionParser(usage=usage)
parser.add_option('-t', '--type', type='choice', action='store', dest='type',
                  choices=['ok', 'repl', 'load', 'pinger'],
                  help='Check type. Chose from "ok", "repl", "load", "pinger"')
parser.add_option("-c", "--crit", type="int", dest="crit_limit",
                  help="Critical limit. Default: 100 for 'load' and 600 for 'repl'")
parser.add_option("--conf", dest="config", type="str", default="/etc/mysql_mon.conf", help="Config file. Used in pinger and backup check. Default: /etc/mysql_mon.conf")

(opts, args) = parser.parse_args()

if opts.type == 'load':
        if not opts.crit_limit:
                opts.crit_limit = 100
elif opts.type == 'repl':
        if not opts.crit_limit:
                opts.crit_limit = 600

### Global vars
mysql_init_path = ['mysql-*']
init_lookup_list = ['datadir', 'socket']
conf_lookup_list = ['port', 'bind-address']

### Version check
isEL6 = version_info[0] == 2 and version_info[1] >= 6

### Functions

def output(line):
    if isEL6:
        stdout.write(line + "<br>")
        stdout.flush()
    else:
        print line

def open_file(filename):
    """ We try to open file and copy it into list. """

    if not isfile(filename):
        output("I/O error. There is no '%s'. Check me." % filename)
        raise Exception('NO_FILE')
    try:
        return list(open(filename))
    except IOError, err:
        output("I/O error. Can't open file '%s'. Check me." % filename)
        output("Error %s: %s" % (err.errno, err.strerror))
        raise Exception('IO_ERROR')
    except:
        raise Exception

def load_json(file):
    if not isfile(file):
        print "file %s not found." % file
        exit(1)

    return json.load(open(file))

def print_list(list):
    """ Eh... well... it's printing the list... string by string... """

    for string in list:
        output(string)

def get_all_mysql(mysql_init_path):
    """ Making list of all mysql init scripts """

    inits = []
    chdir('/etc/init.d')
    for path in mysql_init_path:
        if glob(path):
            inits.extend(glob(path))

    return inits

def make_mysql_dict(inits, init_lookup_list, conf_lookup_list):
    """ Make dict of all mysql with datadir and socket args """

    chdir('/etc/init.d')
    mysql_dict = {}

    for init in inits:
        mysql_args_dict = {}

        mysql_args_dict['db'] = init.partition('-')[2]
        file = open_file(init)
        for line in file:
            line = line.split('=')
            if line[0] in init_lookup_list:
                mysql_args_dict[line[0]] = line[1].strip()
        mysql_dict[init] = mysql_args_dict

        conf_file = open_file(mysql_dict[init]['datadir'] + '/my.cnf')
        for line in conf_file:
            line = line.split('=')
            if line[0].strip(' ') in conf_lookup_list:
                mysql_dict[init][line[0].strip(' \n')] = line[1].strip(' \n')

    return mysql_dict

def check_flag(mysql_dict):

    result_dict = {}

    for inst in mysql_dict.keys():
        flag = '/var/tmp/mysql-backup-tmp-%s.sock.flag' % inst
        result_dict[inst] = {'flag': False, 'stale': False, 'file': flag}

        if isfile(flag):
            result_dict[inst]['flag'] = True
            cut_time = time.time()
            bk_mtime = os.path.getmtime(flag)
            if cut_time - bk_mtime > 3600:
                result_dict[inst]['stale'] = True

    return result_dict

def check_ok(mysql_dict, flag_dict):
    """ Check if mysql err file ends with 'OK' """

    hostname = socket.gethostname()
    result = []

    for inst in mysql_dict.keys():
        if flag_dict[inst]['flag'] and flag_dict[inst]['stale']:
            result.append('Stale backup flag found! %s is older than 60 min.' % flag_dict[inst]['file'])
        if not flag_dict[inst]['flag']:
            chdir(mysql_dict[inst]['datadir'])
            file = open_file(hostname + '.err')
            if file[-1].strip() != 'OK':
                result.append('Mysql with datadir "%s" has problems: %s' % (mysql_dict[inst]['datadir'], file[1].strip()))

    if result:
        print_list(result)
        exit(2)

def check_ro(sock):
    db = MySQLdb.connect(unix_socket=sock)
    cur = db.cursor()
    cur.execute("SELECT @@global.read_only;")
    row = cur.fetchone()
    if row[0] == 1:
        return True
    else:
        return False

def check_mysql(mysql_dict, flag_dict, crit, check_repl=False, check_load=False):
    """ Check replica lag or mysql proc count """

    result_critical = []
    result_warning = []

    for inst in mysql_dict.keys():
        if flag_dict[inst]['flag'] and flag_dict[inst]['stale']:
            result_warning.append('Stale backup flag found! %s is older than 60 min.' % flag_dict[inst]['file'])
        if not flag_dict[inst]['flag']:
            try:
                db = MySQLdb.connect(unix_socket=mysql_dict[inst]['socket'])
                cur = db.cursor()

                if check_repl:
                    cur.execute("show slave status")
                    ### We came to agreement what if where is nothing in "show slave status" we assume what it's master
                    if cur.rowcount == 0:
                        exit(0)

                    ### Get values
                    row = cur.fetchone()
                    ### Get keys
                    field_names = [i[0] for i in cur.description]
                    ### Make dict
                    slave_dict = dict(zip(field_names, row))

                    if slave_dict['Slave_IO_Running'] == 'Yes' and slave_dict['Slave_SQL_Running'] == 'Yes':
                        if slave_dict['Seconds_Behind_Master'] >= crit:
                            result_critical.append('Mysql with datadir %s: Seconds_Behind_Master is more than %s - %s' % (mysql_dict[inst]['datadir'], crit, slave_dict['Seconds_Behind_Master']))
                    else:
                        result_critical.append('Mysql with datadir %s: replication is not running' % (mysql_dict[inst]['datadir']))
                elif check_load:
                    ### Works from Mysql 5.1.7
                    proc_count = cur.execute("SELECT * FROM INFORMATION_SCHEMA.PROCESSLIST where COMMAND != 'Sleep'")
                    long_proc = cur.execute("SELECT * FROM INFORMATION_SCHEMA.PROCESSLIST where COMMAND != 'Sleep' and TIME > 5 ")
                    ### if proc count above critical AND 20% of procs runs longer than 5 sec...
                    if int(proc_count) >= crit and int(long_proc) > (crit / 100) * 20:
                            result_critical.append('Mysql with datadir %s: process count is more than %s - %s' % (mysql_dict[inst]['datadir'], crit, cur.rowcount))
            except Exception, err:
                output('Mysql monitoring error. Check me.')
                print err
                exit(1)

    ### Print result
    if result_critical and result_warning:
        print_list(result_critical)
        print_list(result_warning)
        exit(1)
    elif result_critical:
        print_list(result_critical)
        exit(1)
    elif result_warning:
        print_list(result_warning)
        exit(2)

def getip():
    """ Returns list of ips of this server """

    ip_list = []
    for interface in interfaces():
        if 2 in ifaddresses(interface):
            if ifaddresses(interface)[2][0]['addr'].startswith('10.') and not ifaddresses(interface)[2][0]['addr'].startswith('10.34'):
                ip_list.append(ifaddresses(interface)[2][0]['addr'])

    if not ip_list:
        output("Can't get server ip list. Check me.")
        exit(1)
    else:
        return ip_list

def check_pinger(mysql_dict, config_file):
    """ Check if mysql on this host is in pinger database """

    pinger_list = []
    ip_list = getip()

    try:
        config_dict = load_json(config_file)
    except Exception, err:
        if 'IO_ERROR' in err:
            print err
            exit(1)
        else:
            output("Unhandled exeption. Check me.")
            print err
            exit(1)

    if not config_dict['pinger']:
        output('Cant load "pinger" key from config %s') % config_file
    else:
        config = config_dict['pinger']

    ### Connect to db and check remote_stor_ping table for ip:port on this host
    try:
        db = MySQLdb.connect(host=config['host'], user=config['user'], passwd=config['pass'], db=config['db'], connect_timeout=1)
        cur = db.cursor()
        for inst in mysql_dict.values():
            if not check_ro(inst['socket']):
                if inst['bind-address'] == '0.0.0.0':
                    for ip in ip_list:
                        cur.execute("SELECT * FROM remote_stor_ping WHERE connect_str like '%%:%s%%';" % (ip))
                        if int(cur.rowcount) is 0:
                            pinger_list.append('Mysql with ip %s not found in pinger database!' % (ip))
                else:
                    cur.execute("SELECT * FROM remote_stor_ping WHERE connect_str like '%%:%s%%';" % (inst['bind-address']))
                    if int(cur.rowcount) is 0:
                        pinger_list.append('Mysql with ip %s not found in pinger database!' % (inst['bind-address']))
    except Exception, err:
            output('MySQL error. Check me.')
            ### We cant print exeption error here 'cos it can contain auth data
            exit(1)

    if pinger_list:
        print_list(pinger_list)
        exit(2)

### Make depended things
inits = get_all_mysql(mysql_init_path)
mysql_dict = make_mysql_dict(inits, init_lookup_list, conf_lookup_list)
flag_dict = check_flag(mysql_dict)

### Check things
if opts.type == 'ok':
    check_ok(mysql_dict, flag_dict)
if opts.type == 'repl':
    check_mysql(mysql_dict, flag_dict, opts.crit_limit, check_repl=True)
if opts.type == 'load':
    check_mysql(mysql_dict, flag_dict, opts.crit_limit, check_load=True)
if opts.type == 'pinger':
    check_pinger(mysql_dict, opts.config)
