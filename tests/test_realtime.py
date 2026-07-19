import asyncio

from pyVoIP.VoIP import CallState

from partyline_llm.config import Settings
from partyline_llm.profiles import PARTYLINE_PROFILE, SPOOKY_PROFILE
from partyline_llm.realtime import (
    CONNECTING_TONE,
    RealtimeSIPBridge,
    connection_details,
    greeting_event,
    greeting_events,
    session_update_event,
)


def settings(provider: str = "xai") -> Settings:
    return Settings(
        realtime_provider=provider,
        xai_api_key="xai-test-key",
        xai_model="grok-voice-latest",
        xai_voice="eve",
        openai_api_key="test-key",
        openai_model="gpt-realtime-2.1",
        openai_voice="marin",
        openai_safety_identifier="safe-id",
        realtime_vad_threshold=0.85 if provider == "xai" else 0.75,
        realtime_vad_prefix_padding_ms=333 if provider == "xai" else 300,
        realtime_vad_silence_duration_ms=900,
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
    assert "model" not in session
    assert session["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert session["audio"]["output"]["format"] == {"type": "audio/pcmu"}
    assert session["voice"] == "eve"
    assert session["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.85,
        "prefix_padding_ms": 333,
        "silence_duration_ms": 900,
    }


def test_xai_session_does_not_reuse_openai_profile_voice() -> None:
    event = session_update_event(settings(), SPOOKY_PROFILE)

    assert event["session"]["voice"] == "eve"


def test_openai_realtime_remains_supported() -> None:
    current = settings("openai")
    event = session_update_event(current, SPOOKY_PROFILE)
    session = event["session"]
    url, headers = connection_details(current)

    assert session["model"] == "gpt-realtime-2.1"
    assert session["audio"]["output"]["voice"] == "cedar"
    assert session["audio"]["input"]["turn_detection"]["interrupt_response"]
    assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1"
    assert headers["Authorization"] == "Bearer test-key"


def test_xai_connection_uses_grok_voice_endpoint() -> None:
    url, headers = connection_details(settings())

    assert url == "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
    assert headers == {"Authorization": "Bearer xai-test-key"}


def test_greeting_is_response_instruction() -> None:
    event = greeting_event("Hello.")
    assert event["type"] == "response.create"
    assert "Hello." in event["response"]["instructions"]


def test_xai_greeting_creates_text_turn_then_response() -> None:
    events = greeting_events("xai", "Hello.")

    assert [event["type"] for event in events] == [
        "conversation.item.create",
        "response.create",
    ]
    assert "Hello." in events[0]["item"]["content"][0]["text"]


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
