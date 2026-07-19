from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any
from urllib.parse import urlencode

from pyVoIP.VoIP import CallState
from websockets.asyncio.client import ClientConnection, connect

from .audio import (
    FRAME_BYTES,
    FRAME_MS,
    PCMUFrameBuffer,
    pcmu_to_pyvoip_u8,
    pyvoip_u8_to_pcmu,
    telephone_tone,
)
from .call import AudioCall, CallAudioObserver
from .config import Settings
from .profiles import BotProfile
from .recording import CallMonitor


LOG = logging.getLogger(__name__)
INPUT_CHUNK_MS = 40
INPUT_CHUNK_BYTES = 8000 * INPUT_CHUNK_MS // 1000
AUDIO_EVENT_TYPES = {"response.output_audio.delta", "response.audio.delta"}
AUDIO_DONE_TYPES = {"response.output_audio.done", "response.audio.done"}
TRANSCRIPT_DELTA_TYPES = {
    "response.output_audio_transcript.delta",
    "response.audio_transcript.delta",
}
CONNECTING_TONE = telephone_tone((350, 440))


def _openai_session_update_event(
    settings: Settings, profile: BotProfile
) -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": settings.openai_model,
            "output_modalities": ["audio"],
            "instructions": profile.instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": settings.realtime_vad_threshold,
                        "prefix_padding_ms": (
                            settings.realtime_vad_prefix_padding_ms
                        ),
                        "silence_duration_ms": (
                            settings.realtime_vad_silence_duration_ms
                        ),
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": profile.voice_for("openai") or settings.openai_voice,
                },
            },
        },
    }


def _xai_session_update_event(
    settings: Settings, profile: BotProfile
) -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "instructions": profile.instructions,
            "voice": profile.voice_for("xai") or settings.xai_voice,
            "turn_detection": {
                "type": "server_vad",
                "threshold": settings.realtime_vad_threshold,
                "prefix_padding_ms": settings.realtime_vad_prefix_padding_ms,
                "silence_duration_ms": (
                    settings.realtime_vad_silence_duration_ms
                ),
            },
            "audio": {
                "input": {"format": {"type": "audio/pcmu"}},
                "output": {"format": {"type": "audio/pcmu"}},
            },
        },
    }


def session_update_event(
    settings: Settings, profile: BotProfile
) -> dict[str, Any]:
    if settings.realtime_provider == "xai":
        return _xai_session_update_event(settings, profile)
    return _openai_session_update_event(settings, profile)


def greeting_event(greeting: str) -> dict[str, Any]:
    """Return the OpenAI-compatible one-shot greeting event."""
    return {
        "type": "response.create",
        "response": {
            "output_modalities": ["audio"],
            "instructions": f"Say this greeting naturally and briefly: {greeting}",
        },
    }


def greeting_events(provider: str, greeting: str) -> tuple[dict[str, Any], ...]:
    if provider == "xai":
        return (
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Say this greeting naturally and briefly, with "
                                f"no extra commentary: {greeting}"
                            ),
                        }
                    ],
                },
            },
            {"type": "response.create"},
        )
    return (greeting_event(greeting),)


def connection_details(settings: Settings) -> tuple[str, dict[str, str]]:
    query = urlencode({"model": settings.realtime_model})
    if settings.realtime_provider == "xai":
        return (
            f"wss://api.x.ai/v1/realtime?{query}",
            {"Authorization": f"Bearer {settings.realtime_api_key}"},
        )
    return (
        f"wss://api.openai.com/v1/realtime?{query}",
        {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "OpenAI-Safety-Identifier": settings.openai_safety_identifier,
        },
    )


async def _clear_queue(queue: asyncio.Queue[bytes]) -> None:
    while True:
        try:
            queue.get_nowait()
            queue.task_done()
        except asyncio.QueueEmpty:
            return


