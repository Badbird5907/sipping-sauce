from __future__ import annotations

import asyncio

from .cli import build_parser, configure_logging, load_settings_or_exit
from .profiles import PARTYLINE_PROFILE
from .sip import run_forever


def main() -> None:
    parser = build_parser(
        "Join a SIP party line and bridge it to a Realtime voice agent.", ".env"
    )
    args = parser.parse_args()
    settings = load_settings_or_exit(args.env_file)
    configure_logging(settings)
    profile = PARTYLINE_PROFILE.configured(
        "PARTYLINE", provider=settings.realtime_provider
    )

    if args.check:
        print(f"Configuration OK: {settings.summary()}")
        return

    try:
        asyncio.run(run_forever(settings, profile, once=args.once))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
