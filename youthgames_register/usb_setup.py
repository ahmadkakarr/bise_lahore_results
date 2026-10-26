"""Pin one observed phone's USB identity to its existing isolated network profile.

Installation reloads udev rules only. It never restarts NetworkManager, changes
Wi-Fi, activates connections, renames a live device, or touches registration DBs.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

RULE_PATH=Path('/etc/udev/rules.d/70-bise-mobile-usb.rules')
HEADER='# Managed by bise_lahore_results usb_setup; exact observed phone only.\n'


def load(path):
    value=json.loads(Path(path).read_text())
    fields={'schema_version','stable_interface','usb_vendor','usb_serial','networkmanager_profile_uuid','route_table'}
    if not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int or value['schema_version']!=1:
        raise ValueError('Use the complete schema-1 observed USB device plan')
    patterns={'stable_interface':r'[A-Za-z][A-Za-z0-9_.-]{0,14}','usb_vendor':r'[0-9a-f]{4}',
              'usb_serial':r'[A-Za-z0-9_-]{1,80}',
              'networkmanager_profile_uuid':r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'}
    for key,pattern in patterns.items():
        if not isinstance(value[key],str) or not re.fullmatch(pattern,value[key]):
            raise ValueError('Invalid USB identity field: '+key)
    if type(value['route_table']) is not int or not 10000<=value['route_table']<=60000:
        raise ValueError('Use an explicit non-main USB policy-routing table')
    return value


def rule(plan):
    # usb_id exports ID_USB_* even when an earlier rule already set ID_BUS.
    # ENV predicates avoid the same-ancestor constraint of multiple ATTRS keys.
    return HEADER+('ACTION=="add", SUBSYSTEM=="net", SUBSYSTEMS=="usb", IMPORT{builtin}="usb_id"\n'
        'ACTION=="add", SUBSYSTEM=="net", '
        'DRIVERS=="rndis_host|cdc_ether|cdc_ncm", '
        f'ENV{{ID_USB_VENDOR_ID}}=="{plan["usb_vendor"]}", '
        f'ENV{{ID_USB_SERIAL_SHORT}}=="{plan["usb_serial"]}", '
        f'NAME:="{plan["stable_interface"]}"\n')


def command(arguments):
    result=subprocess.run(arguments,capture_output=True,text=True,timeout=15,check=False)
    if result.returncode:
        raise ValueError('Command failed: '+arguments[0]+'; '+result.stderr.strip()[:500])
    return result.stdout.strip()


def profile_issues(plan):
    """Read-only assertion: stable name must select the already isolated profile."""
    expected={'connection.interface-name':plan['stable_interface'],
              'connection.autoconnect':'yes','ipv4.method':'auto',
              'ipv4.route-table':str(plan['route_table']),'ipv6.route-table':str(plan['route_table']),
              'ipv4.ignore-auto-dns':'yes','ipv6.ignore-auto-dns':'yes'}
    issues=[]
    for key,value in expected.items():
        actual=command(['nmcli','--escape','no','-g',key,'connection','show','uuid',plan['networkmanager_profile_uuid']])
        if actual!=value:issues.append(key+' differs from the isolated USB profile')
    for family in ('ipv4','ipv6'):
        rules=command(['nmcli','--escape','no','-g',family+'.routing-rules','connection','show','uuid',plan['networkmanager_profile_uuid']])
        entries=[r.strip() for r in rules.split(',') if r.strip()]
        if not entries or any(f'oif {plan["stable_interface"]} table {plan["route_table"]}' not in r for r in entries):
            issues.append(family+' must restrict its USB routing rule to the stable device')
        dns=command(['nmcli','--escape','no','-g',family+'.dns','connection','show','uuid',plan['networkmanager_profile_uuid']])
        if dns not in ('','--'):issues.append(family+' has static USB DNS servers; normal DNS must remain on Wi-Fi')
    mac=command(['nmcli','--escape','no','-g','802-3-ethernet.mac-address','connection','show','uuid',plan['networkmanager_profile_uuid']])
    if mac not in ('','--'):issues.append('USB profile is pinned to a MAC that can change on tether reconnect')
    return issues


def devices(plan,sys_root=Path('/sys')):
    """Read exact USB serial, independent of port path or randomized MAC."""
    matches=[]
    for path in (sys_root/'bus/usb/devices').glob('*'):
        try:
            if (path/'idVendor').read_text().strip()!=plan['usb_vendor'] or (path/'serial').read_text().strip()!=plan['usb_serial']:
                continue
            usb=path.resolve()
            interfaces=[]
            for interface in (sys_root/'class/net').glob('*'):
                device=(interface/'device').resolve()
                if device.is_relative_to(usb):interfaces.append(interface.name)
            matches.append({'interfaces':sorted(interfaces),'tethering_network_present':bool(interfaces)})
        except (OSError,ValueError):continue
    return matches


def status(plan,installed_path=RULE_PATH):
    observed=devices(plan)
    return {'rule_installed':installed_path.exists() and installed_path.read_text()==rule(plan),
            'stable_interface':plan['stable_interface'],'profile_issues':profile_issues(plan),
            'phone_present':bool(observed),'observed_networks':[name for d in observed for name in d['interfaces']],
            'applicant_posts':0}


def publish(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink():raise ValueError('Refusing to replace a symbolic-link rules file')
    if path.exists():
        previous=path.read_text()
        if previous==text:return False
        # Device identity changes are explicit: never silently replace a rule.
        raise ValueError('Existing USB rule differs; preserve and inspect it before changing the phone identity')
    fd,temporary=tempfile.mkstemp(prefix='.bise-usb-',suffix='.rules',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            stream.write(text);stream.flush();os.fsync(stream.fileno());os.fchmod(stream.fileno(),0o644)
        os.link(temporary,path)  # Atomic no-overwrite publication.
    finally:Path(temporary).unlink(missing_ok=True)
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)
    return True


def install(plan):
    if os.geteuid()!=0:
        raise ValueError('Installing a persistent USB naming rule in /etc requires sudo; use the prepared install command')
    issues=profile_issues(plan)
    if issues:raise ValueError('Isolated profile validation failed: '+'; '.join(issues))
    # Do not overwrite a name currently used by a different phone/device.
    target=Path('/sys/class/net')/plan['stable_interface']
    observed=devices(plan)
    if target.exists() and not any(plan['stable_interface'] in row['interfaces'] for row in observed):
        raise ValueError('The reserved interface name belongs to another device; no rule installed')
    with tempfile.TemporaryDirectory(prefix='bise-usb-rule-') as work:
        test=Path(work)/RULE_PATH.name;test.write_text(rule(plan))
        command(['udevadm','verify',str(test)])
    changed=publish(RULE_PATH,rule(plan))
    # Always reload on rerun: recovers a previous interruption after publication.
    command(['udevadm','control','--reload-rules'])
    return {'status':'installed' if changed else 'already_installed','rule':str(RULE_PATH),
            'stable_interface':plan['stable_interface'],'requires_replug':True,
            'networkmanager_restarted':False,'wifi_modified':False,'applicant_posts':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('connection_profiles/mobile-usb-device.json'))
    actions=parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--install',action='store_true')
    actions.add_argument('--status',action='store_true')
    actions.add_argument('--write-rule',type=Path,help='Prepare a reviewable rule without system changes')
    args=parser.parse_args()
    try:
        plan=load(args.config)
        if args.install:result=install(plan)
        elif args.status:result=status(plan)
        else:
            publish(args.write_rule,rule(plan))
            result={'status':'prepared','rule':str(args.write_rule),'applicant_posts':0}
        print(json.dumps(result,indent=2))
        return 0
    except (ValueError,OSError,subprocess.SubprocessError) as error:
        print('Error: '+str(error),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
