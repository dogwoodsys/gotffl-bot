"""X posting client — write-only.

Only POST /2/tweets is implemented. There is no read method, deliberately:
reads cost money, the requirements doc rules them out, and the developer
application stated this account does not read X content. A missing method is a
stronger guarantee than a comment asking people not to call one.
"""

import os
from dataclasses import dataclass

import boto3
from requests_oauthlib import OAuth1Session

from shared.logger import get_logger

log = get_logger(__name__)

TWEETS_URL = "https://api.x.com/2/tweets"
_TIMEOUT = (5, 20)


class XRateLimited(RuntimeError):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"rate limited; retry after {retry_after}s")


class XAuthError(RuntimeError):
    """Credentials rejected or billing lapsed. Retrying will not help."""


@dataclass(frozen=True)
class _Credentials:
    consumer_key: str
    consumer_secret: str
    access_token: str
    access_token_secret: str


class XClient:
    def __init__(self, prefix: str | None = None, ssm=None, session=None):
        self._prefix = prefix or os.environ["X_PREFIX"]
        self._ssm = ssm or boto3.client("ssm")
        self._session = session

    def _load(self) -> _Credentials:
        names = ["consumer_key", "consumer_secret", "access_token", "access_token_secret"]
        response = self._ssm.get_parameters(
            Names=[f"{self._prefix}/{n}" for n in names], WithDecryption=True
        )
        values = {p["Name"].rsplit("/", 1)[1]: p["Value"] for p in response["Parameters"]}
        missing = [n for n in names if n not in values]
        if missing:
            raise XAuthError(f"missing X credentials: {', '.join(missing)}")
        return _Credentials(**values)

    def _oauth(self) -> OAuth1Session:
        if self._session is None:
            creds = self._load()
            self._session = OAuth1Session(
                creds.consumer_key,
                client_secret=creds.consumer_secret,
                resource_owner_key=creds.access_token,
                resource_owner_secret=creds.access_token_secret,
            )
        return self._session

    def post(self, text: str, in_reply_to: str | None = None) -> str:
        """Publish one post. Returns its id.

        The returned id is trusted as proof of publication — the bot never
        reads its own timeline to verify, because reads are billed and the
        response already carries the answer.
        """
        payload: dict = {"text": text}
        if in_reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": in_reply_to}

        response = self._oauth().post(TWEETS_URL, json=payload, timeout=_TIMEOUT)

        if response.status_code == 429:
            raise XRateLimited(int(response.headers.get("retry-after", "900")))
        if response.status_code in (401, 403):
            raise XAuthError(f"X rejected the request ({response.status_code})")
        response.raise_for_status()

        tweet_id = str(response.json()["data"]["id"])
        log.info("posted", extra={"tweet_id": tweet_id, "reply_to": in_reply_to})
        return tweet_id
