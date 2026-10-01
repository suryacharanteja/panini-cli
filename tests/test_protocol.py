import asyncio
import ctypes
import os
import sys
from pathlib import Path

import pytest
from panini_cli.protocol import AppServer, ProtocolError


@pytest.fixture
def anyio_backend():
    return "asyncio"


def server(tmp_path, callback=None):
    async def approve(method, params):
        assert (method, params) == ("approve", {"command": "fixture"})
        return {"decision": "accept"}

    return AppServer(
        [sys.executable, str(Path(__file__).with_name("fake_server.py"))],
        tmp_path,
        callback or approve,
    )


@pytest.mark.anyio
async def test_bidirectional_approval_does_not_block_responses(tmp_path):
    peer = server(tmp_path)
    try:
        assert await peer.start() == {"userAgent": "fixture"}
        assert await peer.request("interleave", {}) == {"done": True}
        events = [await asyncio.wait_for(peer.next_event(), 2) for _ in range(2)]
        assert events == [
            {"method": "progress", "params": {"step": 1}},
            {"method": "decision", "params": {"decision": "accept"}},
        ]
    finally:
        await peer.close()


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["bad-frame", "disconnect"])
async def test_disconnect_or_malformed_frames_fail_pending_requests(tmp_path, method):
    peer = server(tmp_path)
    await peer.start()
    with pytest.raises(ProtocolError):
        await peer.request(method, {}, timeout=2)
    await peer.close()
    assert peer.process.returncode is not None


@pytest.mark.anyio
async def test_timeout_does_not_leave_request_pending(tmp_path):
    peer = server(tmp_path)
    try:
        await peer.start()
        with pytest.raises(TimeoutError):
            await peer.request("never", {}, timeout=0.05)
        assert peer.pending == {}
    finally:
        await peer.close()


@pytest.mark.anyio
async def test_cancel_while_spawning_does_not_leak_process(monkeypatch, tmp_path):
    from panini_cli.process import ProcessTree

    original = asyncio.create_subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        child = await original(*args, **kwargs)
        spawned.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    tree = ProcessTree()
    start = asyncio.create_task(tree.start([sys.executable, "-c", "import time; time.sleep(90)"], tmp_path))
    await spawned.wait()
    start.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert tree.closed and tree.process.returncode is not None


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object test")
async def test_stop_kills_windows_descendants(tmp_path):
    peer = server(tmp_path)
    await peer.start()
    child = await peer.request("child", {})
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, child["pid"])
    assert handle
    try:
        await peer.close()
        assert kernel.WaitForSingleObject(handle, 2000) == 0
    finally:
        kernel.CloseHandle(handle)
