#!/usr/bin/env python3
"""
Data Miner auto-farmer.

Telegram Mini App: @Datamineer_bot
API: https://tz.tamimdev.dev/api

Credential model (per skill `telegram-miniapp-automation`):
  - the credential is the raw `initData` / `tgWebAppData` query string
    minted by Telegram inside the web-view.
  - it is sent as the `x-telegram-init-data` HTTP header as-is
    (percent-encoded intact — a second decode breaks `hash`).
  - on HTTP 401/403 we mint a fresh initData via Telethon
    `messages.requestWebView` and retry.

This bot:
  1. verifies a fresh initData reaches the API (`/api/auth`),
  2. caches it,
  3. runs a farm loop: claim mining, claim tasks, optionally upgrade level.

Usage:
  python bot.py --phone +62... --otp          # one-time Telegram login
  python bot.py --mint                        # mint + cache initData
  python bot.py --run                         # farm one account
  python bot.py --run-all                     # farm every data/*.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from telethon import TelegramClient, functions, types
from telethon.errors import RPCError, SessionPasswordNeededError
from telethon.utils import get_input_user

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_URL = "https://tz.tamimdev.dev"
API = f"{BASE_URL}/api"
APP_HOST = "tz.tamimdev.dev"
BOT_USERNAME = "Datamineer_bot"
# The referral / startapp parameter supplied with the bot link.
START_PARAM = "DATAHN9SFY"

APP_TITLE = "Data Miner"

ROOT = Path(__file__).resolve().parent
SESSIONS = ROOT / "sessions"
DATA = ROOT / "data"
SESSIONS.mkdir(exist_ok=True)
DATA.mkdir(exist_ok=True)

SESSION_NAME = "data-miner"
ACCOUNT_FILE = DATA / "account.json"

# Auth header discovered from the web bundle.
AUTH_HEADER = "x-telegram-init-data"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("data-miner")


# --------------------------------------------------------------------------- #
# Mint initData with Telethon
# --------------------------------------------------------------------------- #


def _session_path() -> Path:
    return SESSIONS / f"{SESSION_NAME}.session"


async def _login(phone: str, otp_first: bool) -> None:
    """Interactive one-time login; creates the .session file."""
    api_id, api_hash = _default_api_credentials()
    client = TelegramClient(str(_session_path()), api_id=api_id, api_hash=api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        try:
            sent = await client.send_code_request(phone)
            code = input(f"OTP sent to {phone}: ").strip()
            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                pw = input("2FA password: ").strip()
                await client.sign_in(password=pw)
        except RPCError as exc:
            log.error("login failed: %s", exc)
            raise
    me = await client.get_me()
    log.info("logged in as @%s (id %s)", getattr(me, "username", "?"), me.id)
    await client.disconnect()


async def _mint() -> str:
    """Mint a fresh initData via messages.requestWebView and return it raw."""
    api_id, api_hash = _default_api_credentials()
    client = TelegramClient(str(_session_path()), api_id=api_id, api_hash=api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telethon session is not authorized — run --phone first")

    bot_entity = await client.get_entity(BOT_USERNAME)
    # skill fix: requestWebView needs a real InputUser, not InputPeerUser,
    # or Telegram answers BOT_INVALID.
    input_user = get_input_user(bot_entity)

    res = await client(
        functions.messages.RequestWebViewRequest(
            peer=input_user,
            bot=input_user,
            url=f"https://{APP_HOST}/",
            start_param=START_PARAM,
            platform="android",
            theme_params=types.DataJSON(data=json.dumps({"bg_color": "#ffffff"})),
        )
    )
    await client.disconnect()

    url = res.url  # https://host/#tgWebAppData=<encoded>
    # skill parsing rule: extract from the fragment with parse_qs once.
    from urllib.parse import parse_qs, urlparse

    fragment = parse_qs(urlparse(url).fragment)
    init_data = fragment.get("tgWebAppData", [""])[0]
    if not init_data:
        raise RuntimeError("failed to extract tgWebAppData from webview url")
    log.info("minted initData (%d chars)", len(init_data))
    return init_data


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #


@dataclass
class DataMinerClient:
    init_data: str
    session: requests.Session = field(default_factory=requests.Session)
    last_mint: float = field(default_factory=time.monotonic)

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            AUTH_HEADER: self.init_data,
            "Accept": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "User-Agent": "Mozilla/5.0 (Linux; Android 11) Telegram WebView",
        }

    async def _refresh_auth(self) -> None:
        """Throttle-safe refresh: mint on 401/403 (skill pattern)."""
        now = time.monotonic()
        if now - self.last_mint < 60:  # ≥60s throttle
            raise RuntimeError("auth expired and refresh throttled (60s)")
        self.init_data = await _mint()
        self.last_mint = time.monotonic()
        self._cache()
        log.info("auth refreshed")

    def post(self, path: str, payload: Optional[Dict[str, Any]] = None, allow_refresh: bool = True) -> Dict[str, Any]:
        try:
            r = self.session.post(
                f"{API}{path}",
                headers=self._headers(),
                data=json.dumps(payload) if payload is not None else b"",
                timeout=20,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"network error on {path}: {exc}") from exc

        if r.status_code in (401, 403) and allow_refresh:
            log.warning("%s -> %s, refreshing auth", path, r.status_code)
            asyncio.run(self._refresh_auth())
            return self.post(path, payload, allow_refresh=False)

        # API returns JSON even on 4xx; pass through.
        try:
            return r.json()
        except Exception:
            return {"success": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}

    def get(self, path: str) -> Dict[str, Any]:
        r = self.session.get(f"{API}{path}", headers=self._headers(), timeout=20)
        return r.json()

    def auth(self) -> Dict[str, Any]:
        return self.post("/auth")

    def tasks(self) -> Dict[str, Any]:
        return self.get("/tasks")

    def claim_mining(self, amount: float) -> Dict[str, Any]:
        # payload discovered in the frontend bundle:
        #   body: JSON.stringify({claimedAmount: N})
        return self.post("/user/claim-mining", {"claimedAmount": amount})

    def claim_task(self, task_id: int) -> Dict[str, Any]:
        return self.post("/tasks/claim", {"taskId": task_id})

    def upgrade_level(self) -> Dict[str, Any]:
        return self.post("/user/upgrade-level")

    # --- tap mining ----------------------------------------------------- #
    # The on-screen round "DATA" button (the <div> the user pasted) does NOT
    # hit a dedicated tap endpoint. Each tap just adds a fixed 0.0005 to a
    # local `unclaimedMined` counter in the browser and renders a particle;
    # the only network call happens when the user presses "Claim", which posts
    # {claimedAmount: n} to /api/user/claim-mining.
    TAP_VALUE = 0.0005          # = the `5e-4` in the bundle
    CLAIM_THRESHOLD = 0.0001    # button disabled when unclaimedMined <= 1e-4

    def claim_taps(self, taps: int) -> Dict[str, Any]:
        """Claim accumulated tap mining once."""
        amount = round(taps * self.TAP_VALUE, 6)

        if amount <= self.CLAIM_THRESHOLD:
            return {
                "success": False,
                "message": "amount below claim threshold",
                "amount": amount,
                "taps": taps,
            }

        res = self.claim_mining(amount)
        res["tapped_amount"] = amount
        res["taps"] = taps
        return res

    def _cache(self) -> None:
        DATA.mkdir(exist_ok=True)
        with open(ACCOUNT_FILE, "w") as fh:
            json.dump({"initData": self.init_data}, fh, indent=2)
        os.chmod(ACCOUNT_FILE, 0o600)


# --------------------------------------------------------------------------- #
# Farm loop
# --------------------------------------------------------------------------- #


def _load_cached() -> Optional[DataMinerClient]:
    if not ACCOUNT_FILE.exists():
        return None
    try:
        payload = json.loads(ACCOUNT_FILE.read_text())
    except Exception:
        return None
    if not payload.get("initData"):
        return None
    return DataMinerClient(init_data=payload["initData"])


def _default_api_credentials() -> tuple[int, str]:
    """Return Telethon api credentials from env or fall back to Telegram Desktop keys."""

    api_id = int(os.getenv("DM_API_ID", "0") or 0)
    api_hash = os.getenv("DM_API_HASH", "")
    if api_id and api_hash:
        return api_id, api_hash
    # Public Telegram Desktop credentials (safe default for read-only minting).
    return 2040, "b18441a1ff607e10a989891a5462e627"


async def ensure_auth(client: Optional[DataMinerClient]) -> DataMinerClient:
    if client is None:
        init_data = await _mint()
        client = DataMinerClient(init_data=init_data)
        client._cache()

    res = client.auth()
    if not res.get("success"):
        log.warning("cached auth failed (%s), refreshing", res.get("message"))
        await client._refresh_auth()
        res = client.auth()
    if not res.get("success"):
        raise RuntimeError(f"cannot authenticate: {res}")
    return client


def farm_loop(
    client: DataMinerClient,
    do_upgrade: bool = False,
    claim_interval: int = 3600,
) -> None:
    """
    Continuous auto-tap.

    Tap berjalan terus menerus secara lokal.
    Claim dilakukan setiap `claim_interval` detik.

    Default:
        3600 detik = 1 jam
    """

    log.info(
        "starting continuous auto-tap — claim every %s seconds (%.1f minutes)",
        claim_interval,
        claim_interval / 60,
    )

    tap_count = 0
    last_claim = time.monotonic()
    last_status = time.monotonic()

    while True:
        try:
            # --------------------------------------------------------- #
            # Continuous tap
            # --------------------------------------------------------- #

            tap_count += 1

            # Setiap tap = 0.0005
            mined = tap_count * client.TAP_VALUE

            # --------------------------------------------------------- #
            # Status setiap 10 detik
            # --------------------------------------------------------- #

            now = time.monotonic()

            if now - last_status >= 10:
                elapsed = now - last_claim
                remaining = max(0, claim_interval - elapsed)

                log.info(
                    "auto-tap: %s taps | %.6f COIN | claim in %.0fs",
                    tap_count,
                    mined,
                    remaining,
                )

                last_status = now

            # --------------------------------------------------------- #
            # Claim setiap 1 jam
            # --------------------------------------------------------- #

            if now - last_claim >= claim_interval:

                log.info(
                    "1 hour reached — claiming %s taps = %.6f COIN",
                    tap_count,
                    mined,
                )

                # Claim hasil auto-tap
                if tap_count > 0:
                    res = client.claim_taps(tap_count)

                    log.info(
                        "tap claim: %s | taps=%s | amount=%.6f",
                        res.get("message", "ok"),
                        tap_count,
                        mined,
                    )

                # Reset counter setelah claim
                tap_count = 0
                last_claim = time.monotonic()

                # ----------------------------------------------------- #
                # Refresh auth setelah claim
                # ----------------------------------------------------- #

                auth = client.auth()

                if not auth.get("success"):
                    log.warning(
                        "auth failed after claim: %s",
                        auth.get("message"),
                    )
                    time.sleep(5)
                    continue

                user = auth.get("user", {})
                settings = auth.get("settings", {})
                coin = settings.get("coinSymbol", "COIN")

                log.info(
                    "balance=%s %s | level=%s | hashrate=%s",
                    user.get("miningBalance"),
                    coin,
                    user.get("level"),
                    user.get("hashrate"),
                )

                # ----------------------------------------------------- #
                # Optional upgrade
                # ----------------------------------------------------- #

                if do_upgrade:
                    bal = float(user.get("miningBalance") or 0)

                    if bal > 0:
                        res = client.upgrade_level()

                        log.info(
                            "upgrade-level: %s",
                            res.get("message", "ok"),
                        )

            # --------------------------------------------------------- #
            # Tiny yield supaya CPU tidak 100%
            # --------------------------------------------------------- #

            time.sleep(0.001)

        except KeyboardInterrupt:
            log.info("stopped by user")
            break

        except RuntimeError as exc:
            log.error("%s — retry in 10s", exc)
            time.sleep(10)

        except Exception as exc:
            log.exception("unexpected error: %s — retry in 10s", exc)
            time.sleep(10)

# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Data Miner auto-farmer")
    p.add_argument("--phone", help="Telegram phone (+62...) for one-time login")
    p.add_argument("--otp", action="store_true", help="trigger OTP login flow")
    p.add_argument("--mint", action="store_true", help="mint initData then exit")
    p.add_argument("--run", action="store_true", help="farm account.json")
    p.add_argument("--run-all", action="store_true", help="farm data/*.json (sequential)")
    p.add_argument("--no-upgrade", action="store_true", help="skip level upgrades")
    p.add_argument("--tap", action="store_true", help="single auto-tap burst, then exit (use with --taps)")
    p.add_argument("--taps", type=int, default=0, metavar="N",
                   help="taps per round (each tap = 0.0005 mined). 0 disables auto-tap.")
    args = p.parse_args(argv)

    if args.phone:
        asyncio.run(_login(args.phone, args.otp))
        return 0

    if args.mint:
        asyncio.run(_mint())
        return 0

    if args.run:
        client = asyncio.run(ensure_auth(_load_cached()))
        farm_loop(
            client,
            do_upgrade=not args.no_upgrade,
            claim_interval=3600,
        )
        return 0

    # if args.tap:
    #     client = asyncio.run(ensure_auth(_load_cached()))
    #     # verify auth first
    #     auth = client.auth()
    #     if not auth.get("success"):
    #         raise RuntimeError(f"cannot authenticate: {auth.get('message')}")
    #     n = args.taps or 100
    #     res = client.auto_tap(n)
    #     print(json.dumps(res, indent=2))
    #     return 0

    if args.run_all:
        for f in sorted(DATA.glob("*.json")):
            try:
                payload = json.loads(f.read_text())
            except Exception:
                continue

            if not payload.get("initData"):
                continue

            log.info("=== farming %s ===", f.name)

            client = asyncio.run(
                ensure_auth(
                    DataMinerClient(init_data=payload["initData"])
                )
            )

            farm_loop(
                client,
                do_upgrade=not args.no_upgrade,
                claim_interval=3600,
            )

        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
