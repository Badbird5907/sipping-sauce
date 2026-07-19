from __future__ import annotations

import asyncio
import logging

from .cli import build_parser, configure_logging, load_settings_or_exit
from .profiles import PARTYLINE_PROFILE
from .recording import CallMonitor
from .sip import run_forever
from .webui import DashboardServer


LOG = logging.getLogger(__name__)


def main() -> None:
    parser = build_parser(
        "Join a SIP party line and bridge it to a Realtime voice agent.", ".env"
    )
    args = parser.parse_args()
    settings = load_settings_or_exit(args.env_file, webui_port_default=8081)
    configure_logging(settings)
    profile = PARTYLINE_PROFILE.configured(
        "PARTYLINE", provider=settings.realtime_provider
    )

    if args.check:
        print(f"Configuration OK: {settings.summary()}")
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
            run_forever(
                settings, profile, once=args.once, monitor=monitor
            )
        )
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        dashboard.close()


if __name__ == "__main__":
    main()
