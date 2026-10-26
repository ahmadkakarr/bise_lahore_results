"""Binding must precede TCP SYN, including cancellation and exception handling."""
import asyncio
import socket
import unittest
from unittest.mock import AsyncMock,Mock,patch

import httpcore
import httpx

from youthgames_register import bound_transport as bound


class BoundTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_interface_and_source_are_set_before_connect_not_after(self):
        calls=[];raw=Mock()
        raw.setblocking.side_effect=lambda *args:calls.append(('blocking',args))
        raw.setsockopt.side_effect=lambda *args:calls.append(('option',args))
        raw.bind.side_effect=lambda *args:calls.append(('bind',args))
        loop=Mock();loop.getaddrinfo=AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('192.0.2.9',443))])
        async def connect(*args):calls.append(('connect',args))
        loop.sock_connect=connect
        stream=Mock()
        with patch.object(bound.asyncio,'get_running_loop',return_value=loop),patch.object(bound.socket,'socket',return_value=raw), \
                patch.object(bound.SocketStream,'from_socket',new_callable=AsyncMock,return_value=stream):
            result=await bound.InterfaceBackend('usb0','192.0.2.2').connect_tcp('example.invalid',443,timeout=1)
        self.assertIs(result.stream,stream)
        binding=calls.index(('option',(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,b'usb0\0')))
        source=calls.index(('bind',(('192.0.2.2',0),)))
        connected=next(i for i,call in enumerate(calls) if call[0]=='connect')
        self.assertLess(binding,source);self.assertLess(source,connected);raw.close.assert_not_called()

    async def test_bind_failure_closes_socket_and_never_attempts_unbound_connect(self):
        raw=Mock();raw.setsockopt.side_effect=PermissionError('fixture bind denied')
        loop=Mock();loop.getaddrinfo=AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('192.0.2.9',443))])
        loop.sock_connect=AsyncMock()
        with patch.object(bound.asyncio,'get_running_loop',return_value=loop),patch.object(bound.socket,'socket',return_value=raw):
            with self.assertRaises(httpcore.ConnectError):await bound.InterfaceBackend('usb0','192.0.2.2').connect_tcp('example.invalid',443,timeout=1)
        loop.sock_connect.assert_not_called();raw.close.assert_called_once()

    async def test_cancellation_during_connect_closes_prebound_socket(self):
        raw=Mock();started=asyncio.Event()
        loop=Mock();loop.getaddrinfo=AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('192.0.2.9',443))])
        async def connect(*args):started.set();await asyncio.Event().wait()
        loop.sock_connect=connect
        with patch.object(bound.asyncio,'get_running_loop',return_value=loop),patch.object(bound.socket,'socket',return_value=raw):
            task=asyncio.create_task(bound.InterfaceBackend('usb0','192.0.2.2').connect_tcp('example.invalid',443,timeout=2))
            await asyncio.wait_for(started.wait(),1);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        raw.close.assert_called_once()

    async def test_core_errors_are_mapped_to_httpx_for_existing_recovery(self):
        for core_type,httpx_type in ((httpcore.ConnectTimeout,httpx.ConnectTimeout),(httpcore.ReadTimeout,httpx.ReadTimeout),
                (httpcore.ConnectError,httpx.ConnectError),(httpcore.RemoteProtocolError,httpx.RemoteProtocolError)):
            with self.subTest(error=core_type),self.assertRaises(httpx_type):
                with bound.httpx_errors():raise core_type('fixture')

    async def test_pool_has_verified_tls_and_no_automatic_connection_retries(self):
        with patch.object(bound.httpcore,'AsyncConnectionPool') as pool:
            transport=bound.InterfaceTransport('usb0','192.0.2.2')
        settings=pool.call_args.kwargs
        self.assertEqual(settings['retries'],0);self.assertEqual(settings['local_address'],'192.0.2.2')
        self.assertTrue(settings['ssl_context'].check_hostname)
        self.assertEqual(settings['network_backend'],transport.backend)

    async def test_dynamic_source_is_resolved_again_on_reconnect_before_binding_same_interface(self):
        sockets=[Mock(),Mock()];wrapped=Mock()
        loop=Mock();loop.getaddrinfo=AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('192.0.2.9',443))])
        loop.sock_connect=AsyncMock()
        backend=bound.InterfaceBackend('usb0','192.0.2.2',dynamic_source=True)
        with patch.object(bound.asyncio,'get_running_loop',return_value=loop), \
                patch.object(bound.asyncio,'to_thread',new_callable=AsyncMock,side_effect=['192.0.2.2','192.0.2.3']), \
                patch.object(bound.socket,'socket',side_effect=sockets), \
                patch.object(bound.SocketStream,'from_socket',new_callable=AsyncMock,return_value=wrapped):
            await backend.connect_tcp('example.invalid',443,timeout=1)
            await backend.connect_tcp('example.invalid',443,timeout=1)
        sockets[0].bind.assert_called_once_with(('192.0.2.2',0))
        sockets[1].bind.assert_called_once_with(('192.0.2.3',0))
        for raw in sockets:raw.setsockopt.assert_any_call(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,b'usb0\0')

    async def test_dynamic_source_outage_never_creates_an_unbound_socket(self):
        with patch.object(bound.asyncio,'to_thread',new_callable=AsyncMock,side_effect=OSError('USB missing')), \
                patch.object(bound.socket,'socket') as raw:
            with self.assertRaises(httpcore.ConnectError):
                await bound.InterfaceBackend('usb0',None,dynamic_source=True).connect_tcp('example.invalid',443,timeout=1)
        raw.assert_not_called()
