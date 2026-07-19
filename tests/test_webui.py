import asyncio
import json
from pathlib import Path
from threading import Event, Thread
from urllib.request import Request, urlopen

from partyline_llm.audio import FRAME_BYTES
from partyline_llm.recording import CallMonitor
from partyline_llm.webui import DashboardServer


def test_dashboard_lists_and_serves_recordings(tmp_path: Path) -> None:
    async def record_call(monitor: CallMonitor) -> None:
        async with monitor.track(
            caller="104", direction="incoming", profile="gargoyle"
        ) as session:
            session.caller_audio(bytes([132]) * FRAME_BYTES)
            session.bot_audio(bytes([124]) * FRAME_BYTES)
            await asyncio.sleep(0.03)

    monitor = CallMonitor(tmp_path)
    asyncio.run(record_call(monitor))
    server = DashboardServer(monitor, "127.0.0.1", 0)
    server.start()
    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/", timeout=2) as response:
            assert "SIP call monitor" in response.read().decode("utf-8")
        with urlopen(f"http://{host}:{port}/api/status", timeout=2) as response:
            status = json.load(response)
        assert status["active"] == []
        assert status["recordings"][0]["caller"] == "104"

        recording_url = status["recordings"][0]["recording_url"]
        request = Request(
            f"http://{host}:{port}{recording_url}",
            headers={"Range": "bytes=0-15"},
        )
        with urlopen(request, timeout=2) as response:
            assert response.status == 206
            assert response.headers["Content-Type"] == "audio/wav"
            assert len(response.read()) == 16
    finally:
        server.close()


def test_dashboard_streams_an_active_call(tmp_path: Path) -> None:
    monitor = CallMonitor(tmp_path, record=False)
    ready = Event()
    stop = Event()

    async def active_call() -> None:
        async with monitor.track(
            caller="105", direction="incoming", profile="oracle"
        ) as session:
            ready.set()
            while not stop.is_set():
                session.caller_audio(bytes([136]) * FRAME_BYTES)
                session.bot_audio(bytes([120]) * FRAME_BYTES)
                await asyncio.sleep(0.02)

    producer = Thread(target=lambda: asyncio.run(active_call()), daemon=True)
    producer.start()
    assert ready.wait(timeout=2)
    server = DashboardServer(monitor, "127.0.0.1", 0)
    server.start()
    response = None
    try:
        host, port = server.address
        active = monitor.active_sessions()[0]
        response = urlopen(
            f"http://{host}:{port}{active['live_url']}", timeout=2
        )
        assert response.headers["Content-Type"] == "application/octet-stream"
        assert response.headers["X-Audio-Format"] == "unsigned-8-bit-pcm"
        assert response.read(FRAME_BYTES) == bytes([128]) * FRAME_BYTES
    finally:
        if response is not None:
            response.close()
        stop.set()
        producer.join(timeout=2)
        server.close()
