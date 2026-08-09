"""Entry point. Orchestration only — no business logic."""

import sys

sys.path.insert(0, "/opt/python")

from processor import process
from shared.logger import get_logger
from validator import validate_records

log = get_logger(__name__)


def handler(event, context):
    """Consume the outbox. Batch size is 1, so one bad message cannot sink a batch.

    Returns partialBatchItemFailures so a failure is redriven rather than
    silently dropped.
    """
    log.info("invoked", extra={"function": getattr(context, "function_name", "local")})
    failures = []

    try:
        records = validate_records(event)
    except ValueError:
        log.exception("malformed event")
        raise

    for message_id, post in records:
        try:
            result = process(post)
            log.info(
                "processed",
                extra={
                    "key": post.idempotency_key,
                    "published": result.published,
                    "shadowed": result.shadowed,
                    "reason": result.reason,
                },
            )
        except ValueError:
            # Invalid content will never become valid. Send it to the DLQ
            # rather than redriving it three times first.
            log.exception("invalid post; routing to DLQ")
            failures.append({"itemIdentifier": message_id})
        except Exception:
            log.exception("publish failed")
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}
