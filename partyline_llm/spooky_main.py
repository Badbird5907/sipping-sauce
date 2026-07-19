from __future__ import annotations

import asyncio
from pathlib import Path

from .cli import build_parser, configure_logging, load_settings_or_exit
from .incoming import run_incoming_forever
from .profiles import SPOOKY_PROFILE


def main() -> None:
    parser = build_parser(
        "Answer calls to SIP extension 666 with a spooky Realtime voice bot.",
        ".env.spooky",
    )
    args = parser.parse_args()
    env_file = args.env_file
    if env_file == ".env.spooky" and not Path(env_file).exists():
        env_file = ".env"
    settings = load_settings_or_exit(
        env_file,
        sip_username_default="666",
        sip_transport_default="tcp",
        sip_local_port_default=5066,
        sip_rtp_port_low_default=41000,
        sip_rtp_port_high_default=41100,
        max_concurrent_calls_default=4,
    )
    configure_logging(settings)
    profile = SPOOKY_PROFILE.configured("SPOOKY", include_generic=False)

    if args.check:
        print(f"Configuration OK: spooky profile, {settings.summary()}")
        return

    try:
        asyncio.run(
            run_incoming_forever(settings, profile, once=args.once)
        )
    except KeyboardInterrupt:
        print("\nThe line has gone quiet.")


if __name__ == "__main__":
    main()
