"""Identity extraction from auth.json blobs."""

from __future__ import annotations

import base64
import dataclasses
import json

import pytest

from codex_swap.exceptions import AuthParseError
from codex_swap.identity import (
    decode_jwt_payload,
    identity_from_dict,
    parse_auth,
    try_parse_auth,
)
from tests.conftest import make_api_key_auth, make_auth


def test_parses_a_chatgpt_login():
    identity = parse_auth(make_auth("alice@example.com", plan="pro", account_id="acct-1"))
    assert identity.email == "alice@example.com"
    assert identity.plan == "pro"
    assert identity.account_id == "acct-1"
    assert identity.auth_mode == "chatgpt"
    assert identity.is_api_key is False
    assert identity.key == ("alice@example.com", "acct-1")


def test_parses_an_api_key_login():
    identity = parse_auth(make_api_key_auth("sk-proj-XXXXXXbadc0f"))
    assert identity.is_api_key
    assert identity.email == "apikey-badc0f"
    assert identity.plan == "apikey"
    assert identity.expires_at is None


def test_expiry_round_trip():
    fresh = parse_auth(make_auth(expires_in=3600))
    stale = parse_auth(make_auth(expires_in=-3600))
    assert fresh.is_expired() is False
    assert stale.is_expired() is True
    assert fresh.expiry() is not None


def test_missing_exp_is_not_expired():
    """A blob with no expiry claim must not be reported as stale."""
    payload = json.loads(make_auth())
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps({"email": "x@example.com"}).encode())
    payload["tokens"]["id_token"] = f"{header}.{body.decode().rstrip('=')}.sig"
    identity = parse_auth(json.dumps(payload))
    assert identity.expires_at is None
    assert identity.is_expired() is False
    assert identity.expiry() is None


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-jwt",
        "only.two",
        "a.!!!not-base64!!!.c",
        "a." + base64.urlsafe_b64encode(b"not json").decode().rstrip("=") + ".c",
        "a." + base64.urlsafe_b64encode(b'["a list"]').decode().rstrip("=") + ".c",
    ],
)
def test_undecodable_tokens_yield_empty_claims(token):
    """A future token shape must degrade to thinner display, never a crash."""
    assert decode_jwt_payload(token) == {}


@pytest.mark.parametrize(
    "text",
    ["", "not json", "[]", '"a string"', "123", "null"],
)
def test_unparseable_blobs_raise(text):
    with pytest.raises(AuthParseError):
        parse_auth(text)


def test_blob_without_identity_raises():
    with pytest.raises(AuthParseError, match="no account identity"):
        parse_auth(json.dumps({"auth_mode": "chatgpt", "tokens": {}}))


def test_try_parse_returns_none_instead_of_raising():
    assert try_parse_auth("not json") is None
    assert try_parse_auth(None) is None
    assert try_parse_auth(make_auth()) is not None


def test_hostile_types_do_not_crash_the_parser():
    """Every field is defensively typed; a wrong type must not raise TypeError."""
    data = {
        "auth_mode": 42,
        "OPENAI_API_KEY": ["not", "a", "string"],
        "tokens": "not a dict",
        "last_refresh": 12345,
    }
    with pytest.raises(AuthParseError):
        identity_from_dict(data)


def test_non_integer_exp_is_ignored():
    payload = json.loads(make_auth())
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    claims = {"email": "x@example.com", "exp": "soon"}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    payload["tokens"]["id_token"] = f"{header}.{body}.sig"
    assert parse_auth(json.dumps(payload)).expires_at is None


def test_expiry_survives_an_absurd_timestamp():
    payload = json.loads(make_auth())
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    claims = {"email": "x@example.com", "exp": 10**18}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    payload["tokens"]["id_token"] = f"{header}.{body}.sig"
    identity = parse_auth(json.dumps(payload))
    assert identity.expiry() is None  # out of range, reported as unknown
    assert identity.is_expired() is False


def test_declared_auth_mode_wins_over_inference():
    data = json.loads(make_auth())
    data["auth_mode"] = "chatgpt"
    data["OPENAI_API_KEY"] = "sk-also-present"
    assert identity_from_dict(data).auth_mode == "chatgpt"


def test_identity_is_hashable_and_frozen():
    identity = parse_auth(make_auth())
    assert {identity}  # frozen dataclass -> usable as a dict/set key
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.email = "mutated@example.com"


def test_expiry_boundary_is_inclusive():
    """A token expiring exactly now counts as expired, not valid."""
    import datetime as dt

    identity = parse_auth(make_auth(expires_in=0))
    exp = identity.expiry()
    assert identity.is_expired(now=exp) is True
    assert identity.is_expired(now=exp - dt.timedelta(seconds=1)) is False
    assert identity.expires_at is not None
