"""Bind the device and source before TCP SYN; public HTTPX/HTTPCore APIs only."""

import asyncio
from contextlib import contextmanager
import select
import socket

import anyio
from anyio.abc import SocketStream,SocketAttribute
from anyio.streams.tls import TLSStream,TLSAttribute
import httpcore
import httpx


@contextmanager
def core_errors(timeout_error,io_error):
    try:yield
    except TimeoutError as error:raise timeout_error(str(error)) from error
    except (OSError,anyio.BrokenResourceError,anyio.ClosedResourceError) as error:
        raise io_error(str(error)) from error


class Stream(httpcore.AsyncNetworkStream):
    def __init__(self,stream):self.stream=stream

    async def read(self,max_bytes,timeout=None):
        with core_errors(httpcore.ReadTimeout,httpcore.ReadError),anyio.fail_after(timeout):
            try:return await self.stream.receive(max_bytes)
            except anyio.EndOfStream:return b''

    async def write(self,buffer,timeout=None):
        if not buffer:return
        with core_errors(httpcore.WriteTimeout,httpcore.WriteError),anyio.fail_after(timeout):
            await self.stream.send(buffer)

    async def aclose(self):await self.stream.aclose()

    async def start_tls(self,ssl_context,server_hostname=None,timeout=None):
        with core_errors(httpcore.ConnectTimeout,httpcore.ConnectError),anyio.fail_after(timeout):
            try:
                stream=await TLSStream.wrap(self.stream,server_side=False,hostname=server_hostname,
                    ssl_context=ssl_context,standard_compatible=False)
            except BaseException:
                # Close even in a cancelled timeout scope; no connected FD leaks.
                with anyio.CancelScope(shield=True):await self.stream.aclose()
                raise
            return Stream(stream)

    def get_extra_info(self,info):
        if info=='ssl_object':return self.stream.extra(TLSAttribute.ssl_object,None)
        if info=='client_addr':return self.stream.extra(SocketAttribute.local_address,None)
        if info=='server_addr':return self.stream.extra(SocketAttribute.remote_address,None)
        raw=self.stream.extra(SocketAttribute.raw_socket,None)
        if info=='socket':return raw
        if info=='is_readable':
            try:return bool(raw and select.select([raw],[],[],0)[0])
            except (ValueError,OSError):return True
        return None


class InterfaceBackend(httpcore.AsyncNetworkBackend):
    def __init__(self,interface,source_ip,dynamic_source=False):
        self.interface=interface;self.source_ip=source_ip;self.dynamic_source=dynamic_source

    async def connect_tcp(self,host,port,timeout=None,local_address=None,socket_options=None):
        if local_address not in (None,self.source_ip):raise httpcore.ConnectError('Source binding changed')
        loop=asyncio.get_running_loop()
        with core_errors(httpcore.ConnectTimeout,httpcore.ConnectError):
            async with asyncio.timeout(timeout):
                source=self.source_ip
                if self.dynamic_source:
                    from .connections import current_source
                    source=await asyncio.to_thread(current_source,self.interface)
                targets=await loop.getaddrinfo(host,port,family=socket.AF_INET,type=socket.SOCK_STREAM)
                last_error=None
                for family,kind,protocol,_,address in targets:
                    raw=socket.socket(family,kind,protocol)
                    try:
                        raw.setblocking(False)
                        raw.setsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,self.interface.encode()+b'\0')
                        raw.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
                        for option in socket_options or ():raw.setsockopt(*option)
                        raw.bind((source,0))
                        await loop.sock_connect(raw,address)
                        wrapped=await SocketStream.from_socket(raw)
                        raw=None  # SocketStream now owns the connected FD.
                        return Stream(wrapped)
                    except OSError as error:last_error=error
                    finally:
                        if raw is not None:raw.close()
                raise last_error or OSError('No IPv4 endpoint resolved')

    async def connect_unix_socket(self,*args,**kwargs):raise httpcore.ConnectError('Unix routes are unsupported')

    async def sleep(self,seconds):await asyncio.sleep(seconds)


@contextmanager
def httpx_errors():
    try:yield
    except (httpcore.TimeoutException,httpcore.NetworkError,httpcore.ProtocolError,
            httpcore.ProxyError,httpcore.UnsupportedProtocol) as error:
        for name in ('ConnectTimeout','ReadTimeout','WriteTimeout','PoolTimeout','ConnectError','ReadError',
                     'WriteError','RemoteProtocolError','LocalProtocolError','ProxyError','UnsupportedProtocol',
                     'TimeoutException','NetworkError','ProtocolError'):
            if isinstance(error,getattr(httpcore,name)):
                raise getattr(httpx,name)(str(error)) from error
        raise


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self,stream):self.stream=stream

    async def __aiter__(self):
        with httpx_errors():
            async for chunk in self.stream:yield chunk

    async def aclose(self):await self.stream.aclose()


class InterfaceTransport(httpx.AsyncBaseTransport):
    def __init__(self,interface,source_ip,dynamic_source=False):
        self.backend=InterfaceBackend(interface,source_ip,dynamic_source)
        self.pool=httpcore.AsyncConnectionPool(ssl_context=httpcore.default_ssl_context(),
            max_connections=1,max_keepalive_connections=1,keepalive_expiry=60,retries=0,
            local_address=None if dynamic_source else source_ip,network_backend=self.backend,http1=True,http2=False)

    async def handle_async_request(self,request):
        core_request=httpcore.Request(method=request.method,url=httpcore.URL(
            scheme=request.url.raw_scheme,host=request.url.raw_host,port=request.url.port,target=request.url.raw_path),
            headers=request.headers.raw,content=request.stream,extensions=request.extensions)
        with httpx_errors():response=await self.pool.handle_async_request(core_request)
        return httpx.Response(response.status,headers=response.headers,stream=ResponseStream(response.stream),extensions=response.extensions)

    async def aclose(self):await self.pool.aclose()
