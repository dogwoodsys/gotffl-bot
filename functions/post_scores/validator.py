"""Configuration validation. Refuses rather than defaulting."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduledRequest:
    league_key: str
    outbox_url: str


def validate_input(event: dict) -> ScheduledRequest:
    league_key = os.environ.get("YAHOO_LEAGUE_KEY")
    outbox_url = os.environ.get("OUTBOX_URL")
    if not league_key:
        raise ValueError("YAHOO_LEAGUE_KEY is not configured")
    if not outbox_url:
        raise ValueError("OUTBOX_URL is not configured")
    return ScheduledRequest(league_key=league_key, outbox_url=outbox_url)
