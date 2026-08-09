"""The single dedup surface for this project.

Every guard against posting the same thing twice goes through this module. No
Lambda writes its own dedup marker, and no module-level dict or set is used to
remember what has been sent — module state survives warm invocations, is
invisible to concurrent ones, and vanishes on a cold start. That combination
delivered four duplicate messages to a real family (LL-063).

Here the equivalent failure is a public post, so the ordering rule below is not
a detail: **claim before you act, confirm after.** A crash between the two
leaves a claim that expires, and the work retries. The reverse order would let a
crash produce a marker for a post that never happened, and the post would be
silently dropped forever.
"""

import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

from shared.logger import get_logger

log = get_logger(__name__)

_TTL_SEEN = 90 * 24 * 3600  # transaction markers
_TTL_CLAIM = 15 * 60  # unconfirmed claim; long enough to outlive any retry chain

_table = None


def _get_table() -> Any:
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"])
    return _table


def claim(kind: str, key: str) -> bool:
    """Try to take exclusive ownership of one unit of work.

    Returns True if this caller now owns it, False if it is already claimed or
    already done. A claim that is never confirmed expires after 15 minutes so
    the work is retried rather than lost.
    """
    now = int(time.time())
    try:
        _get_table().put_item(
            Item={"pk": f"{kind}#{key}", "sk": "CLAIM", "claimed_at": now, "ttl": now + _TTL_CLAIM},
            # Take it only if nobody holds it and it was never confirmed.
            ConditionExpression="attribute_not_exists(pk) OR (attribute_not_exists(confirmed_at)"
            " AND #t < :now)",
            ExpressionAttributeNames={"#t": "ttl"},
            ExpressionAttributeValues={":now": now},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.info("claim declined", extra={"kind": kind, "key": key})
            return False
        raise
    return True


def confirm(kind: str, key: str, **evidence: Any) -> None:
    """Mark the work permanently done. Call only after the side effect succeeded.

    `evidence` records what happened (e.g. the tweet id) so a human can audit
    the claim later. Do not pass rendered text — the logger redacts it, and this
    row is the audit trail, not a content store.
    """
    now = int(time.time())
    _get_table().update_item(
        Key={"pk": f"{kind}#{key}", "sk": "CLAIM"},
        UpdateExpression="SET confirmed_at = :now, #t = :ttl" + "".join(
            f", #k{i} = :v{i}" for i in range(len(evidence))
        ),
        ExpressionAttributeNames={
            "#t": "ttl",
            **{f"#k{i}": name for i, name in enumerate(evidence)},
        },
        ExpressionAttributeValues={
            ":now": now,
            ":ttl": now + _TTL_SEEN,
            **{f":v{i}": value for i, value in enumerate(evidence.values())},
        },
    )
    log.info("confirmed", extra={"kind": kind, "key": key})


def is_done(kind: str, key: str) -> bool:
    """True only for work that completed. An unconfirmed claim reads as not done."""
    item = _get_table().get_item(
        Key={"pk": f"{kind}#{key}", "sk": "CLAIM"}, ConsistentRead=True
    ).get("Item")
    return item is not None and "confirmed_at" in item


def release(kind: str, key: str) -> None:
    """Give up a claim after a failure, so the retry doesn't wait out the TTL."""
    _get_table().delete_item(
        Key={"pk": f"{kind}#{key}", "sk": "CLAIM"},
        ConditionExpression="attribute_not_exists(confirmed_at)",
    )
