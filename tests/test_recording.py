import asyncio
from pathlib import Path
from queue import Empty
import wave

from partyline_llm.audio import FRAME_BYTES
from partyline_llm.recording import CallMonitor


def test_call_monitor_records_stereo_wav_and_metadata(tmp_path: Path) -> None:
    async def scenario() -> None:
        monitor = CallMonitor(tmp_path)
        async with monitor.track(
            caller="102",
            direction="incoming",
            profile="comedian",
            sip_call_id="call-123",
        ) as session:
            session.caller_audio(bytes([140]) * FRAME_BYTES)
            session.bot_audio(bytes([116]) * FRAME_BYTES)
            await asyncio.sleep(0.03)

        recordings = monitor.recordings()
        assert len(recordings) == 1
        assert recordings[0]["caller"] == "102"
        assert recordings[0]["profile"] == "comedian"
        recording_path = tmp_path / str(recordings[0]["recording_name"])
        with wave.open(str(recording_path), "rb") as recording:
            assert recording.getnchannels() == 2
            assert recording.getsampwidth() == 1
            assert recording.getframerate() == 8000
            audio = recording.readframes(recording.getnframes())
        assert bytes([140, 116]) in audio

    asyncio.run(scenario())


def test_live_stream_publishes_mixed_audio(tmp_path: Path) -> None:
    async def scenario() -> None:
        monitor = CallMonitor(tmp_path, record=False)
        async with monitor.track(
            caller="103", direction="incoming", profile="oracle"
        ) as session:
            stream = session.subscribe()
            session.caller_audio(bytes([138]) * FRAME_BYTES)
            session.bot_audio(bytes([118]) * FRAME_BYTES)
            await asyncio.sleep(0.03)
            chunk = stream.get_nowait()
            assert chunk == bytes([128]) * FRAME_BYTES
            session.unsubscribe(stream)

        try:
            stream.get_nowait()
        except Empty:
            pass

    asyncio.run(scenario())
