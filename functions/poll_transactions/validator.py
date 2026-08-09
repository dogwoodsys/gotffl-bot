"""EventBridge delivers a fixed-shape scheduled event; nothing to validate from
the payload. What must be checked is configuration, before any network call."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PollRequest:
    league_key: str
    outbox_url: str


def validate_input(event: dict) -> PollRequest:
    league_key = os.environ.get("YAHOO_LEAGUE_KEY")
    outbox_url = os.environ.get("OUTBOX_URL")
    # Refuse on missing config rather than inventing a default. A silently
    # wrong league key would post another league's transactions.
    if not league_key:
        raise ValueError("YAHOO_LEAGUE_KEY is not configured")
    if not outbox_url:
        raise ValueError("OUTBOX_URL is not configured")
    return PollRequest(league_key=league_key, outbox_url=outbox_url)
