"""Reading identity out of a Codex ``auth.json`` blob.

Codex writes one of two shapes into ``auth.json``:

* ChatGPT sign-in — ``tokens`` holds an ``id_token``/``access_token``/
  ``refresh_token`` triple. Email, plan and account id live in the *unverified*
  payload of the ``id_token`` JWT, under the ``https://api.openai.com/auth``
  claim.
* API key — ``OPENAI_API_KEY`` is set and there are no tokens.

The JWT is decoded, never verified: codex-swap only needs the labels it carries
to tell one stored account from another, and the token is one Codex itself
wrote to a 0600 file in the user's own home directory. Nothing here touches the
network, so ``list``/``status`` stay instant and work offline.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from codex_swap.exceptions import AuthParseError
from codex_swap.models import Identity

#: Namespaced claim the OpenAI identity provider puts plan/account metadata in.
_AUTH_CLAIM = "https://api.openai.com/auth"


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Return the payload of a JWT without verifying its signature.

    Returns an empty dict for anything that is not a decodable JWT payload —
    callers treat missing claims as "unknown", never as an error, so that a
    future token shape degrades into a thinner display rather than a crash.
    """
    if not token or not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    segment = parts[1]
    segment += "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(segment.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_auth(text: str) -> Identity:
    """Extract an :class:`Identity` from raw ``auth.json`` text.

    Raises:
        AuthParseError: the text is not JSON, is not an object, or carries no
            usable identity at all (neither an email claim nor an API key).
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise AuthParseError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AuthParseError("expected a JSON object at the top level")
    return identity_from_dict(data)


def identity_from_dict(data: dict) -> Identity:
    """Build an :class:`Identity` from an already-parsed auth mapping."""
    tokens = data.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    api_key = data.get("OPENAI_API_KEY")
    api_key = api_key if isinstance(api_key, str) and api_key else None

    declared_mode = data.get("auth_mode")
    mode = declared_mode or ("apikey" if api_key else "chatgpt")

    claims = decode_jwt_payload(tokens.get("id_token") or "")
    auth_claim = claims.get(_AUTH_CLAIM)
    auth_claim = auth_claim if isinstance(auth_claim, dict) else {}

    email = claims.get("email") or ""
    if not isinstance(email, str):
        email = ""

    account_id = auth_claim.get("chatgpt_account_id") or tokens.get("account_id") or ""
    if not isinstance(account_id, str):
        account_id = ""

    plan = auth_claim.get("chatgpt_plan_type") or ""
    if not isinstance(plan, str):
        plan = ""

    if not email and api_key:
        # API-key blobs carry no identity of their own. The key's tail is
        # stable, non-secret enough to display, and distinguishes two keys.
        email = f"apikey-{api_key[-6:]}"
        plan = plan or "apikey"

    if not email:
        raise AuthParseError(
            "no account identity found (no id_token email claim and no OPENAI_API_KEY)"
        )

    exp = claims.get("exp")
    if not isinstance(exp, int):
        exp = None

    last_refresh = data.get("last_refresh") or ""
    if not isinstance(last_refresh, str):
        last_refresh = ""

    return Identity(
        email=email,
        account_id=account_id,
        plan=plan,
        auth_mode=mode,
        expires_at=exp,
        last_refresh=last_refresh,
    )


def try_parse_auth(text: str | None) -> Identity | None:
    """:func:`parse_auth` that returns None instead of raising.

    Used on display paths, where an unreadable blob should be reported in the
    table rather than aborting the whole command.
    """
    if text is None:
        return None
    try:
        return parse_auth(text)
    except AuthParseError:
        return None
