"""Publish final scores for the most recently completed week."""

from dataclasses import dataclass

from shared import idempotency, progress
from shared.logger import get_logger
from shared.outbox import enqueue
from shared.render import render_scores
from shared.yahoo import YahooClient

log = get_logger(__name__)

KIND = "scores"


@dataclass(frozen=True)
class ScheduledResult:
    week: int | None = None
    enqueued: bool = False


def process(request, client: YahooClient | None = None) -> ScheduledResult:
    api = client or YahooClient(request.league_key)

    week = progress.next_target(KIND, api.current_week())
    if week is None:
        return ScheduledResult()

    key = f"{KIND}#{week}"
    if not idempotency.claim("ENQUEUE", key):
        log.info("already enqueued", extra={"week": week})
        return ScheduledResult(week=week)

    try:
        post = render_scores(week, api.scoreboard(week))
        enqueue(post, request.outbox_url)
    except Exception:
        idempotency.release("ENQUEUE", key)
        raise

    idempotency.confirm("ENQUEUE", key)
    progress.record_week(KIND, week)
    return ScheduledResult(week=week, enqueued=True)
