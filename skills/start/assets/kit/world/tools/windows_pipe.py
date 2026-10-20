"""User-restricted, local-only Windows IPC. Payloads are JSON bytes, never pickle."""

from __future__ import annotations

import _winapi
import asyncio
import contextlib
import json
import uuid
from multiprocessing.connection import PipeClient, PipeConnection

import pywintypes
import win32api
import win32con
import win32pipe
import win32security

MAX_MESSAGE = 1024 * 1024


def pipe_security():
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        sid_text = win32security.ConvertSidToStringSid(sid)
    finally:
        token.Close()
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"D:P(A;;GA;;;{sid_text})", win32security.SDDL_REVISION_1
    )
    attributes.bInheritHandle = False
    return attributes


def new_handle(address, attributes, *, first=False):
    flags = win32pipe.PIPE_ACCESS_DUPLEX | win32con.FILE_FLAG_OVERLAPPED
    if first:
        flags |= _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE
    handle = win32pipe.CreateNamedPipe(
        address, flags,
        win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | 0x08,
        255, 65536, 65536, 0, attributes,
    )
    return handle.Detach()


def exchange(address: str, payload: dict) -> dict:
    if not address.startswith("\\\\.\\pipe\\world-mcp-"):
        raise ValueError("not a local World named pipe")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_MESSAGE:
        raise ValueError("bridge request exceeds maximum size")
    with PipeClient(address) as connection:
        connection.send_bytes(data)
        if not connection.poll(120):
            raise TimeoutError("Windows tool bridge response timed out")
        return json.loads(connection.recv_bytes(MAX_MESSAGE))


class WindowsPipeServer:
    def __init__(self, dispatch):
        self.address = "\\\\.\\pipe\\world-mcp-" + uuid.uuid4().hex
        self._dispatch = dispatch
        self._security = pipe_security()
        self._handle = new_handle(self.address, self._security, first=True)
        self._accept_task = None
        self._connections = set()
        self._tasks = set()
        self._overlapped = None

    async def start(self):
        self._accept_task = asyncio.create_task(self._accept())

    async def _accept(self):
        while True:
            try:
                self._overlapped = _winapi.ConnectNamedPipe(self._handle, overlapped=True)
            except OSError as exc:
                if exc.winerror != _winapi.ERROR_NO_DATA:
                    raise
                stale = self._handle
                self._handle = new_handle(self.address, self._security)
                _winapi.CloseHandle(stale)
                continue
            while True:
                ready = await asyncio.to_thread(
                    _winapi.WaitForMultipleObjects, [self._overlapped.event], False, 100
                )
                if ready != _winapi.WAIT_TIMEOUT:
                    break
            self._overlapped.GetOverlappedResult(True)
            self._overlapped = None
            connection = PipeConnection(self._handle)
            self._handle = None
            self._connections.add(connection)
            # Keep a listening instance alive so another process cannot claim this name.
            self._handle = new_handle(self.address, self._security)
            while len(self._tasks) >= 32:
                await asyncio.sleep(0.05)
            task = asyncio.create_task(self._serve(connection))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _serve(self, connection):
        try:
            if not await asyncio.to_thread(connection.poll, 10):
                return
            raw = await asyncio.to_thread(connection.recv_bytes, MAX_MESSAGE)
            response = await asyncio.wait_for(self._dispatch(json.loads(raw)), 110)
            data = json.dumps(response, ensure_ascii=False).encode("utf-8")
            if len(data) > MAX_MESSAGE:
                too_large = {"content": [{"type": "text", "text": "bridge response too large"}]}
                data = json.dumps({**too_large, "isError": True}).encode()
            try:
                await asyncio.wait_for(asyncio.to_thread(connection.send_bytes, data), 10)
            except (asyncio.CancelledError, TimeoutError):
                # PipeConnection.close cancels any outstanding overlapped send.
                connection.close()
                raise
        except (OSError, EOFError, ValueError, TimeoutError):
            pass
        finally:
            connection.close()
            self._connections.discard(connection)

    async def close(self):
        if self._accept_task:
            self._accept_task.cancel()
            await asyncio.gather(self._accept_task, return_exceptions=True)
        if self._overlapped:
            self._overlapped.cancel()
            with contextlib.suppress(OSError):
                self._overlapped.GetOverlappedResult(True)
            self._overlapped = None
        if self._handle is not None:
            _winapi.CloseHandle(self._handle)
            self._handle = None
        # Let short pending I/O complete before closing its handle.
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for connection in tuple(self._connections):
            connection.close()
