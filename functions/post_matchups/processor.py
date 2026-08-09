"""Publish the pairings for the week now in progress.

This is the job the requirements flagged: it runs the day after scores and
standings, and must not post until Yahoo has actually rolled the week over.
Rather than assume Wednesday noon is late enough, it targets Yahoo's own
`current_week` and refuses to publish a week it has already published.
"""

from dataclasses import dataclass

from shared import idempotency, progress
from shared.logger import get_logger
from shared.outbox import enqueue
from shared.render import render_matchups
from shared.yahoo import YahooClient

log = get_logger(__name__)

KIND = "matchups"


@dataclass(frozen=True)
class ScheduledResult:
    week: int | None = None
    enqueued: bool = False


def process(request, client: YahooClient | None = None) -> ScheduledResult:
    api = client or YahooClient(request.league_key)
    current = api.current_week()

    # Matchups are for the week under way, so the target is current_week
    # itself — not the completed week the Tuesday jobs use.
    if current <= progress.last_week(KIND):
        log.info(
            "matchups already published for this week; deferring",
            extra={"current_week": current, "last_published": progress.last_week(KIND)},
        )
        return ScheduledResult()

    key = f"{KIND}#{current}"
    if not idempotency.claim("ENQUEUE", key):
        log.info("already enqueued", extra={"week": current})
        return ScheduledResult(week=current)

    try:
        enqueue(render_matchups(current, api.matchups(current)), request.outbox_url)
    except Exception:
        idempotency.release("ENQUEUE", key)
        raise

    idempotency.confirm("ENQUEUE", key)
    progress.record_week(KIND, current)
    return ScheduledResult(week=current, enqueued=True)
