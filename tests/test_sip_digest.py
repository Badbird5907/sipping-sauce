from types import SimpleNamespace

from partyline_llm.sip_digest import (
    digest_authorization_value,
    server_observed_address,
)


def test_qop_auth_matches_rfc_2617_example() -> None:
    value = digest_authorization_value(
        username="Mufasa",
        password="Circle Of Life",
        method="GET",
        uri="/dir/index.html",
        challenge={
            "realm": "testrealm@host.com",
            "nonce": "dcd98b7102dd2f0e8b11d0f600bfb0c093",
            "qop": "auth",
            "opaque": "5ccc069c403ebaf9f0171e9517f40e41",
            "algorithm": "MD5",
        },
        cnonce="0a4f113b",
    )

    assert 'response="6629fae49393a05397450978507c4ef1"' in value
    assert "qop=auth" in value
    assert "nc=00000001" in value
    assert 'cnonce="0a4f113b"' in value


def test_server_observed_address_uses_received_and_rport() -> None:
    response = SimpleNamespace(
        headers={
            "Via": [
                {
                    "address": ("192.168.8.163", "5062"),
                    "received": "10.13.37.101",
                    "rport": "5062",
                }
            ]
        }
    )

    assert server_observed_address(response) == ("10.13.37.101", 5062)
