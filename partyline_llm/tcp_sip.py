from __future__ import annotations

import asyncio
from dataclasses import dataclass
import errno
import logging
import random
import re
import secrets
import socket
import struct
from threading import Lock, Thread
import time
import uuid

from pyVoIP.VoIP import CallState

from .audio import (
    FRAME_BYTES,
    PYVOIP_SILENCE,
    pcmu_to_pyvoip_u8,
    pyvoip_u8_to_pcmu,
)
from .config import Settings
from .profiles import ProfileSource, resolve_profile
from .realtime import run_realtime_bridge
from .recording import CallMonitor
from .sip_digest import digest_authorization_value


LOG = logging.getLogger(__name__)
DEFAULT_REGISTER_EXPIRES = 3600
MAX_REGISTER_REFRESH_SECONDS = 15 * 60
MIN_REGISTER_REFRESH_SECONDS = 5


@dataclass(slots=True)
class SIPMessage:
    start_line: str
    headers: dict[str, list[str]]
    body: str

    def header(self, name: str, default: str = "") -> str:
        values = self.headers.get(name.lower())
        return values[0] if values else default

    @property
    def method(self) -> str:
        return self.start_line.split(" ", 1)[0].upper()

    @property
    def status_code(self) -> int | None:
        if not self.start_line.startswith("SIP/2.0 "):
            return None
        try:
            return int(self.start_line.split(" ", 2)[1])
        except (IndexError, ValueError):
            return None


async def read_sip_message(reader: asyncio.StreamReader) -> SIPMessage:
    while True:
        raw_headers = await reader.readuntil(b"\r\n\r\n")
        if not raw_headers.strip():
            continue
        text = raw_headers.decode("utf-8", errors="replace")
        lines = text[:-4].split("\r\n")
        start_line = lines[0]
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers.setdefault(name.strip().lower(), []).append(value.strip())
        try:
            content_length = int(headers.get("content-length", ["0"])[0])
        except ValueError:
            content_length = 0
        body = ""
        if content_length:
            body = (await reader.readexactly(content_length)).decode(
                "utf-8", errors="replace"
            )
        return SIPMessage(start_line, headers, body)


def parse_digest_challenge(value: str) -> dict[str, str]:
    challenge: dict[str, str] = {}
    for match in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', value):
        challenge[match.group(1).lower()] = match.group(2) or match.group(3)
    return challenge


def parse_observed_address(message: SIPMessage) -> tuple[str, int] | None:
    via = message.header("via")
    received = re.search(r"(?:^|;)received=([^;\s]+)", via, re.I)
    rport = re.search(r"(?:^|;)rport=(\d+)", via, re.I)
    if not received or not rport:
        return None
    return received.group(1), int(rport.group(1))


def parse_registration_expiry(
    message: SIPMessage, default: int = DEFAULT_REGISTER_EXPIRES
) -> int:
    """Return the expiry granted by the registrar, preferring Contact."""
    contact = message.header("contact")
    contact_expiry = re.search(r"(?:^|;)\s*expires\s*=\s*(\d+)", contact, re.I)
    if contact_expiry:
        return max(1, int(contact_expiry.group(1)))
    expires = message.header("expires").strip()
    if expires.isdigit():
        return max(1, int(expires))
    return default


def parse_remote_audio(sdp: str) -> tuple[str, int]:
    connection = re.search(r"^c=IN IP4\s+([^\s]+)", sdp, re.M)
    media = re.search(r"^m=audio\s+(\d+)\s+[^\s]+\s+(.+)$", sdp, re.M)
    if not connection or not media:
        raise RuntimeError("Incoming INVITE did not contain usable IPv4 audio SDP")
    payloads = media.group(2).split()
    if "0" not in payloads:
        raise RuntimeError("Incoming call did not offer G.711 PCMU payload 0")
    return connection.group(1), int(media.group(1))


def _with_tag(to_header: str, tag: str) -> str:
    if re.search(r"(?:^|;)tag=", to_header, re.I):
        return to_header
    return f"{to_header};tag={tag}"


