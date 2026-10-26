"""Independent fixed-interface readiness; an offline route cannot block peers."""
import asyncio
from dataclasses import replace
import time

from . import connections as network


class RouteHealth:
    def __init__(self,routes,stop,emit,recover=True,poll_seconds=2,retry_seconds=5):
        self.specs={r.name:r for r in routes}
        self.current={r.name:replace(r,public_ip=None) for r in routes}
        self.stop,self.emit,self.recover=stop,emit,recover
        self.poll_seconds,self.retry_seconds=poll_seconds,retry_seconds
        self.online={r.name:False for r in routes}
        self.next_check={r.name:0.0 for r in routes}
        self.locks={r.name:asyncio.Lock() for r in routes}
        self.generation={r.name:0 for r in routes}
        self.physical_down={r.name:False for r in routes}
        self.local_ready={r.name:False for r in routes}
        self.probe_requested={r.name:False for r in routes}
        self.probe_failures={r.name:0 for r in routes}
        self.last_portal_success={r.name:float('-inf') for r in routes}
        self.on_reconnect=None
        self.last_event={};self.last_public_check={};self.error=None

    def report(self,name,state,reason=''):
        value=(state,reason)
        if self.last_event.get(name)!=value:
            self.last_event[name]=value
            self.emit('connection_health',connection=name,state=state,reason=reason,
                retry_seconds=self.retry_seconds if state!='online' else 0,
                local_ready=self.local_ready[name],dispatch_ready=self.online[name],
                probe_failures=self.probe_failures[name])

    def invalidate(self,name,reason='Temporary request failure; rechecking configured interface'):
        # A failed portal request is not proof of a disconnected USB/internet
        # route. Coalesce concurrent worker failures into one pending check.
        if not self.probe_requested[name]:
            self.probe_requested[name]=True;self.next_check[name]=0
            if self.online[name]:self.report(name,'degraded',reason)

    def portal_succeeded(self,name):
        # Evidence only: never clears a server cooldown or authorizes a replay.
        self.last_portal_success[name]=time.monotonic()

    async def check(self,name):
        # A slow periodic IP echo must not hold every worker behind its lock
        # while this same interface/source already has verified egress.
        if self.locks[name].locked() and self.online[name] and self.local_ready[name]:
            return True
        async with self.locks[name]:
            if time.monotonic()<self.next_check[name]:return self.online[name]
            spec=self.specs[name];old=self.current[name]
            phase='inventory';source=None
            try:
                if getattr(spec,'is_proxy',False):
                    # CONNECT uses the ordinary uplink to this fixed endpoint;
                    # it is not a Linux device and must never bind/fall back.
                    source=None
                else:
                    addresses=(await asyncio.to_thread(network.inventory)).get(spec.interface,[])
                    if not addresses:
                        phase='link';self.local_ready[name]=False
                        self.physical_down[name]=True
                        raise network.NetworkUnavailable(network.unavailable_reason(spec.interface))
                    if spec.dynamic_source or spec.source_ip is None:
                        if len(addresses)!=1:
                            phase='source';self.local_ready[name]=False
                            raise network.NetworkUnavailable('Multiple IPv4 sources on this interface; waiting for one source or explicit source_ip')
                        source=addresses[0]
                    else:source=spec.source_ip
                    if source not in addresses:
                        phase='link';self.local_ready[name]=False
                        self.physical_down[name]=True
                        raise network.NetworkUnavailable('Configured source IP is not currently assigned')
                self.local_ready[name]=True
                candidate=replace(spec,source_ip=source)
                trusted=old.public_ip is not None and source==old.source_ip and not self.physical_down[name]
                if not trusted:self.online[name]=False
                # Echo only at initial/reconnect/source changes and periodically;
                # local carrier/source checks are cheap and more frequent.
                if (self.probe_requested[name] or not self.online[name] or source!=old.source_ip
                        or time.monotonic()-self.last_public_check.get(name,0)>=60):
                    phase='egress'
                    observed=(await network.verify([candidate],pinned=False))[0]
                    if any(peer!=name and self.online[peer] and route.public_ip==observed
                           for peer,route in self.current.items()):
                        phase='conflict'
                        raise network.NetworkUnavailable('Egress is shared with another active connection; this route is paused')
                    candidate=replace(candidate,public_ip=observed)
                    self.last_public_check[name]=time.monotonic()
                else:candidate=replace(candidate,public_ip=old.public_ip)
            except network.NetworkUnavailable as error:
                if not self.recover:
                    self.online[name]=False
                    raise
                self.next_check[name]=time.monotonic()+self.retry_seconds
                if phase=='egress':
                    self.probe_failures[name]+=1
                    self.probe_requested[name]=True
                    # One external echo service timing out does not invalidate
                    # a recently verified, unchanged route serving the portal.
                    recent=time.monotonic()-self.last_portal_success[name]<30
                    if trusted and (self.probe_failures[name]<3 or recent):
                        self.online[name]=True
                        self.report(name,'degraded','Interface/source unchanged; egress probe failed; rechecking without treating USB as disconnected')
                        return True
                self.online[name]=False
                state='offline' if phase=='link' else 'paused' if phase in ('source','conflict') else 'unreachable' if phase=='egress' else 'checking'
                self.report(name,state,str(error))
                return False
            # A successful echo after a portal error is not proof that the
            # portal recovered. Only a verified physical/address transition
            # interrupts its transport wait; server cooldowns are untouched.
            reconnected=self.physical_down[name] or (old.public_ip is not None and
                (old.source_ip!=candidate.source_ip or old.public_ip!=candidate.public_ip))
            if reconnected or not self.online[name]:self.generation[name]+=1
            self.physical_down[name]=False
            self.probe_requested[name]=False;self.probe_failures[name]=0
            self.current[name]=candidate;self.online[name]=True
            self.next_check[name]=time.monotonic()+self.poll_seconds
            self.report(name,'online','Configured route verified; queue may resume')
            if reconnected and self.on_reconnect:self.on_reconnect(name)
            return True

    async def initial(self):
        while not self.stop.is_set():
            checks=await self.check_all()
            if any(checks):
                self.emit('stage',message=f'{sum(checks)}/{len(checks)} connections online; available routes can work while peers reconnect')
                return list(self.current.values())
            self.emit('stage',message='All configured connections offline; waiting for reconnect (Ctrl+C to stop)')
            try:await asyncio.wait_for(self.stop.wait(),timeout=self.retry_seconds)
            except TimeoutError:pass
        return None

    async def check_all(self):
        tasks=[asyncio.create_task(self.check(name)) for name in self.specs]
        try:return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            raise

    async def wait(self,name,needed=lambda:True):
        while not self.stop.is_set() and needed():
            if await self.check(name):return True
            delay=max(.01,min(self.retry_seconds,self.next_check[name]-time.monotonic()))
            try:await asyncio.wait_for(self.stop.wait(),timeout=delay)
            except TimeoutError:pass
        return False

    async def watch(self):
        try:
            while not self.stop.is_set():
                await self.check_all()
                try:await asyncio.wait_for(self.stop.wait(),timeout=self.poll_seconds)
                except TimeoutError:pass
        except asyncio.CancelledError:raise
        except BaseException as error:
            self.error=type(error).__name__+': '+str(error)
            self.emit('stage',message='Network configuration requires attention: '+self.error)
            self.stop.set()
            raise