class RealtimeSIPBridge:
    def __init__(
        self,
        settings: Settings,
        profile: BotProfile,
        audio_observer: CallAudioObserver | None = None,
    ) -> None:
        self.settings = settings
        self.profile = profile
        self.audio_observer = audio_observer
        # Realtime audio can arrive faster than telephone playback. Keep every
        # frame until it is played or deliberately cleared on caller barge-in.
        self.output_frames: asyncio.Queue[bytes] = asyncio.Queue()
        self.output_buffer = PCMUFrameBuffer()
        self._greeting_sent = False

    async def run(self, call: AudioCall) -> None:
        url, headers = connection_details(self.settings)
        provider = self.settings.realtime_provider

        tone_task = asyncio.create_task(
            self._play_connecting_tone(call), name="connecting-tone"
        )
        try:
            LOG.info(
                "Connecting to %s Realtime model %s",
                provider,
                self.settings.realtime_model,
            )
            async with connect(
                url,
                additional_headers=headers,
                open_timeout=15,
                close_timeout=5,
                ping_interval=20,
                ping_timeout=20,
                max_size=None,
            ) as websocket:
                tone_task.cancel()
                await asyncio.gather(tone_task, return_exceptions=True)
                await websocket.send(
                    json.dumps(session_update_event(self.settings, self.profile))
                )
                LOG.info("%s Realtime connection established", provider)

                tasks = [
                    asyncio.create_task(
                        self._send_sip_audio(websocket, call),
                        name=f"sip-to-{provider}",
                    ),
                    asyncio.create_task(
                        self._receive_realtime_events(websocket),
                        name=f"{provider}-events",
                    ),
                    asyncio.create_task(
                        self._play_realtime_audio(call),
                        name=f"{provider}-to-sip",
                    ),
                    asyncio.create_task(self._watch_call(call), name="call-watcher"),
                ]
                try:
                    done, pending = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for task in done:
                        error = task.exception()
                        if error is not None:
                            raise error
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if not tone_task.done():
                tone_task.cancel()
            await asyncio.gather(tone_task, return_exceptions=True)

    async def _play_connecting_tone(self, call: AudioCall) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        offset = 0
        while call.state == CallState.ANSWERED:
            frame = CONNECTING_TONE[offset : offset + FRAME_BYTES]
            self._write_bot_audio(call, frame)
            offset = (offset + FRAME_BYTES) % len(CONNECTING_TONE)
            next_tick += FRAME_MS / 1000
            await asyncio.sleep(max(0, next_tick - loop.time()))

    async def _send_sip_audio(
        self, websocket: ClientConnection, call: AudioCall
    ) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while call.state == CallState.ANSWERED:
            audio = call.read_audio(INPUT_CHUNK_BYTES, blocking=False)
            if self.audio_observer is not None:
                self.audio_observer.caller_audio(audio)
            event = {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pyvoip_u8_to_pcmu(audio)).decode("ascii"),
            }
            await websocket.send(json.dumps(event))
            next_tick += INPUT_CHUNK_MS / 1000
            await asyncio.sleep(max(0, next_tick - loop.time()))

    async def _receive_realtime_events(self, websocket: ClientConnection) -> None:
        transcript = ""
        async for message in websocket:
            event = json.loads(message)
            event_type = event.get("type", "")

            if event_type in AUDIO_EVENT_TYPES:
                chunk = base64.b64decode(event.get("delta", ""))
                for frame in self.output_buffer.append(chunk):
                    await self._queue_frame(frame)
            elif event_type in AUDIO_DONE_TYPES:
                final_frame = self.output_buffer.flush()
                if final_frame is not None:
                    await self._queue_frame(final_frame)
            elif event_type == "input_audio_buffer.speech_started":
                self.output_buffer.clear()
                await _clear_queue(self.output_frames)
                LOG.debug("Caller speech started; cleared queued bot audio")
            elif event_type in TRANSCRIPT_DELTA_TYPES:
                delta = event.get("delta", "")
                transcript += delta
                if delta:
                    LOG.debug("Assistant transcript delta: %s", delta)
            elif event_type in {
                "response.output_audio_transcript.done",
                "response.audio_transcript.done",
            }:
                final = event.get("transcript", transcript).strip()
                if final:
                    LOG.info("Assistant: %s", final)
                transcript = ""
            elif event_type == "session.updated":
                LOG.info(
                    "%s Realtime session configured",
                    self.settings.realtime_provider,
                )
                if self.profile.greeting and not self._greeting_sent:
                    for greeting in greeting_events(
                        self.settings.realtime_provider, self.profile.greeting
                    ):
                        await websocket.send(json.dumps(greeting))
                    self._greeting_sent = True
            elif event_type == "error":
                error = event.get("error", {})
                message_text = error.get("message", json.dumps(error))
                raise RuntimeError(
                    f"{self.settings.realtime_provider} Realtime error: "
                    f"{message_text}"
                )
            elif event_type in {
                "session.created",
                "response.created",
                "response.done",
            }:
                LOG.debug("Realtime event: %s", event_type)

    async def _queue_frame(self, frame: bytes) -> None:
        await self.output_frames.put(frame)

    async def _play_realtime_audio(self, call: AudioCall) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while call.state == CallState.ANSWERED:
            try:
                frame = await asyncio.wait_for(
                    self.output_frames.get(), timeout=FRAME_MS / 1000
                )
            except TimeoutError:
                next_tick = loop.time()
                continue
            self._write_bot_audio(
                call,
                pcmu_to_pyvoip_u8(
                    frame, gain=self.settings.realtime_output_gain
                ),
            )
            self.output_frames.task_done()
            next_tick += FRAME_MS / 1000
            await asyncio.sleep(max(0, next_tick - loop.time()))

    async def _watch_call(self, call: AudioCall) -> None:
        while call.state == CallState.ANSWERED:
            await asyncio.sleep(0.1)
        LOG.info("SIP call ended")

    def _write_bot_audio(self, call: AudioCall, audio: bytes) -> None:
        call.write_audio(audio)
        if self.audio_observer is not None:
            self.audio_observer.bot_audio(audio)


async def run_realtime_bridge(
    settings: Settings,
    profile: BotProfile,
    call: AudioCall,
    *,
    monitor: CallMonitor | None = None,
    caller: str = "unknown",
    direction: str = "incoming",
    sip_call_id: str | None = None,
) -> None:
    if monitor is None:
        await RealtimeSIPBridge(settings, profile).run(call)
        return
    async with monitor.track(
        caller=caller,
        direction=direction,
        profile=profile.name,
        sip_call_id=sip_call_id,
    ) as session:
        await RealtimeSIPBridge(settings, profile, session).run(call)
