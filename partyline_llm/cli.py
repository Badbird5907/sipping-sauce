from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigurationError, Settings


def build_parser(description: str, default_env_file: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--env-file",
        default=default_env_file,
        help=f"environment file to load (default: {default_env_file})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration without registering or placing a call",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one call session and then exit",
    )
    return parser


def load_settings_or_exit(
    env_file: str,
    *,
    sip_username_default: str | None = None,
    sip_transport_default: str = "udp",
    sip_local_port_default: int = 5062,
    sip_rtp_port_low_default: int = 40000,
    sip_rtp_port_high_default: int = 40100,
    max_concurrent_calls_default: int = 1,
) -> Settings:
    try:
        return Settings.from_env(
            env_file,
            sip_username_default=sip_username_default,
            sip_transport_default=sip_transport_default,
            sip_local_port_default=sip_local_port_default,
            sip_rtp_port_low_default=sip_rtp_port_low_default,
            sip_rtp_port_high_default=sip_rtp_port_high_default,
            max_concurrent_calls_default=max_concurrent_calls_default,
        )
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
