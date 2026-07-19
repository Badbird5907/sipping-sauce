from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from queue import Empty, Full, Queue
import re
from threading import Lock
from typing import AsyncIterator
import uuid
import wave

from .audio import FRAME_BYTES, FRAME_MS, PYVOIP_SILENCE, SAMPLE_RATE


LOG = logging.getLogger(__name__)
LiveQueue = Queue[bytes | None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _slug(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return cleaned[:48] or fallback


def _stereo_frame(caller: bytes, bot: bytes) -> bytes:
    stereo = bytearray(len(caller) * 2)
    stereo[0::2] = caller
    stereo[1::2] = bot
    return bytes(stereo)


def _mixed_frame(caller: bytes, bot: bytes) -> bytes:
    return bytes(max(0, min(255, left + right - 128)) for left, right in zip(caller, bot))


class CallSession:
    """Capture, persist, and publish the audio for one call."""

    def __init__(
        self,
        recordings_dir: Path,
        *,
        caller: str,
        direction: str,
        profile: str,
        sip_call_id: str | None,
        record: bool,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.caller = caller or "unknown"
        self.direction = direction
        self.profile = profile
        self.sip_call_id = sip_call_id
        self.started_at = _utc_now()
        self.ended_at: datetime | None = None
        self.error: str | None = None
        self.record = record
        stamp = self.started_at.strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{_slug(profile, 'call')}-{self.id}.wav"
        self.recording_path = recordings_dir / name
        self.metadata_path = self.recording_path.with_suffix(".json")
        self._caller_buffer = bytearray()
        self._bot_buffer = bytearray()
        self._audio_lock = Lock()
        self._subscriber_lock = Lock()
        self._subscribers: set[LiveQueue] = set()
        self._active = True

    @property
    def active(self) -> bool:
        return self._active

    def caller_audio(self, data: bytes) -> None:
        with self._audio_lock:
            self._caller_buffer.extend(data)

    def bot_audio(self, data: bytes) -> None:
        with self._audio_lock:
            self._bot_buffer.extend(data)

    def subscribe(self) -> LiveQueue:
        stream: LiveQueue = Queue(maxsize=25)
        with self._subscriber_lock:
            if self._active:
                self._subscribers.add(stream)
            else:
                stream.put_nowait(None)
        return stream

    def unsubscribe(self, stream: LiveQueue) -> None:
        with self._subscriber_lock:
            self._subscribers.discard(stream)

    def finish(self, error: BaseException | None = None) -> None:
        self.ended_at = _utc_now()
        self.error = str(error) if error else None
        self._active = False

    def snapshot(self) -> dict[str, object]:
        end = self.ended_at or _utc_now()
        return {
            "id": self.id,
            "caller": self.caller,
            "direction": self.direction,
            "profile": self.profile,
            "sip_call_id": self.sip_call_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_seconds": round((end - self.started_at).total_seconds(), 1),
            "active": self.active,
            "error": self.error,
            "recording": self.record,
            "recording_name": self.recording_path.name if self.record else None,
            "recording_url": (
                f"/recordings/{self.recording_path.name}" if self.record else None
            ),
            "live_url": f"/api/sessions/{self.id}/live.pcm" if self.active else None,
        }

    async def capture(self) -> None:
        writer: wave.Wave_write | None = None
        try:
            if self.record:
                try:
                    self.recording_path.parent.mkdir(parents=True, exist_ok=True)
                    writer = wave.open(str(self.recording_path), "wb")
                    writer.setnchannels(2)
                    writer.setsampwidth(1)
                    writer.setframerate(SAMPLE_RATE)
                except (OSError, wave.Error):
                    LOG.exception(
                        "Could not open call recording %s", self.recording_path
                    )
                    self.record = False
                    writer = None

            loop = asyncio.get_running_loop()
            next_tick = loop.time()
            while self.active or self._has_buffered_audio():
                caller, bot = self._take_frames()
                if writer is not None:
                    try:
                        writer.writeframesraw(_stereo_frame(caller, bot))
                    except (OSError, wave.Error):
                        LOG.exception(
                            "Could not continue call recording %s",
                            self.recording_path,
                        )
                        try:
                            writer.close()
                        except (OSError, wave.Error):
                            pass
                        writer = None
                        self.record = False
                self._publish(_mixed_frame(caller, bot))
                next_tick += FRAME_MS / 1000
                await asyncio.sleep(max(0, next_tick - loop.time()))
        finally:
            if writer is not None:
                writer.close()
            self._close_subscribers()

    def write_metadata(self) -> None:
        if not self.record:
            return
        try:
            self.metadata_path.write_text(
                json.dumps(self.snapshot(), indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            LOG.exception("Could not write recording metadata %s", self.metadata_path)

    def _has_buffered_audio(self) -> bool:
        with self._audio_lock:
            return bool(self._caller_buffer or self._bot_buffer)

    def _take_frames(self) -> tuple[bytes, bytes]:
        with self._audio_lock:
            caller = bytes(self._caller_buffer[:FRAME_BYTES])
            bot = bytes(self._bot_buffer[:FRAME_BYTES])
            del self._caller_buffer[:FRAME_BYTES]
            del self._bot_buffer[:FRAME_BYTES]
        return (
            caller.ljust(FRAME_BYTES, PYVOIP_SILENCE),
            bot.ljust(FRAME_BYTES, PYVOIP_SILENCE),
        )

    def _publish(self, chunk: bytes) -> None:
        with self._subscriber_lock:
            subscribers = tuple(self._subscribers)
        for stream in subscribers:
            try:
                stream.put_nowait(chunk)
            except Full:
                try:
                    stream.get_nowait()
                except Empty:
                    pass
                try:
                    stream.put_nowait(chunk)
                except Full:
                    pass

    def _close_subscribers(self) -> None:
        with self._subscriber_lock:
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
        for stream in subscribers:
            try:
                stream.put_nowait(None)
            except Full:
                try:
                    stream.get_nowait()
                except Empty:
                    pass
                try:
                    stream.put_nowait(None)
                except Full:
                    pass


class CallMonitor:
    def __init__(self, recordings_dir: str | Path, *, record: bool = True) -> None:
        self.recordings_dir = Path(recordings_dir).expanduser().resolve()
        self.record = record
        self._active: dict[str, CallSession] = {}
        self._lock = Lock()

    @asynccontextmanager
    async def track(
        self,
        *,
        caller: str,
        direction: str,
        profile: str,
        sip_call_id: str | None = None,
    ) -> AsyncIterator[CallSession]:
        session = CallSession(
            self.recordings_dir,
            caller=caller,
            direction=direction,
            profile=profile,
            sip_call_id=sip_call_id,
            record=self.record,
        )
        with self._lock:
            self._active[session.id] = session
        capture_task = asyncio.create_task(
            session.capture(), name=f"record-call-{session.id}"
        )
        error: BaseException | None = None
        try:
            yield session
        except BaseException as exc:
            error = exc
            raise
        finally:
            session.finish(error)
            await asyncio.gather(capture_task, return_exceptions=True)
            with self._lock:
                self._active.pop(session.id, None)
            session.write_metadata()

    def active_sessions(self) -> list[dict[str, object]]:
        with self._lock:
            sessions = tuple(self._active.values())
        return [session.snapshot() for session in sessions]

    def get_active(self, session_id: str) -> CallSession | None:
        with self._lock:
            return self._active.get(session_id)

    def recordings(self) -> list[dict[str, object]]:
        if not self.recordings_dir.exists():
            return []
        entries: list[dict[str, object]] = []
        for metadata_path in self.recordings_dir.glob("*.json"):
            try:
                item = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            recording_name = item.get("recording_name")
            if not isinstance(recording_name, str):
                continue
            recording_path = self.recording_path(recording_name)
            if recording_path is None or not recording_path.is_file():
                continue
            item["size_bytes"] = recording_path.stat().st_size
            entries.append(item)
        entries.sort(key=lambda item: str(item.get("started_at", "")), reverse=True)
        return entries

    def recording_path(self, name: str) -> Path | None:
        if Path(name).name != name:
            return None
        candidate = (self.recordings_dir / name).resolve()
        if candidate.parent != self.recordings_dir or candidate.suffix.lower() != ".wav":
            return None
        return candidate
