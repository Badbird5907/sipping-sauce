from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .cli import build_parser, configure_logging, load_settings_or_exit
from .incoming import run_incoming_forever
from .profiles import RandomProfileCycle, SPOOKY_PROFILES
from .recording import CallMonitor
from .webui import DashboardServer


LOG = logging.getLogger(__name__)


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
        webui_port_default=8080,
    )
    configure_logging(settings)
    profiles = tuple(
        profile.configured(
            "SPOOKY",
            provider=settings.realtime_provider,
            include_generic=False,
        )
        for profile in SPOOKY_PROFILES
    )
    profile_cycle = RandomProfileCycle(profiles)

    if args.check:
        print(
            f"Configuration OK: {len(profiles)} rotating 666 personalities, "
            f"{settings.summary()}"
        )
        return

    monitor = CallMonitor(
        settings.recordings_dir, record=settings.record_calls
    )
    dashboard = DashboardServer(
        monitor, settings.webui_host, settings.webui_port
    )
    if settings.webui_enabled:
        try:
            dashboard.start()
        except OSError:
            LOG.exception("Could not start the call dashboard")

    try:
        asyncio.run(
            run_incoming_forever(
                settings,
                profile_cycle,
                once=args.once,
                monitor=monitor,
            )
        )
    except KeyboardInterrupt:
        print("\nThe line has gone quiet.")
    finally:
        dashboard.close()


if __name__ == "__main__":
    main()
