"""Explicit Linux Wi-Fi/USB routes; read-only checks never submit applicants."""

import argparse
import asyncio
from dataclasses import dataclass,replace
import ipaddress
import json
from pathlib import Path
import re
import socket
import subprocess

import httpx

from .config import signature

ECHO='https://api.ipify.org?format=json'


class NetworkUnavailable(ValueError):
    """A configured route is temporarily offline; configuration stays fixed."""


def ip_info(*arguments):
    try:return json.loads(subprocess.run(['ip','-j',*arguments],capture_output=True,text=True,check=True,timeout=5).stdout)
    except FileNotFoundError:
        raise ValueError('Cannot inspect network routes; install Linux iproute2') from None
    except (OSError,ValueError,subprocess.SubprocessError):
        raise NetworkUnavailable('Network inventory temporarily unavailable; retrying route discovery') from None


@dataclass(frozen=True)
class Connection:
    name:str
    interface:str
    source_ip:str | None
    public_ip:str | None
    interval:float
    dynamic_source:bool=False

    @property
    def key(self):return signature({'policy':'approved-interface-v1','interface':self.interface})

    def transport(self):
        from .bound_transport import InterfaceTransport
        if self.dynamic_source:return InterfaceTransport(self.interface,self.source_ip,dynamic_source=True)
        return InterfaceTransport(self.interface,self.source_ip)

    @property
    def label(self):
        if self.interface.startswith('wl') or self.name.lower()=='wifi':return 'Wi-Fi'
        if (self.interface.startswith('en') and 'u' in self.interface) or self.name.lower() in ('usb','mobile'):
            return 'Mobile USB'
        return self.name

    def public(self):
        # Identifiers only: addresses and transient sessions stay out of logs.
        return {'connection':self.name,'label':self.label,'interface':self.interface,'connection_key':self.key,
            'dispatch_interval_seconds':self.interval}


def inventory():
    addresses=ip_info('address','show')
    # USB may intentionally have its default in a dedicated policy table so it
    # cannot take over normal laptop traffic. SO_BINDTODEVICE selects its rule.
    defaults={row.get('dev') for row in ip_info('-4','route','show','table','all')
        if row.get('dst')=='default' and row.get('type','unicast')=='unicast'
        and 'linkdown' not in row.get('flags',[])}
    def ready(row):
        # RNDIS/USB drivers may not implement operstate and report UNKNOWN
        # despite an administratively UP interface and a live lower carrier.
        return row.get('operstate')=='UP' or (row.get('operstate')=='UNKNOWN'
            and {'UP','LOWER_UP'}.issubset(set(row.get('flags',[]))))
    return {row['ifname']:[a['local'] for a in row.get('addr_info',[])
        if a['family']=='inet' and a['scope']=='global'] for row in addresses
        if row['ifname'] in defaults and ready(row)
        and any(a['family']=='inet' and a['scope']=='global' for a in row.get('addr_info',[]))}


def unavailable_reason(interface, sys_root=Path('/sys')):
    """Diagnose USB renames without adopting an unapproved replacement route."""
    basic=f'Configured interface {interface} is disconnected; waiting for reconnect'
    if not ((interface.startswith('en') and 'u' in interface) or interface.startswith('usb')):
        return basic
    others=[]
    for entry in (sys_root/'class/net').glob('*'):
        try:
            device=(entry/'device').resolve()
            if any(re.fullmatch(r'usb[0-9]+',part) for part in device.parts):others.append(entry.name)
        except OSError:continue
    if others:
        return (basic+'; USB network detected as '+', '.join(sorted(others))+
                '. Stable phone identity/routing setup is needed; no automatic route substitution')
    return basic+'; no USB tethering network device is present (charging/file transfer alone is not tethering)'


def load(path, require_pinned=True, online=True):
    try:config=json.loads(Path(path).read_text())
    except (ValueError,OSError):raise ValueError('Cannot read connections config') from None
    if set(config)!={'schema_version','connections'} or config['schema_version']!=1:
        raise ValueError('Use the schema-1 connections config from connections discover')
    rows=config['connections']
    if not isinstance(rows,list) or not 2<=len(rows)<=8:raise ValueError('Connect at least two independent internet routes (maximum eight)')
    available=inventory() if online else {};routes=[];names=set();devices=set()
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'name','interface','source_ip','expected_public_ip','dispatch_interval_seconds'}:
            raise ValueError('Connections need name/interface/source_ip/expected_public_ip/dispatch_interval_seconds')
        name,interface=row['name'],row['interface'];interval=row['dispatch_interval_seconds']
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',name) or name in names:
            raise ValueError('Connection names must be unique simple labels')
        if not isinstance(interface,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}',interface) or interface in devices:
            raise ValueError('Configured interfaces must be unique Linux interface names')
        if type(interval) not in (int,float) or not 5<=interval<=3600:raise ValueError('Each connection interval must be finite, 5–3600 seconds')
        source=row['source_ip'];dynamic=source is None
        try:
            if source is not None and (not isinstance(source,str) or not ipaddress.IPv4Address(source)):raise ValueError()
        except (ValueError,TypeError):raise ValueError('Source IP must be IPv4 or null for DHCP') from None
        if online:
            if interface not in available:raise NetworkUnavailable('An assigned interface is missing, down or has no default route; waiting for USB/Wi-Fi')
            addresses=available[interface]
            if source is None and len(addresses)==1:source=addresses[0]
            if source is None:raise ValueError('Multiple IPv4 addresses require an explicit source IP')
            if source not in addresses:raise NetworkUnavailable('Source IP is not assigned to its connection interface')
        public=row['expected_public_ip']
        try:
            if public is not None and (not isinstance(public,str) or not ipaddress.IPv4Address(public).is_global):raise ValueError()
        except (ValueError,TypeError):raise ValueError('Expected public IP must be a global IPv4 address') from None
        if require_pinned and not public:raise ValueError('Public IPs are not pinned; run connections check --pin first')
        if online:
            try:
                with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as probe:
                    probe.setsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,interface.encode()+b'\0')
                    probe.bind((source,0))
            except OSError as error:
                if isinstance(error,PermissionError):raise ValueError('Permission denied binding the configured network interface') from None
                raise NetworkUnavailable('Cannot bind assigned interface/source IP; waiting without route fallback') from None
        routes.append(Connection(name,interface,source,public,float(interval),dynamic))
        names.add(name);devices.add(interface)
    if require_pinned and len({route.public_ip for route in routes})!=len(routes):
        raise ValueError('Connections share a public IP; independent per-connection pacing is not applicable')
    return routes


