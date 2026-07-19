import asyncio
import errno
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pyVoIP.VoIP import CallState

from partyline_llm.config import Settings
from partyline_llm.tcp_sip import (
    SIPMessage,
    TCPSIPPhone,
    parse_digest_challenge,
    parse_remote_audio,
    parse_registration_expiry,
    read_sip_message,
)


def settings() -> Settings:
    return Settings(
        realtime_provider="xai",
        xai_api_key="xai-test-key",
        xai_model="grok-voice-latest",
        xai_voice="eve",
        openai_api_key="test-key",
        openai_model="gpt-realtime-2.1",
        openai_voice="marin",
        openai_safety_identifier="safe-id",
        realtime_vad_threshold=0.85,
        realtime_vad_prefix_padding_ms=333,
        realtime_vad_silence_duration_ms=900,
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


def test_parse_digest_challenge() -> None:
    challenge = parse_digest_challenge(
        'Digest realm="asterisk",nonce="abc",algorithm=MD5,qop="auth"'
    )
    assert challenge == {
        "realm": "asterisk",
        "nonce": "abc",
        "algorithm": "MD5",
        "qop": "auth",
    }


def test_parse_remote_pcmu_audio() -> None:
    sdp = (
        "v=0\r\n"
        "c=IN IP4 10.13.37.10\r\n"
        "m=audio 18000 RTP/AVP 0 101\r\n"
    )
    assert parse_remote_audio(sdp) == ("10.13.37.10", 18000)


def test_registration_expiry_prefers_contact_parameter() -> None:
    message = SIPMessage(
        "SIP/2.0 200 OK",
        {
            "contact": ["<sip:666@example>;expires=1800"],
            "expires": ["3600"],
        },
        "",
    )

    assert parse_registration_expiry(message) == 1800


def test_registration_expiry_uses_expires_header_as_fallback() -> None:
    message = SIPMessage(
        "SIP/2.0 200 OK", {"expires": ["900"]}, ""
    )

    assert parse_registration_expiry(message) == 900


def test_read_tcp_sip_message_with_body() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        body = "hello"
        reader.feed_data(
            (
                "MESSAGE sip:666@example SIP/2.0\r\n"
                "Call-ID: abc\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
                f"{body}"
            ).encode()
        )
        reader.feed_eof()
        message = await read_sip_message(reader)
        assert message.method == "MESSAGE"
        assert message.header("call-id") == "abc"
        assert message.body == body

    asyncio.run(scenario())


def test_active_call_count_tracks_answered_dialogs() -> None:
    phone = TCPSIPPhone(settings())
    phone._calls = {
        "a": SimpleNamespace(state=CallState.ANSWERED),
        "b": SimpleNamespace(state=CallState.ANSWERED),
        "c": SimpleNamespace(state=CallState.ENDED),
    }

    assert phone.active_call_count == 2


def test_tcp_connection_falls_back_when_local_port_is_busy() -> None:
    async def scenario() -> None:
        phone = TCPSIPPhone(settings())
        reader = asyncio.StreamReader()
        writer = SimpleNamespace()
        open_connection = AsyncMock(
            side_effect=[
                OSError(errno.EADDRINUSE, "Address already in use"),
                (reader, writer),
            ]
        )

        with patch(
            "partyline_llm.tcp_sip.asyncio.open_connection", open_connection
        ):
            result = await phone._open_connection()

        assert result == (reader, writer)
        assert open_connection.await_args_list[0].kwargs["local_addr"] == (
            "10.13.37.11",
            5066,
        )
        assert open_connection.await_args_list[1].kwargs["local_addr"] == (
            "10.13.37.11",
            0,
        )

    asyncio.run(scenario())


def test_tcp_connection_does_not_hide_other_socket_errors() -> None:
    async def scenario() -> None:
        phone = TCPSIPPhone(settings())
        open_connection = AsyncMock(
            side_effect=OSError(errno.ENETUNREACH, "Network unreachable")
        )

        with (
            patch(
                "partyline_llm.tcp_sip.asyncio.open_connection",
                open_connection,
            ),
            pytest.raises(OSError) as raised,
        ):
            await phone._open_connection()

        assert raised.value.errno == errno.ENETUNREACH
        open_connection.assert_awaited_once()

    asyncio.run(scenario())


def test_next_call_surfaces_background_connection_failure() -> None:
    async def scenario() -> None:
        phone = TCPSIPPhone(settings())

        async def fail_reader() -> None:
            raise ConnectionResetError("PBX closed the connection")

        phone._reader_task = asyncio.create_task(
            fail_reader(), name="sip-tcp-reader"
        )

        with pytest.raises(RuntimeError, match="sip-tcp-reader failed") as raised:
            await phone.next_call()

        assert isinstance(raised.value.__cause__, ConnectionResetError)

    asyncio.run(scenario())


def test_registration_renewal_uses_digest_and_granted_expiry() -> None:
    async def scenario() -> None:
        phone = TCPSIPPhone(settings())
        phone._register_cseq = 2
        phone._register_challenge = {
            "realm": "asterisk",
            "nonce": "abc",
            "algorithm": "MD5",
            "qop": "auth",
        }
        response = SIPMessage(
            "SIP/2.0 200 OK",
            {
                "cseq": ["3 REGISTER"],
                "contact": ["<sip:666@example>;expires=600"],
            },
            "",
        )

        async def send_and_respond(message: str) -> None:
            del message
            phone._register_responses.put_nowait(response)

        phone._send = AsyncMock(  # type: ignore[method-assign]
            side_effect=send_and_respond
        )

        await phone._renew_registration()

        phone._send.assert_awaited_once()
        request = phone._send.await_args.args[0]
        assert "CSeq: 3 REGISTER" in request
        assert "Authorization: Digest" in request
        assert phone._registration_expires == 600

    asyncio.run(scenario())
