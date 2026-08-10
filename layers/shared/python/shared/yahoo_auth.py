"""Yahoo OAuth token management.

Access tokens live one hour; the poller runs every two minutes. Refreshing on
every invocation would be 30 needless round trips an hour and would race
itself, so the access token is cached in Parameter Store and refreshed only
near expiry.

Yahoo may rotate the refresh token when it is used. If a rotated token is not
written back, the next refresh fails with an unrecoverable 400 and the bot goes
dark until someone re-authorizes by hand — so the write-back below is not
optional bookkeeping.

That same rotation makes concurrent refreshes dangerous: two invocations
refreshing at once each receive a token, the second invalidating the first, and
whichever write lands last decides whether the stored token is the live one.
Reserved concurrency used to make this impossible, but this account's Lambda
limit leaves no headroom to reserve, so `_refresh` takes a short distributed
lock instead.
"""

import os
import time
from dataclasses import dataclass

import boto3
import requests

from shared.logger import get_logger

log = get_logger(__name__)

TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
REFRESH_MARGIN_SECONDS = 300
_TIMEOUT = (5, 15)


class YahooAuthError(RuntimeError):
    """Refresh failed in a way retrying will not fix. Needs re-authorization."""


@dataclass(frozen=True)
class _Cached:
    access_token: str
    expires_at: int


class TokenManager:
    def __init__(self, prefix: str | None = None, ssm=None):
        self.prefix = prefix or os.environ["YAHOO_PREFIX"]
        self._ssm = ssm or boto3.client("ssm")

    # ------------------------------------------------------------ parameters

    def _get(self, name: str) -> str | None:
        try:
            return self._ssm.get_parameter(Name=f"{self.prefix}/{name}", WithDecryption=True)[
                "Parameter"
            ]["Value"]
        except self._ssm.exceptions.ParameterNotFound:
            return None

    def _put(self, name: str, value: str) -> None:
        self._ssm.put_parameter(
            Name=f"{self.prefix}/{name}", Value=value, Type="SecureString", Overwrite=True
        )

    # ----------------------------------------------------------------- token

    def _cached(self) -> _Cached | None:
        token = self._get("access_token")
        expiry = self._get("access_token_expires_at")
        if not token or not expiry:
            return None
        try:
            return _Cached(token, int(expiry))
        except ValueError:
            # A corrupt expiry is not a reason to use a token of unknown age.
            log.warning("cached expiry unparseable; forcing refresh")
            return None

    def access_token(self, now: int | None = None) -> str:
        now = int(time.time()) if now is None else now
        cached = self._cached()
        if cached and cached.expires_at - REFRESH_MARGIN_SECONDS > now:
            return cached.access_token

        if not self._acquire_refresh_lock(now):
            # Another invocation is refreshing right now. Re-read rather than
            # racing it: a second rotation would invalidate the token it just
            # obtained, and the losing write would leave a dead refresh token.
            fresh = self._cached()
            if fresh and fresh.expires_at > now:
                log.info("used token refreshed by a concurrent invocation")
                return fresh.access_token
            log.warning("refresh lock held but no fresh token yet; refreshing anyway")

        return self._refresh(now)

    @staticmethod
    def _acquire_refresh_lock(now: int) -> bool:
        """Best-effort mutual exclusion over one 60-second window.

        Degrades to True when the state table is unreachable — a refresh that
        might race is better than a bot that cannot authenticate at all.
        """
        try:
            from shared import idempotency

            return idempotency.claim("YAHOO_REFRESH", str(now // 60))
        except Exception:
            log.warning("refresh lock unavailable; proceeding without it")
            return True

    def _refresh(self, now: int) -> str:
        refresh_token = self._get("refresh_token")
        client_id = self._get("client_id")
        client_secret = self._get("client_secret")
        if not (refresh_token and client_id and client_secret):
            raise YahooAuthError("Yahoo credentials missing from Parameter Store")

        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": "oob",
            },
            timeout=_TIMEOUT,
        )
        if response.status_code in (400, 401):
            # Yahoo rejected the refresh token itself. Retrying cannot help.
            raise YahooAuthError(
                f"refresh rejected ({response.status_code}); re-authorization required"
            )
        response.raise_for_status()

        payload = response.json()
        token = payload["access_token"]
        expires_at = now + int(payload.get("expires_in", 3600))

        self._put("access_token", token)
        self._put("access_token_expires_at", str(expires_at))

        rotated = payload.get("refresh_token")
        if rotated and rotated != refresh_token:
            # Yahoo rotated it. Losing this write costs all future access.
            self._put("refresh_token", rotated)
            log.info("refresh token rotated")

        log.info("access token refreshed", extra={"expires_in": expires_at - now})
        return token
