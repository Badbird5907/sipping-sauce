from __future__ import annotations

import asyncio
import logging
from threading import Lock

from pyVoIP.VoIP import CallState, InvalidStateError, VoIPCall, VoIPPhone

from .config import Settings
from .phone import create_phone, safe_hangup, wait_for_registration
from .profiles import BotProfile
from .realtime import RealtimeSIPBridge


LOG = logging.getLogger(__name__)


class SIPIncomingClient:
    """Register a SIP endpoint and bridge one incoming call at a time."""

    def __init__(self, settings: Settings, profile: BotProfile) -> None:
        self.settings = settings
        self.profile = profile
        self.phone: VoIPPhone | None = None
        self.call: VoIPCall | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._incoming: asyncio.Queue[VoIPCall] | None = None
        self._busy = False
        self._busy_lock = Lock()

    def _incoming_callback(self, call: VoIPCall) -> None:
        """Called by PyVoIP on a timer thread when an INVITE arrives."""
        with self._busy_lock:
            if self._busy:
                try:
                    if call.state == CallState.RINGING:
                        call.deny()
                except InvalidStateError:
                    pass
                return
            self._busy = True

        if self._loop is None:
            self._release_busy()
            return
        self._loop.call_soon_threadsafe(self._offer_call, call)

    def _offer_call(self, call: VoIPCall) -> None:
        if self._incoming is None:
            self._reject(call)
            return
        try:
            self._incoming.put_nowait(call)
        except asyncio.QueueFull:
            self._reject(call)

    def _reject(self, call: VoIPCall) -> None:
        try:
            if call.state == CallState.RINGING:
                call.deny()
        except InvalidStateError:
            pass
        self._release_busy()

    def _release_busy(self) -> None:
        with self._busy_lock:
            self._busy = False

    async def run(self, *, once: bool = False) -> None:
        self._loop = asyncio.get_running_loop()
        self._incoming = asyncio.Queue(maxsize=1)
        self.phone = create_phone(
            self.settings, call_callback=self._incoming_callback
        )

        try:
            LOG.info(
                "Registering incoming SIP endpoint %s with %s:%s",
                self.settings.sip_username,
                self.settings.sip_server,
                self.settings.sip_port,
            )
            self.phone.start()
            await wait_for_registration(
                self.phone, self.settings.sip_register_timeout
            )
            LOG.info(
                "%s is waiting for an incoming call",
                self.settings.sip_username,
            )

            while True:
                self.call = await self._incoming.get()
                try:
                    if self.call.state != CallState.RINGING:
                        continue
                    caller = self.call.request.headers.get("From", {}).get(
                        "number", "unknown"
                    )
                    LOG.info("Answering incoming call from %s", caller)
                    self.call.answer()
                    await RealtimeSIPBridge(
                        self.settings, self.profile
                    ).run(self.call)
                except Exception:
                    LOG.exception("Incoming call session failed")
                finally:
                    safe_hangup(self.call)
                    self.call = None
                    self._incoming.task_done()
                    self._release_busy()
                if once:
                    return
                LOG.info("Waiting for the next call to %s", self.settings.sip_username)
        finally:
            self.close()

    def close(self) -> None:
        safe_hangup(self.call)
        if self.phone is not None:
            self.phone.stop()
        self.call = None
        self.phone = None
        self._release_busy()


async def run_incoming_forever(
    settings: Settings, profile: BotProfile, *, once: bool = False
) -> None:
    if settings.sip_transport == "tcp":
        from .tcp_sip import run_tcp_incoming_forever

        await run_tcp_incoming_forever(settings, profile, once=once)
        return

    while True:
        client = SIPIncomingClient(settings, profile)
        try:
            await client.run(once=once)
        except asyncio.CancelledError:
            client.close()
            raise
        except Exception:
            LOG.exception("Incoming SIP service failed")
        if once:
            return
        LOG.info("Re-registering in %.1f seconds", settings.reconnect_seconds)
        await asyncio.sleep(settings.reconnect_seconds)
