"""Validate the SQS event envelope before any business logic runs."""

import json

from shared.models import RenderedPost


def validate_records(event: dict) -> list[tuple[str, RenderedPost]]:
    """Return (messageId, post) pairs. Raises ValueError on a malformed event."""
    records = event.get("Records")
    if not isinstance(records, list) or not records:
        raise ValueError("event contains no Records")

    out = []
    for record in records:
        message_id = record.get("messageId")
        if not message_id:
            raise ValueError("record missing messageId")
        try:
            body = json.loads(record["body"])
        except (KeyError, json.JSONDecodeError) as exc:
            raise ValueError(f"record {message_id} has an unparseable body") from exc
        try:
            out.append((message_id, RenderedPost.from_message(body)))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"record {message_id} is not a rendered post") from exc
    return out
