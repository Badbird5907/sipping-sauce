from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import socket

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when required bridge configuration is missing or invalid."""


def discover_local_ip(remote_host: str, remote_port: int) -> str:
    """Return the local IPv4 address used to reach the SIP server."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((remote_host, remote_port))
        return str(sock.getsockname()[0])


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _integer(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _number(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    openai_api_key: str
    openai_model: str
    openai_voice: str
    openai_safety_identifier: str
    openai_vad_threshold: float
    openai_vad_prefix_padding_ms: int
    openai_vad_silence_duration_ms: int
    sip_server: str
    sip_port: int
    sip_transport: str
    sip_username: str
    sip_password: str
    sip_partyline: str
    sip_local_ip: str
    sip_local_port: int
    sip_rtp_port_low: int
    sip_rtp_port_high: int
    sip_register_timeout: float
    sip_answer_timeout: float
    max_concurrent_calls: int
    reconnect_seconds: float
    log_level: str
    openai_output_gain: float = 2.0

    @classmethod
    def from_env(
        cls,
        env_file: str | None = ".env",
        *,
        sip_username_default: str | None = None,
        sip_transport_default: str = "udp",
        sip_local_port_default: int = 5062,
        sip_rtp_port_low_default: int = 40000,
        sip_rtp_port_high_default: int = 40100,
        max_concurrent_calls_default: int = 1,
    ) -> "Settings":
        if env_file:
            load_dotenv(dotenv_path=env_file, override=True)

        sip_server = _required("SIP_SERVER")
        sip_port = _integer("SIP_PORT", 5060)
        sip_username = os.getenv("SIP_USERNAME", "").strip()
        if not sip_username:
            sip_username = sip_username_default or ""
        if not sip_username:
            raise ConfigurationError("SIP_USERNAME is required")
        local_ip = os.getenv("SIP_LOCAL_IP", "").strip()
        if not local_ip:
            try:
                local_ip = discover_local_ip(sip_server, sip_port)
            except OSError as exc:
                raise ConfigurationError(
                    "Could not auto-detect SIP_LOCAL_IP; set it explicitly"
                ) from exc

        rtp_low = _integer("SIP_RTP_PORT_LOW", sip_rtp_port_low_default)
        rtp_high = _integer("SIP_RTP_PORT_HIGH", sip_rtp_port_high_default)
        if rtp_low > rtp_high:
            raise ConfigurationError(
                "SIP_RTP_PORT_LOW must be less than or equal to SIP_RTP_PORT_HIGH"
            )

        safety_hash = hashlib.sha256(
            f"sip-partyline:{sip_username}".encode("utf-8")
        ).hexdigest()[:32]
        sip_transport = os.getenv(
            "SIP_TRANSPORT", sip_transport_default
        ).strip().lower()
        if sip_transport not in {"udp", "tcp"}:
            raise ConfigurationError("SIP_TRANSPORT must be udp or tcp")
        vad_threshold = _number("OPENAI_VAD_THRESHOLD", 0.75)
        if vad_threshold > 1:
            raise ConfigurationError("OPENAI_VAD_THRESHOLD must be at most 1")

        return cls(
            openai_api_key=_required("OPENAI_API_KEY"),
            openai_model=os.getenv(
                "OPENAI_REALTIME_MODEL", "gpt-realtime-2.1"
            ).strip(),
            openai_voice=os.getenv("OPENAI_VOICE", "marin").strip(),
            openai_safety_identifier=os.getenv(
                "OPENAI_SAFETY_IDENTIFIER", safety_hash
            ).strip(),
            openai_vad_threshold=vad_threshold,
            openai_vad_prefix_padding_ms=_integer(
                "OPENAI_VAD_PREFIX_PADDING_MS", 300, minimum=0
            ),
            openai_vad_silence_duration_ms=_integer(
                "OPENAI_VAD_SILENCE_DURATION_MS", 900, minimum=0
            ),
            sip_server=sip_server,
            sip_port=sip_port,
            sip_transport=sip_transport,
            sip_username=sip_username,
            sip_password=_required("SIP_PASSWORD"),
            sip_partyline=os.getenv("SIP_PARTYLINE", "*99").strip(),
            sip_local_ip=local_ip,
            sip_local_port=_integer("SIP_LOCAL_PORT", sip_local_port_default),
            sip_rtp_port_low=rtp_low,
            sip_rtp_port_high=rtp_high,
            sip_register_timeout=_number("SIP_REGISTER_TIMEOUT", 15),
            sip_answer_timeout=_number("SIP_ANSWER_TIMEOUT", 30),
            max_concurrent_calls=_integer(
                "MAX_CONCURRENT_CALLS", max_concurrent_calls_default
            ),
            reconnect_seconds=_number("RECONNECT_SECONDS", 5),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            openai_output_gain=_number("OPENAI_OUTPUT_GAIN", 2.0),
        )

    def summary(self) -> str:
        return (
            f"SIP/{self.sip_transport.upper()} "
            f"{self.sip_username}@{self.sip_server}:{self.sip_port} "
            f"from {self.sip_local_ip}:{self.sip_local_port}, "
            f"party line {self.sip_partyline}, model {self.openai_model}, "
            f"max calls {self.max_concurrent_calls}"
        )
