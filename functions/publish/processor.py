"""Publish one rendered post, or log it and send nothing.

This is the only component that can make something public, so the shadow-mode
check happens here and nowhere else — a producer cannot bypass it, and there is
exactly one line to audit before launch.
"""

import json
import os
import time
from dataclasses import dataclass

import boto3
from shared import idempotency
from shared.logger import get_logger
from shared.models import RenderedPost
from shared.text import fits
from shared.x_client import XClient

log = get_logger(__name__)

_ssm = None
_ddb = None


@dataclass(frozen=True)
class PublishResult:
    published: bool
    shadowed: bool
    tweet_ids: tuple[str, ...] = ()
    reason: str | None = None


def _shadow_mode() -> bool:
    """Read live from Parameter Store, not from an environment variable.

    Going live must be a parameter change, not a redeploy — and coming back
    must be just as fast. Anything other than an explicit "false" is treated as
    shadow: if this value is unreadable or malformed, the safe failure is to
    post nothing.
    """
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    try:
        value = _ssm.get_parameter(Name=os.environ["SHADOW_MODE_PARAM"], WithDecryption=True)[
            "Parameter"
        ]["Value"]
    except Exception:
        log.warning("shadow flag unreadable; defaulting to shadow mode")
        return True
    return value.strip().lower() != "false"


def _record_shadow(post: RenderedPost) -> None:
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    now = int(time.time())
    _ddb.Table(os.environ["STATE_TABLE"]).put_item(
        Item={
            "pk": f"SHADOW#{post.idempotency_key}",
            "sk": str(now),
            # Rendered text lives here, encrypted at rest and TTL'd — never in
            # CloudWatch, where it would sit in plaintext alongside real names.
            "segments": list(post.segments),
            "post_type": post.post_type.value,
            "ttl": now + 30 * 24 * 3600,
        }
    )


def validate(post: RenderedPost) -> None:
    """Reject anything that must not be sent. Raises ValueError."""
    if not post.segments:
        raise ValueError("post has no segments")
    for i, segment in enumerate(post.segments):
        if not segment.strip():
            raise ValueError(f"segment {i} is empty")
        if not fits(segment):
            raise ValueError(f"segment {i} exceeds the character limit")
        if "{" in segment and "}" in segment:
            # An unrendered template placeholder reaching the timeline is the
            # kind of error nobody forgets. Cheap to catch, embarrassing to miss.
            raise ValueError(f"segment {i} contains an unresolved placeholder")


def process(post: RenderedPost, client: XClient | None = None) -> PublishResult:
    validate(post)

    if idempotency.is_done("POST", post.idempotency_key):
        log.info("already published; skipping", extra={"key": post.idempotency_key})
        return PublishResult(published=False, shadowed=False, reason="duplicate")

    if _shadow_mode():
        _record_shadow(post)
        log.info(
            "shadow mode: not sending",
            extra={"key": post.idempotency_key, "segments": len(post.segments)},
        )
        return PublishResult(published=False, shadowed=True)

    if not idempotency.claim("POST", post.idempotency_key):
        log.info(
            "claim declined; another invocation owns this",
            extra={"key": post.idempotency_key},
        )
        return PublishResult(published=False, shadowed=False, reason="claimed_elsewhere")

    tweet_ids: list[str] = []
    api = client or XClient()
    try:
        reply_to = None
        for segment in post.segments:
            reply_to = api.post(segment, in_reply_to=reply_to)
            tweet_ids.append(reply_to)
    except Exception:
        # A partial thread is already public and must never be re-sent from the
        # start. Confirm what went out, then fail so the DLQ and alarm fire.
        if tweet_ids:
            idempotency.confirm(
                "POST", post.idempotency_key, tweet_ids=tweet_ids, partial=True
            )
        else:
            idempotency.release("POST", post.idempotency_key)
        raise

    idempotency.confirm("POST", post.idempotency_key, tweet_ids=tweet_ids)
    _emit_published_metric(len(tweet_ids))
    return PublishResult(published=True, shadowed=False, tweet_ids=tuple(tweet_ids))


def _emit_published_metric(count: int) -> None:
    """EMF — the staleness and spend alarms are built on this metric."""
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "Gotffl",
                            "Dimensions": [[]],
                            "Metrics": [{"Name": "PostsPublished", "Unit": "Count"}],
                        }
                    ],
                },
                "PostsPublished": count,
            }
        )
    )
