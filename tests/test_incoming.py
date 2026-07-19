import asyncio

from pyVoIP.VoIP import CallState

from partyline_llm.config import Settings
from partyline_llm.incoming import SIPIncomingClient
from partyline_llm.profiles import SPOOKY_PROFILE


class FakeCall:
    def __init__(self) -> None:
        self.state = CallState.RINGING
        self.denied = False

    def deny(self) -> None:
        self.denied = True
        self.state = CallState.ENDED


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
        sip_transport="tcp",
        sip_username="666",
        sip_password="secret",
        sip_partyline="*99",
        sip_local_ip="10.13.37.11",
        sip_local_port=5066,
        sip_rtp_port_low=41000,
        sip_rtp_port_high=41100,
        sip_register_timeout=15,
        sip_answer_timeout=30,
        max_concurrent_calls=4,
        reconnect_seconds=5,
        log_level="INFO",
    )


def test_second_incoming_call_is_rejected_while_busy() -> None:
    async def scenario() -> None:
        client = SIPIncomingClient(settings(), SPOOKY_PROFILE)
        client._loop = asyncio.get_running_loop()
        client._incoming = asyncio.Queue(maxsize=1)
        first = FakeCall()
        second = FakeCall()

        client._incoming_callback(first)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert await client._incoming.get() is first

        client._incoming_callback(second)  # type: ignore[arg-type]
        assert second.denied

    asyncio.run(scenario())
