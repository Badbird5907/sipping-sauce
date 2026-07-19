from __future__ import annotations

import hashlib
import logging
import re
import secrets
from types import MethodType
from typing import Any, Callable

from pyVoIP import RTP
from pyVoIP.SIP import SIPMessage, SIPStatus


LOG = logging.getLogger(__name__)


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def digest_authorization_value(
    *,
    username: str,
    password: str,
    method: str,
    uri: str,
    challenge: dict[str, Any],
    nonce_count: int = 1,
    cnonce: str | None = None,
) -> str:
    """Build an RFC 2617/7616 MD5 Digest Authorization value."""
    realm = str(challenge["realm"])
    nonce = str(challenge["nonce"])
    algorithm = str(challenge.get("algorithm", "MD5")).upper()
    if algorithm != "MD5":
        raise RuntimeError(f"Unsupported SIP digest algorithm: {algorithm}")

    qop_options = [
        item.strip().lower()
        for item in str(challenge.get("qop", "")).split(",")
        if item.strip()
    ]
    qop = "auth" if "auth" in qop_options else None
    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")

    fields = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
    ]
    if qop:
        client_nonce = cnonce or secrets.token_hex(8)
        nc = f"{nonce_count:08x}"
        response = _md5(f"{ha1}:{nonce}:{nc}:{client_nonce}:{qop}:{ha2}")
        fields.extend(
            [
                f'response="{response}"',
                "algorithm=MD5",
                f"qop={qop}",
                f"nc={nc}",
                f'cnonce="{client_nonce}"',
            ]
        )
    else:
        response = _md5(f"{ha1}:{nonce}:{ha2}")
        fields.extend([f'response="{response}"', "algorithm=MD5"])

    opaque = challenge.get("opaque")
    if opaque:
        fields.append(f'opaque="{opaque}"')
    return "Digest " + ",".join(fields)


def _replace_authorization(message: str, authorization: str) -> str:
    replacement = f"Authorization: {authorization}\r\n"
    updated, count = re.subn(
        r"Authorization: Digest .*?\r\n", replacement, message, count=1
    )
    if count != 1:
        raise RuntimeError("PyVoIP REGISTER did not contain an Authorization header")
    return updated


def server_observed_address(request: SIPMessage) -> tuple[str, int] | None:
    """Read the public/reflexive SIP address Asterisk put in the Via response."""
    via_headers = request.headers.get("Via", [])
    if not via_headers:
        return None
    via = via_headers[0]
    received = via.get("received")
    rport = via.get("rport")
    if not received or rport in {None, ""}:
        return None
    try:
        return str(received), int(rport)
    except (TypeError, ValueError):
        return None


def install_digest_auth(sip_client: Any) -> None:
    """Patch a PyVoIP SIPClient with qop-aware REGISTER and INVITE auth."""
    original_gen_register: Callable[..., str] = sip_client.gen_register

    def gen_register(this: Any, request: SIPMessage, deregister: bool = False) -> str:
        observed = server_observed_address(request)
        if observed is not None and observed != (this.myIP, this.myPort):
            old_address = (this.myIP, this.myPort)
            this.myIP, this.myPort = observed
            LOG.info(
                "SIP server sees this endpoint as %s:%s; advertising that "
                "address instead of %s:%s",
                observed[0],
                observed[1],
                old_address[0],
                old_address[1],
            )
        message = original_gen_register(request, deregister)
        uri = message.split(" ", 2)[1]
        authorization = digest_authorization_value(
            username=this.username,
            password=this.password,
            method="REGISTER",
            uri=uri,
            challenge=request.authentication,
        )
        return _replace_authorization(message, authorization)

    def invite(
        this: Any,
        number: str,
        ms: dict[int, dict[str, RTP.PayloadType]],
        sendtype: RTP.TransmitType,
    ) -> tuple[SIPMessage, str, int]:
        branch = "z9hG4bK" + this.gen_call_id()[0:25]
        call_id = this.gen_call_id()
        sess_id = this.sessID.next()
        message = this.gen_invite(
            number, str(sess_id), ms, sendtype, branch, call_id
        )

        with this.recvLock:
            this.out.sendto(message.encode("utf-8"), (this.server, this.port))
            response = SIPMessage(this.s.recv(8192))
            while (
                response.status not in {
                    SIPStatus(401),
                    SIPStatus(100),
                    SIPStatus(180),
                }
                or response.headers["Call-ID"] != call_id
            ):
                if not this.NSD:
                    break
                this.parse_message(response)
                response = SIPMessage(this.s.recv(8192))

            if response.status in {SIPStatus(100), SIPStatus(180)}:
                return SIPMessage(message.encode("utf-8")), call_id, sess_id

            ack = this.gen_ack(response)
            this.out.sendto(ack.encode("utf-8"), (this.server, this.port))

            uri = f"sip:{number}@{this.server}"
            authorization = digest_authorization_value(
                username=this.username,
                password=this.password,
                method="INVITE",
                uri=uri,
                challenge=response.authentication,
            )
            message = this.gen_invite(
                number, str(sess_id), ms, sendtype, branch, call_id
            )
            message = message.replace(
                "\r\nContent-Length",
                f"\r\nAuthorization: {authorization}\r\nContent-Length",
            )
            this.out.sendto(message.encode("utf-8"), (this.server, this.port))
            return SIPMessage(message.encode("utf-8")), call_id, sess_id

    sip_client.gen_register = MethodType(gen_register, sip_client)
    sip_client.invite = MethodType(invite, sip_client)
