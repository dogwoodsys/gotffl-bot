"""How far each scheduled post type has got.

The Tuesday/Wednesday jobs need to know which week to post about, and Yahoo's
`current_week` alone cannot tell them: at 6am Tuesday, Yahoo may or may not
have already rolled the week over. Guessing produces one of two silent
failures — posting last week's scores twice, or skipping a week entirely.

So each post type records the last week it published, and derives its target as
"the next one". A week is only published once Yahoo has visibly moved past it,
which makes the rollover question answerable instead of assumed.
"""

import os

import boto3

from shared.logger import get_logger

log = get_logger(__name__)

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"])
    return _table


def last_week(kind: str) -> int:
    """The last week published for `kind`, or 0 if none."""
    item = _get_table().get_item(
        Key={"pk": f"PROGRESS#{kind}", "sk": "WEEK"}, ConsistentRead=True
    ).get("Item")
    return int(item["week"]) if item else 0


def record_week(kind: str, week: int) -> None:
    """Advance the pointer. Never moves backwards — a stale retry that tried to
    rewind would make the next run re-post a week that already went out."""
    from botocore.exceptions import ClientError

    try:
        _get_table().put_item(
            Item={"pk": f"PROGRESS#{kind}", "sk": "WEEK", "week": week},
            ConditionExpression="attribute_not_exists(pk) OR week < :w",
            ExpressionAttributeValues={":w": week},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        log.info("progress pointer already at or past week", extra={"kind": kind, "week": week})


def next_target(kind: str, current_week: int) -> int | None:
    """The week `kind` should publish now, or None if it isn't ready.

    Ready means Yahoo has moved past the target week — `current_week` is
    strictly greater. At 6am Tuesday before Yahoo rolls over, this returns None
    and the job no-ops; the afternoon retry picks it up.
    """
    target = last_week(kind) + 1
    if current_week > target:
        return target
    log.info(
        "week not complete in Yahoo yet; deferring",
        extra={"kind": kind, "target": target, "current_week": current_week},
    )
    return None