def load_routes(path, proxy_config=None):
    """Validate offline; the optional proxy is added only by explicit opt-in."""
    routes=load(path,require_pinned=False,online=False)
    if proxy_config is not None:
        from .proxy_routes import load_proxy
        proxy=load_proxy(proxy_config)
        if len(routes)>=8:raise ValueError('Maximum eight routes including the optional proxy')
        if any(route.name==proxy.name or route.key==proxy.key for route in routes):
            raise ValueError('Proxy must have a unique connection name and route identity')
        routes=[*routes,proxy]
    return routes


async def verify(routes,pinned=True,client_factory=None):
    """One TLS-verified IP echo per route; no applicant data or portal POST."""
    observed=[]
    for route in routes:
        try:
            client=client_factory(route) if client_factory else httpx.AsyncClient(transport=route.transport(),trust_env=False,timeout=15)
            async with client:
                response=await client.get(ECHO);response.raise_for_status()
                address=response.json()['ip']
                if not isinstance(address,str) or not ipaddress.IPv4Address(address).is_global:raise ValueError()
        except (httpx.HTTPError,ValueError,KeyError,TypeError,OSError):
            raise NetworkUnavailable(f'Connection {route.name}: egress check failed; route will not fall back to another connection') from None
        if pinned and route.public_ip!=address:
            raise ValueError(f'Connection {route.name}: public IP changed; inspect and run check --pin before resuming')
        observed.append(address)
    if len(set(observed))!=len(observed):raise NetworkUnavailable('Both routes reach the internet through the same public IP')
    return observed


async def ready(path,stop,emit,recover=True,retry_base=15):
    """Observe current egress on fixed devices; NAT changes are not new routes."""
    retry=0
    while not stop.is_set():
        try:
            routes=await asyncio.to_thread(load,path,False)
            observed=await verify(routes,pinned=False)
            changed=sum(route.public_ip is not None and route.public_ip!=address for route,address in zip(routes,observed))
            emit('stage',message=f'Fixed interfaces verified; {len(observed)} distinct current public IPs; {changed} changed IP observations accepted; existing cooldowns retained')
            return [replace(route,public_ip=address) for route,address in zip(routes,observed)]
        except NetworkUnavailable as error:
            if not recover:raise
            retry+=1;seconds=min(60,retry_base*2**min(retry-1,2))
            emit('stage',message=f'Waiting for configured connections: {error}; retry in {seconds}s (Ctrl+C to stop)')
            emit('network_wait',seconds=seconds,attempt=retry,reason=str(error))
            try:await asyncio.wait_for(stop.wait(),timeout=seconds)
            except TimeoutError:pass
    return None


def current_source(interface):
    try:addresses=inventory().get(interface,[])
    except NetworkUnavailable as error:raise OSError(str(error)) from error
    if len(addresses)!=1:raise OSError('Configured interface is offline or its IPv4 source is ambiguous')
    return addresses[0]


def discover(path):
    available=inventory()
    if len(available)<2:raise ValueError('Only one internet route is ready. Enable mobile USB tethering alongside Wi-Fi, then rerun discovery')
    path=Path(path)
    if path.exists():
        load(path,require_pinned=False)
        return {'status':'existing_config_preserved','config':str(path),'applicant_posts':0}
    rows=[{'name':device,'interface':device,'source_ip':None,'expected_public_ip':None,
        'dispatch_interval_seconds':6} for device in sorted(available)]
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as output:output.write(json.dumps({'schema_version':1,'connections':rows},indent=2)+'\n')
    return {'status':'discovered','config':str(path),'connections':len(rows),'applicant_posts':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    discover_parser=commands.add_parser('discover');discover_parser.add_argument('--output',type=Path,required=True)
    check=commands.add_parser('check');check.add_argument('--config',type=Path,required=True)
    check.add_argument('--pin',action='store_true',help='Explicitly pin verified distinct public IPs in the local config')
    args=parser.parse_args()
    try:
        if args.command=='discover':result=discover(args.output)
        else:
            routes=load(args.config,require_pinned=not args.pin)
            addresses=asyncio.run(verify(routes,pinned=not args.pin))
            if args.pin:
                config=json.loads(args.config.read_text())
                for row,address in zip(config['connections'],addresses):row['expected_public_ip']=address
                temporary=args.config.with_suffix('.tmp');temporary.write_text(json.dumps(config,indent=2)+'\n');temporary.replace(args.config)
            result={'status':'verified','connections':[route.name for route in routes],
                'distinct_public_ips':len(addresses),'applicant_posts':0}
        print(json.dumps(result,indent=2));return 0
    except ValueError as error:
        print(json.dumps({'status':'not_ready','reason':str(error),'applicant_posts':0}));return 2


if __name__=='__main__':raise SystemExit(main())
