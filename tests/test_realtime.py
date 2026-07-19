import asyncio

from pyVoIP.VoIP import CallState

from partyline_llm.config import Settings
from partyline_llm.profiles import PARTYLINE_PROFILE, SPOOKY_PROFILE
from partyline_llm.realtime import (
    CONNECTING_TONE,
    RealtimeSIPBridge,
    greeting_event,
    session_update_event,
)


def settings() -> Settings:
    return Settings(
        openai_api_key="test-key",
        openai_model="gpt-realtime-2.1",
        openai_voice="marin",
        openai_safety_identifier="safe-id",
        openai_vad_threshold=0.75,
        openai_vad_prefix_padding_ms=300,
        openai_vad_silence_duration_ms=900,
        sip_server="10.13.37.10",
        sip_port=5060,
        sip_transport="udp",
        sip_username="199",
        sip_password="secret",
        sip_partyline="*99",
        sip_local_ip="10.13.37.11",
        sip_local_port=5062,
        sip_rtp_port_low=40000,
        sip_rtp_port_high=40100,
        sip_register_timeout=15,
        sip_answer_timeout=30,
        max_concurrent_calls=4,
        reconnect_seconds=5,
        log_level="INFO",
    )


def test_session_uses_telephone_audio_in_both_directions() -> None:
    event = session_update_event(settings(), PARTYLINE_PROFILE)
    session = event["session"]
    assert session["model"] == "gpt-realtime-2.1"
    assert session["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert session["audio"]["output"]["format"] == {"type": "audio/pcmu"}
    assert session["audio"]["output"]["voice"] == "marin"
    assert session["audio"]["input"]["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.75,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 900,
        "create_response": True,
        "interrupt_response": True,
    }


def test_spooky_profile_selects_its_own_voice() -> None:
    event = session_update_event(settings(), SPOOKY_PROFILE)

    assert event["session"]["audio"]["output"]["voice"] == "cedar"


def test_greeting_is_response_instruction() -> None:
    event = greeting_event("Hello.")
    assert event["type"] == "response.create"
    assert "Hello." in event["response"]["instructions"]


def test_audio_queue_does_not_drop_long_responses() -> None:
    async def scenario() -> None:
        bridge = RealtimeSIPBridge(settings(), PARTYLINE_PROFILE)
        for index in range(600):
            await bridge._queue_frame(bytes([index % 256]))
        assert bridge.output_frames.qsize() == 600

    asyncio.run(scenario())


def test_connecting_tone_starts_immediately() -> None:
    class FakeCall:
        state = CallState.ANSWERED

        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def write_audio(self, data: bytes) -> None:
            self.writes.append(data)

    async def scenario() -> None:
        bridge = RealtimeSIPBridge(settings(), PARTYLINE_PROFILE)
        call = FakeCall()
        task = asyncio.create_task(bridge._play_connecting_tone(call))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert call.writes == [CONNECTING_TONE[:160]]

    asyncio.run(scenario())
