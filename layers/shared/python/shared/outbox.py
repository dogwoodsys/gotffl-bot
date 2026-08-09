"""Enqueue rendered posts for the publisher.

Producers call this; only the publisher reads the queue. The FIFO
MessageDeduplicationId is the post's idempotency key, giving a second,
independent guard alongside `shared.idempotency` — SQS drops a duplicate within
its 5-minute window even if the DynamoDB claim was somehow lost.
"""

import json
import os

import boto3

from shared.logger import get_logger
from shared.models import RenderedPost

log = get_logger(__name__)

_client = None


def _sqs():
    global _client
    if _client is None:
        _client = boto3.client("sqs")
    return _client


def enqueue(post: RenderedPost, queue_url: str | None = None) -> str:
    """Send one rendered post. Returns the SQS message id.

    All posts share a single MessageGroupId so the publisher processes them in
    order — a thread's replies must chain to their parent, and two posts racing
    would interleave.
    """
    response = _sqs().send_message(
        QueueUrl=queue_url or os.environ["OUTBOX_URL"],
        MessageBody=json.dumps(post.to_message(), sort_keys=True),
        MessageGroupId="gotffl",
        MessageDeduplicationId=post.idempotency_key,
    )
    log.info(
        "enqueued",
        extra={"post_type": post.post_type.value, "key": post.idempotency_key,
               "segments": len(post.segments)},
    )
    return response["MessageId"]