def _contact_uri(value: str, fallback: str) -> str:
    match = re.search(r"<([^>]+)>", value)
    if match:
        return match.group(1)
    return value.split(";", 1)[0].strip() or fallback


class TCPAudioCall:
    def __init__(
        self,
        phone: "TCPSIPPhone",
        invite: SIPMessage,
        rtp_socket: socket.socket,
        remote_audio: tuple[str, int],
        local_tag: str,
        answer_response: str,
    ) -> None:
        self.phone = phone
        self.invite = invite
        self.rtp_socket = rtp_socket
        self.remote_audio = remote_audio
        self.local_tag = local_tag
        self.answer_response = answer_response
        self.call_id = invite.header("call-id")
        self.state = CallState.ANSWERED
        self._running = True
        self._input = bytearray()
        self._output = bytearray()
        self._input_lock = Lock()
        self._output_lock = Lock()
        self._sequence = random.randint(0, 65535)
        self._timestamp = random.randint(0, 2**32 - 1)
        self._ssrc = random.randint(1, 2**32 - 1)
        self._marker = True
        self._receiver = Thread(target=self._receive_rtp, daemon=True)
        self._transmitter = Thread(target=self._transmit_rtp, daemon=True)
        self._receiver.start()
        self._transmitter.start()

    @property
    def local_rtp_port(self) -> int:
        return int(self.rtp_socket.getsockname()[1])

    def read_audio(self, length: int = FRAME_BYTES, blocking: bool = True) -> bytes:
        del blocking
        with self._input_lock:
            chunk = bytes(self._input[:length])
            del self._input[:length]
        return chunk.ljust(length, PYVOIP_SILENCE)

    def write_audio(self, data: bytes) -> None:
        with self._output_lock:
            self._output.extend(data)

    def hangup(self) -> None:
        if self.state != CallState.ANSWERED:
            return
        self.state = CallState.ENDED
        self._stop_media()
        self.phone.schedule_bye(self)

    def remote_hangup(self) -> None:
        self.state = CallState.ENDED
        self._stop_media()

    def _stop_media(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self.rtp_socket.close()
        except OSError:
            pass

    def _receive_rtp(self) -> None:
        self.rtp_socket.settimeout(0.2)
        while self._running:
            try:
                packet, address = self.rtp_socket.recvfrom(8192)
            except TimeoutError:
                continue
            except OSError:
                return
            if len(packet) < 12 or packet[0] >> 6 != 2:
                continue
            payload_type = packet[1] & 0x7F
            if payload_type != 0:
                continue
            csrc_count = packet[0] & 0x0F
            header_length = 12 + (csrc_count * 4)
            if packet[0] & 0x10 and len(packet) >= header_length + 4:
                extension_words = int.from_bytes(
                    packet[header_length + 2 : header_length + 4], "big"
                )
                header_length += 4 + (extension_words * 4)
            payload = packet[header_length:]
            if packet[0] & 0x20 and payload:
                payload = payload[: -payload[-1]]
            if not payload:
                continue
            self.remote_audio = (address[0], address[1])
            with self._input_lock:
                self._input.extend(pcmu_to_pyvoip_u8(payload))

    def _transmit_rtp(self) -> None:
        next_packet = time.perf_counter()
        while self._running:
            with self._output_lock:
                linear = bytes(self._output[:FRAME_BYTES])
                del self._output[:FRAME_BYTES]
            linear = linear.ljust(FRAME_BYTES, PYVOIP_SILENCE)
            payload = pyvoip_u8_to_pcmu(linear)
            marker = 0x80 if self._marker else 0
            header = struct.pack(
                "!BBHII",
                0x80,
                marker | 0,
                self._sequence,
                self._timestamp,
                self._ssrc,
            )
            try:
                self.rtp_socket.sendto(header + payload, self.remote_audio)
            except OSError:
                return
            self._marker = False
            self._sequence = (self._sequence + 1) & 0xFFFF
            self._timestamp = (self._timestamp + FRAME_BYTES) & 0xFFFFFFFF
            next_packet += 0.02
            time.sleep(max(0, next_packet - time.perf_counter()))


class TCPSIPPhone:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.local_ip = settings.sip_local_ip
        self.local_port = settings.sip_local_port
        self.advertised_ip = self.local_ip
        self.advertised_port = self.local_port
        self.instance_id = str(uuid.uuid4())
        self.register_call_id = f"{uuid.uuid4().hex}@{self.local_ip}"
        self.register_tag = uuid.uuid4().hex[:12]
        self._write_lock = asyncio.Lock()
        self._incoming: asyncio.Queue[TCPAudioCall] = asyncio.Queue()
        self._calls: dict[str, TCPAudioCall] = {}
        self._register_responses: asyncio.Queue[SIPMessage] = asyncio.Queue()
        self._register_challenge: dict[str, str] | None = None
        self._register_nonce_count = 0
        self._register_cnonce = ""
        self._register_cseq = 0
        self._registration_expires = DEFAULT_REGISTER_EXPIRES
        self._reader_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._registration_task: asyncio.Task[None] | None = None

    async def connect_and_register(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.reader, self.writer = await self._open_connection()
        socket_info = self.writer.get_extra_info("sockname")
        self.local_ip, self.local_port = socket_info[0], int(socket_info[1])

        self._register_cseq = 1
        await self._send(self._register_message(self._register_cseq))
        first = await asyncio.wait_for(
            self._read_register_response(), self.settings.sip_register_timeout
        )
        observed = parse_observed_address(first)
        if observed:
            self.advertised_ip, self.advertised_port = observed
        if first.status_code == 401:
            self._update_register_challenge(first)
            self._register_cseq += 1
            await self._send(
                self._register_message(
                    self._register_cseq, self._register_authorization()
                )
            )
            final = await asyncio.wait_for(
                self._read_register_response(),
                self.settings.sip_register_timeout,
            )
        else:
            final = first
        if final.status_code != 200:
            raise RuntimeError(
                f"SIP/TCP registration failed: {final.start_line}"
            )

        self._registration_expires = parse_registration_expiry(final)
        LOG.info(
            "SIP/TCP registered %s using persistent connection %s:%s "
            "for %s seconds",
            self.settings.sip_username,
            self.advertised_ip,
            self.advertised_port,
            self._registration_expires,
        )
        self._reader_task = asyncio.create_task(
            self._message_loop(), name="sip-tcp-reader"
        )
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name="sip-tcp-keepalive"
        )
        self._registration_task = asyncio.create_task(
            self._registration_loop(), name="sip-registration-renewal"
        )

    async def _open_connection(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await asyncio.open_connection(
                self.settings.sip_server,
                self.settings.sip_port,
                local_addr=(self.local_ip, self.local_port),
            )
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE or self.local_port == 0:
                raise
            LOG.warning(
                "SIP/TCP local port %s:%s is still in use; "
                "retrying with an OS-assigned port",
                self.local_ip,
                self.local_port,
            )
            return await asyncio.open_connection(
                self.settings.sip_server,
                self.settings.sip_port,
                local_addr=(self.local_ip, 0),
            )

    async def next_call(self) -> TCPAudioCall:
        incoming_task = asyncio.create_task(
            self._incoming.get(), name="next-incoming-sip-call"
        )
        services = tuple(
            task
            for task in (
                self._reader_task,
                self._keepalive_task,
                self._registration_task,
            )
            if task is not None
        )
        try:
            done, _ = await asyncio.wait(
                (incoming_task, *services), return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            incoming_task.cancel()
            await asyncio.gather(incoming_task, return_exceptions=True)
            raise
        stopped = next((task for task in services if task in done), None)
        if stopped is None:
            return incoming_task.result()
        incoming_task.cancel()
        await asyncio.gather(incoming_task, return_exceptions=True)
        if stopped.cancelled():
            raise RuntimeError(f"{stopped.get_name()} stopped unexpectedly")
        error = stopped.exception()
        if error is None:
            raise RuntimeError(f"{stopped.get_name()} stopped unexpectedly")
        raise RuntimeError(f"{stopped.get_name()} failed") from error

    @property
    def active_call_count(self) -> int:
        return sum(
            call.state == CallState.ANSWERED for call in self._calls.values()
        )

    async def close(self) -> None:
        for call in list(self._calls.values()):
            call.remote_hangup()
        self._calls.clear()
        tasks = (
            self._reader_task,
            self._keepalive_task,
            self._registration_task,
        )
        for task in tasks:
            if task:
                task.cancel()
        await asyncio.gather(
            *(task for task in tasks if task),
            return_exceptions=True,
        )
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except (ConnectionError, OSError):
                LOG.debug("SIP/TCP connection was already closed", exc_info=True)

    def schedule_bye(self, call: TCPAudioCall) -> None:
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._send_bye(call))
            )

    async def _message_loop(self) -> None:
        assert self.reader is not None
        while True:
            message = await read_sip_message(self.reader)
            if message.status_code is not None:
                if "REGISTER" in message.header("cseq").upper():
                    self._register_responses.put_nowait(message)
                continue
            if message.method == "INVITE":
                await self._handle_invite(message)
            elif message.method == "ACK":
                continue
            elif message.method == "BYE":
                await self._send(self._simple_response(message, 200, "OK"))
                call = self._calls.pop(message.header("call-id"), None)
                if call:
                    call.remote_hangup()
            elif message.method == "OPTIONS":
                await self._send(self._simple_response(message, 200, "OK"))
            elif message.method == "CANCEL":
                await self._send(self._simple_response(message, 200, "OK"))

    async def _handle_invite(self, invite: SIPMessage) -> None:
        call_id = invite.header("call-id")
        existing = self._calls.get(call_id)
        if existing:
            await self._send(existing.answer_response)
            return

        if self.active_call_count >= self.settings.max_concurrent_calls:
            LOG.warning(
                "Rejecting incoming call: capacity %s reached",
                self.settings.max_concurrent_calls,
            )
            await self._send(self._simple_response(invite, 486, "Busy Here"))
            return

        await self._send(self._simple_response(invite, 100, "Trying", tag=False))
        remote_audio = parse_remote_audio(invite.body)
        rtp_socket = self._bind_rtp_socket()
        local_tag = uuid.uuid4().hex[:12]
        sdp = self._answer_sdp(int(rtp_socket.getsockname()[1]))
        answer = self._response(
            invite,
            200,
            "OK",
            local_tag=local_tag,
            body=sdp,
            content_type="application/sdp",
        )
        call = TCPAudioCall(
            self, invite, rtp_socket, remote_audio, local_tag, answer
        )
        self._calls[call.call_id] = call
        await self._send(answer)
        self._incoming.put_nowait(call)

    def _bind_rtp_socket(self) -> socket.socket:
        for port in range(
            self.settings.sip_rtp_port_low,
            self.settings.sip_rtp_port_high + 1,
        ):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind((self.local_ip, port))
                return sock
            except OSError:
                sock.close()
        raise RuntimeError("No configured RTP port is available")

    async def _send_bye(self, call: TCPAudioCall) -> None:
        try:
            if not self.writer or self.writer.is_closing():
                return
            remote_uri = _contact_uri(
                call.invite.header("contact"),
                f"sip:{call.invite.header('from')}@{self.settings.sip_server}",
            )
            branch = "z9hG4bK" + uuid.uuid4().hex[:20]
            local_identity = _with_tag(
                call.invite.header("to"), call.local_tag
            )
            message = (
                f"BYE {remote_uri} SIP/2.0\r\n"
                f"Via: SIP/2.0/TCP {self.advertised_ip}:{self.advertised_port};"
                f"branch={branch};rport;alias\r\n"
                f"From: {local_identity}\r\n"
                f"To: {call.invite.header('from')}\r\n"
                f"Call-ID: {call.call_id}\r\n"
                "CSeq: 1 BYE\r\n"
                "Max-Forwards: 70\r\n"
                "Content-Length: 0\r\n\r\n"
            )
            await self._send(message)
        finally:
            self._calls.pop(call.call_id, None)

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            await self._send("\r\n")

    async def _registration_loop(self) -> None:
        while True:
            refresh_seconds = max(
                MIN_REGISTER_REFRESH_SECONDS,
                min(
                    self._registration_expires / 2,
                    MAX_REGISTER_REFRESH_SECONDS,
                ),
            )
            LOG.debug(
                "Refreshing SIP registration in %.1f seconds", refresh_seconds
            )
            await asyncio.sleep(refresh_seconds)
            await self._renew_registration()

    async def _renew_registration(self) -> None:
        while True:
            try:
                self._register_responses.get_nowait()
            except asyncio.QueueEmpty:
                break

        self._register_cseq += 1
        authorization = (
            self._register_authorization()
            if self._register_challenge is not None
            else None
        )
        await self._send(
            self._register_message(self._register_cseq, authorization)
        )
        response = await asyncio.wait_for(
            self._register_responses.get(), self.settings.sip_register_timeout
        )
        if response.status_code == 401:
            self._update_register_challenge(response)
            self._register_cseq += 1
            await self._send(
                self._register_message(
                    self._register_cseq, self._register_authorization()
                )
            )
            response = await asyncio.wait_for(
                self._register_responses.get(),
                self.settings.sip_register_timeout,
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"SIP/TCP registration renewal failed: {response.start_line}"
            )
        self._registration_expires = parse_registration_expiry(response)
        LOG.info(
            "SIP/TCP registration renewed for %s seconds",
            self._registration_expires,
        )

    def _update_register_challenge(self, response: SIPMessage) -> None:
        self._register_challenge = parse_digest_challenge(
            response.header("www-authenticate")
        )
        self._register_nonce_count = 0
        self._register_cnonce = secrets.token_hex(8)

    def _register_authorization(self) -> str:
        if self._register_challenge is None:
            raise RuntimeError("SIP registrar did not provide a digest challenge")
        self._register_nonce_count += 1
        return digest_authorization_value(
            username=self.settings.sip_username,
            password=self.settings.sip_password,
            method="REGISTER",
            uri=f"sip:{self.settings.sip_server}",
            challenge=self._register_challenge,
            nonce_count=self._register_nonce_count,
            cnonce=self._register_cnonce,
        )

    async def _read_register_response(self) -> SIPMessage:
        assert self.reader is not None
        while True:
            message = await read_sip_message(self.reader)
            if message.status_code is not None and "REGISTER" in message.header(
                "cseq"
            ).upper():
                return message

    async def _send(self, message: str) -> None:
        if not self.writer:
            raise RuntimeError("SIP/TCP connection is not open")
        async with self._write_lock:
            self.writer.write(message.encode("utf-8"))
            await self.writer.drain()

    def _register_message(
        self, cseq: int, authorization: str | None = None
    ) -> str:
        branch = "z9hG4bK" + uuid.uuid4().hex[:20]
        contact = (
            f"<sip:{self.settings.sip_username}@{self.advertised_ip}:"
            f"{self.advertised_port};transport=tcp;ob>;reg-id=1;"
            f'+sip.instance="<urn:uuid:{self.instance_id}>"'
        )
        auth_header = (
            f"Authorization: {authorization}\r\n" if authorization else ""
        )
        return (
            f"REGISTER sip:{self.settings.sip_server} SIP/2.0\r\n"
            f"Via: SIP/2.0/TCP {self.advertised_ip}:{self.advertised_port};"
            f"branch={branch};rport;alias\r\n"
            f"From: <sip:{self.settings.sip_username}@{self.settings.sip_server}>;"
            f"tag={self.register_tag}\r\n"
            f"To: <sip:{self.settings.sip_username}@{self.settings.sip_server}>\r\n"
            f"Call-ID: {self.register_call_id}\r\n"
            f"CSeq: {cseq} REGISTER\r\n"
            f"Contact: {contact}\r\n"
            "Max-Forwards: 70\r\n"
            "Supported: outbound, path\r\n"
            "Expires: 3600\r\n"
            f"{auth_header}"
            "User-Agent: partyline-llm/0.1\r\n"
            "Content-Length: 0\r\n\r\n"
        )

    def _simple_response(
        self,
        request: SIPMessage,
        code: int,
        reason: str,
        *,
        tag: bool = True,
    ) -> str:
        return self._response(
            request,
            code,
            reason,
            local_tag=uuid.uuid4().hex[:12] if tag else None,
        )

    def _response(
        self,
        request: SIPMessage,
        code: int,
        reason: str,
        *,
        local_tag: str | None,
        body: str = "",
        content_type: str | None = None,
    ) -> str:
        lines = [f"SIP/2.0 {code} {reason}"]
        lines.extend(f"Via: {value}" for value in request.headers.get("via", []))
        lines.append(f"From: {request.header('from')}")
        to_header = request.header("to")
        if local_tag:
            to_header = _with_tag(to_header, local_tag)
        lines.extend(
            [
                f"To: {to_header}",
                f"Call-ID: {request.header('call-id')}",
                f"CSeq: {request.header('cseq')}",
                f"Contact: <sip:{self.settings.sip_username}@"
                f"{self.advertised_ip}:{self.advertised_port};transport=tcp>",
                "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS",
            ]
        )
        if content_type:
            lines.append(f"Content-Type: {content_type}")
        lines.append(f"Content-Length: {len(body.encode('utf-8'))}")
        return "\r\n".join(lines) + "\r\n\r\n" + body

    def _answer_sdp(self, rtp_port: int) -> str:
        session_id = random.randint(1, 2**31)
        return (
            "v=0\r\n"
            f"o=partyline {session_id} {session_id} IN IP4 {self.advertised_ip}\r\n"
            "s=partyline-llm\r\n"
            f"c=IN IP4 {self.advertised_ip}\r\n"
            "t=0 0\r\n"
            f"m=audio {rtp_port} RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )


async def run_tcp_incoming_forever(
    settings: Settings,
    profile: ProfileSource,
    *,
    once: bool = False,
    monitor: CallMonitor | None = None,
) -> None:
    async def serve_call(phone: TCPSIPPhone, call: TCPAudioCall) -> None:
        selected_profile = resolve_profile(profile)
        LOG.info(
            "Starting call %s as %s (%s/%s active)",
            call.call_id,
            selected_profile.name,
            phone.active_call_count,
            settings.max_concurrent_calls,
        )
        try:
            from_header = call.invite.header("from")
            caller_match = re.search(r"sip:([^@;>]+)", from_header, re.I)
            caller = caller_match.group(1) if caller_match else from_header
            await run_realtime_bridge(
                settings,
                selected_profile,
                call,
                monitor=monitor,
                caller=caller or "unknown",
                direction="incoming",
                sip_call_id=call.call_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("SIP/TCP call session failed: %s", call.call_id)
        finally:
            call.hangup()
            LOG.info("Call %s ended", call.call_id)

    while True:
        phone = TCPSIPPhone(settings)
        sessions: set[asyncio.Task[None]] = set()
        try:
            LOG.info(
                "Connecting SIP/TCP endpoint %s to %s:%s",
                settings.sip_username,
                settings.sip_server,
                settings.sip_port,
            )
            await phone.connect_and_register()
            LOG.info(
                "%s is waiting for incoming TCP calls (max %s)",
                settings.sip_username,
                settings.max_concurrent_calls,
            )
            while True:
                call = await phone.next_call()
                if once:
                    await serve_call(phone, call)
                    return
                task = asyncio.create_task(
                    serve_call(phone, call),
                    name=f"sip-call-{call.call_id}",
                )
                sessions.add(task)
                task.add_done_callback(sessions.discard)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("SIP/TCP service failed")
        finally:
            for task in sessions:
                task.cancel()
            await asyncio.gather(*sessions, return_exceptions=True)
            await phone.close()
        if once:
            return
        LOG.info("Reconnecting SIP/TCP in %.1f seconds", settings.reconnect_seconds)
        await asyncio.sleep(settings.reconnect_seconds)
