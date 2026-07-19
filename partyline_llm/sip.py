from __future__ import annotations

import asyncio
import logging

from pyVoIP.VoIP import VoIPCall, VoIPPhone

from .config import Settings
from .phone import create_phone, safe_hangup, wait_for_answer, wait_for_registration
from .profiles import BotProfile, PARTYLINE_PROFILE
from .realtime import RealtimeSIPBridge


LOG = logging.getLogger(__name__)


class SIPPartyLineClient:
    def __init__(
        self, settings: Settings, profile: BotProfile = PARTYLINE_PROFILE
    ) -> None:
        self.settings = settings
        self.profile = profile
        self.phone: VoIPPhone | None = None
        self.call: VoIPCall | None = None

    async def run_once(self) -> None:
        self.phone = create_phone(self.settings)

        try:
            LOG.info(
                "Registering SIP endpoint %s with %s:%s",
                self.settings.sip_username,
                self.settings.sip_server,
                self.settings.sip_port,
            )
            self.phone.start()
            await wait_for_registration(
                self.phone, self.settings.sip_register_timeout
            )
            LOG.info("SIP registration complete")

            LOG.info("Calling party line %s", self.settings.sip_partyline)
            self.call = self.phone.call(self.settings.sip_partyline)
            await wait_for_answer(self.call, self.settings.sip_answer_timeout)
            LOG.info("Joined party line %s", self.settings.sip_partyline)

            await RealtimeSIPBridge(self.settings, self.profile).run(self.call)
        finally:
            self.close()

    def close(self) -> None:
        safe_hangup(self.call)
        if self.phone is not None:
            self.phone.stop()
        self.call = None
        self.phone = None


async def run_forever(
    settings: Settings,
    profile: BotProfile = PARTYLINE_PROFILE,
    *,
    once: bool = False,
) -> None:
    while True:
        client = SIPPartyLineClient(settings, profile)
        try:
            await client.run_once()
        except asyncio.CancelledError:
            client.close()
            raise
        except Exception:
            LOG.exception("Bridge session failed")
        if once:
            return
        LOG.info("Reconnecting in %.1f seconds", settings.reconnect_seconds)
        await asyncio.sleep(settings.reconnect_seconds)
