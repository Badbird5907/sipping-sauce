from __future__ import annotations

import asyncio
from collections.abc import Callable

from pyVoIP.VoIP import CallState, InvalidStateError, VoIPCall, VoIPPhone
from pyVoIP.VoIP.status import PhoneStatus

from .call import AudioCall
from .config import Settings
from .sip_digest import install_digest_auth


def create_phone(
    settings: Settings,
    *,
    call_callback: Callable[[VoIPCall], None] | None = None,
) -> VoIPPhone:
    phone = VoIPPhone(
        server=settings.sip_server,
        port=settings.sip_port,
        username=settings.sip_username,
        password=settings.sip_password,
        myIP=settings.sip_local_ip,
        callCallback=call_callback,
        sipPort=settings.sip_local_port,
        rtpPortLow=settings.sip_rtp_port_low,
        rtpPortHigh=settings.sip_rtp_port_high,
    )
    install_digest_auth(phone.sip)
    return phone


async def wait_for_registration(phone: VoIPPhone, timeout: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        status = phone.get_status()
        if status == PhoneStatus.REGISTERED:
            return
        if status == PhoneStatus.FAILED:
            raise RuntimeError("SIP registration failed")
        await asyncio.sleep(0.1)
    raise TimeoutError(f"SIP registration did not complete within {timeout:g}s")


async def wait_for_answer(call: VoIPCall, timeout: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if call.state == CallState.ANSWERED:
            return
        if call.state == CallState.ENDED:
            raise RuntimeError("Call ended before it was answered")
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Call was not answered within {timeout:g}s")


def safe_hangup(call: AudioCall | None) -> None:
    if call is None or call.state != CallState.ANSWERED:
        return
    try:
        call.hangup()
    except InvalidStateError:
        pass
