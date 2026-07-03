"""
Tapis token management for the bridge.

Two modes, decided at startup:
  * password grant — TAPIS_CLIENT_BASIC + TAPIS_USERNAME + TAPIS_PASSWORD set:
      the bridge mints its own access token and refreshes it ~5 min before expiry,
      so no manual token is ever needed.
  * manual (fallback) — only TAPIS_TOKEN set: static token, expires on its own.

TAPIS_CLIENT_BASIC is base64("client_id:client_key") of a registered OAuth client
(create one with POST /v3/oauth2/clients). The client bootstraps the whole thing;
after that the bridge is self-sufficient.
"""
import base64
import json
import os
import time

import httpx


def password_grant_configured() -> bool:
    return bool(
        os.environ.get("TAPIS_CLIENT_BASIC")
        and os.environ.get("TAPIS_USERNAME")
        and os.environ.get("TAPIS_PASSWORD")
    )


def token_exp(token: str) -> int:
    """Epoch expiry from a JWT's `exp` claim (0 if unparseable)."""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return int(json.loads(base64.urlsafe_b64decode(seg)).get("exp", 0))
    except Exception:  # noqa: BLE001
        return 0


async def mint_password_token(base_url: str) -> str:
    """Mint an access token via the Tapis password grant."""
    basic = os.environ["TAPIS_CLIENT_BASIC"]
    body = {
        "username": os.environ["TAPIS_USERNAME"],
        "password": os.environ["TAPIS_PASSWORD"],
        "grant_type": "password",
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{base_url}/v3/oauth2/tokens",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/json"},
            json=body,
        )
        r.raise_for_status()
        data = r.json()
    return data["result"]["access_token"]["access_token"]


def seconds_until_refresh(token: str) -> float:
    """How long to sleep before refreshing: 5 min before exp, floor 60s,
    default ~55 min if the token has no readable exp."""
    exp = token_exp(token)
    if not exp:
        return 3300.0
    return max(60.0, exp - time.time() - 300.0)


async def refresh_loop(base_url: str, apply, initial_token: str) -> None:
    """Sleep-then-refresh forever. `apply(token)` installs the new token."""
    import asyncio

    token = initial_token
    while True:
        await asyncio.sleep(seconds_until_refresh(token))
        try:
            token = await mint_password_token(base_url)
            apply(token)
        except Exception as e:  # noqa: BLE001 — keep the loop alive, retry soon
            print(f"[auth] token refresh failed, retrying in 30s: {e}", flush=True)
            await asyncio.sleep(30)
