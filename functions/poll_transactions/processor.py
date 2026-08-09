"""Find transactions not yet seen and enqueue one post for each."""

from dataclasses import dataclass

from shared import idempotency
from shared.logger import get_logger
from shared.outbox import enqueue
from shared.render import render_transaction
from shared.season import should_poll
from shared.yahoo import YahooClient

log = get_logger(__name__)


@dataclass(frozen=True)
class PollResult:
    skipped_off_season: bool = False
    fetched: int = 0
    enqueued: int = 0


def process(request, client: YahooClient | None = None, now=None) -> PollResult:
    if not should_poll(now):
        return PollResult(skipped_off_season=True)

    api = client or YahooClient(request.league_key)
    transactions = api.transactions()
    log.info("fetched", extra={"source": "yahoo", "count": len(transactions)})

    enqueued = 0
    for txn in transactions:
        # Claim first. If enqueue then fails, the claim is released and the
        # next poll retries — the reverse order would drop it permanently.
        if not idempotency.claim("TXN", txn.transaction_key):
            continue
        try:
            enqueue(render_transaction(txn), request.outbox_url)
        except Exception:
            idempotency.release("TXN", txn.transaction_key)
            raise
        idempotency.confirm("TXN", txn.transaction_key)
        enqueued += 1

    return PollResult(fetched=len(transactions), enqueued=enqueued)
