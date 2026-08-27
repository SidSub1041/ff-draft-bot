"""User accounts: Google sign-in plus Sleeper username linking.

Two honest boundaries, stated up front:

* Google is real OAuth/OIDC - the user authenticates with Google, we verify
  the resulting id_token against Google's tokeninfo endpoint and never see a
  password.  It activates only when the operator has registered an OAuth
  client and set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.
* Sleeper has no OAuth at all.  "Sign in with Sleeper" cannot exist; what we
  offer is *linking* a Sleeper username to an account so drafts can be found
  by it.  It is an identity claim, not authentication, and nothing sensitive
  may ever hang off it.

The browser holds one HttpOnly cookie (ffbot_auth) mapping to a row in
auth_sessions.  Draft sessions stay anonymous as before; an authenticated
user on top of one gets their drafts archived under their account.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request

COOKIE = "ffbot_auth"
STATE_TTL = 600.0

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

# Process-lifetime secret for signing the OAuth state parameter.  Losing it
# on restart only aborts logins that were mid-flight at that moment.
_STATE_KEY = secrets.token_bytes(32)


class AuthError(RuntimeError):
    pass


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def google_configured() -> bool:
    return bool(_env("GOOGLE_CLIENT_ID") and _env("GOOGLE_CLIENT_SECRET"))


# ------------------------------------------------------------------ state


def make_state() -> str:
    """`nonce.ts.sig` - verifiable without server-side storage."""
    nonce = secrets.token_urlsafe(12)
    ts = str(int(time.time()))
    sig = hmac.new(_STATE_KEY, f"{nonce}.{ts}".encode(),
                   hashlib.sha256).hexdigest()[:24]
    return f"{nonce}.{ts}.{sig}"


def check_state(state: str) -> bool:
    try:
        nonce, ts, sig = state.split(".")
        want = hmac.new(_STATE_KEY, f"{nonce}.{ts}".encode(),
                        hashlib.sha256).hexdigest()[:24]
        return (hmac.compare_digest(sig, want)
                and time.time() - float(ts) < STATE_TTL)
    except (ValueError, TypeError):
        return False


# ----------------------------------------------------------------- google


def google_auth_url(redirect_uri: str) -> str:
    q = urllib.parse.urlencode({
        "client_id": _env("GOOGLE_CLIENT_ID"),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": make_state(),
        "prompt": "select_account",
    })
    return f"{_env('FFBOT_GOOGLE_AUTH_URL') or GOOGLE_AUTH_URL}?{q}"


def _post_json(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())


def google_exchange(code: str, redirect_uri: str) -> dict:
    """code -> verified identity {sub, email, name}.

    The id_token is validated by Google's own tokeninfo endpoint (signature,
    expiry) and then locally for audience and issuer - we never trust the
    JWT payload unverified.
    """
    try:
        tok = _post_json(_env("FFBOT_GOOGLE_TOKEN_URL") or GOOGLE_TOKEN_URL, {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": _env("GOOGLE_CLIENT_ID"),
            "client_secret": _env("GOOGLE_CLIENT_SECRET"),
            "redirect_uri": redirect_uri,
        })
    except Exception as e:
        raise AuthError(f"Google token exchange failed: {e}") from e
    id_token = tok.get("id_token")
    if not id_token:
        raise AuthError("Google returned no id_token")
    try:
        info = _get_json(
            (_env("FFBOT_GOOGLE_TOKENINFO_URL") or GOOGLE_TOKENINFO_URL)
            + "?" + urllib.parse.urlencode({"id_token": id_token}))
    except Exception as e:
        raise AuthError(f"Google tokeninfo failed: {e}") from e
    if info.get("aud") != _env("GOOGLE_CLIENT_ID"):
        raise AuthError("id_token audience mismatch")
    if info.get("iss") not in ("https://accounts.google.com",
                               "accounts.google.com"):
        raise AuthError("id_token issuer mismatch")
    sub = str(info.get("sub") or "")
    if not sub:
        raise AuthError("id_token carried no subject")
    return {"sub": sub, "email": str(info.get("email") or ""),
            "name": str(info.get("name") or info.get("email") or "player")}


# ---------------------------------------------------------------- cookies


def parse_cookie(header: str | None) -> str:
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return ""


def cookie_headers(token: str, secure: bool, max_age: int = 90 * 86400
                   ) -> list[tuple[str, str]]:
    bits = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax",
            f"Max-Age={max_age}"]
    if secure:
        bits.append("Secure")
    return [("Set-Cookie", "; ".join(bits))]


def clear_cookie(secure: bool) -> list[tuple[str, str]]:
    return cookie_headers("", secure, max_age=0)
