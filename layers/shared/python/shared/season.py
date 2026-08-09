"""Season and hour gating for the transaction poller.

The poller fires every two minutes year-round. Roughly two thirds of those
invocations fall outside the NFL season or in the middle of the night, when no
waiver claim or trade can occur. Returning early costs nothing and removes them
from both the bill and the logs.

All comparisons are in league-local time. A UTC gate would open and close an
hour off for half the season.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

LEAGUE_TZ = ZoneInfo("America/Toronto")

# NFL regular season plus playoffs, with margin on both ends. Deliberately
# generous: a missed transaction is worse than a wasted invocation.
SEASON_START = (8, 15)  # Aug 15 — preseason roster churn
SEASON_END = (1, 15)  # Jan 15 — after fantasy playoffs conclude

# League members are in Ontario. Nothing happens between 2am and 6am.
QUIET_START_HOUR = 2
QUIET_END_HOUR = 6


def now_local() -> datetime:
    return datetime.now(tz=LEAGUE_TZ)


def in_season(moment: datetime | None = None) -> bool:
    moment = moment or now_local()
    local = moment.astimezone(LEAGUE_TZ)
    md = (local.month, local.day)
    # The window wraps the new year, so "in season" is start-or-later OR
    # end-or-earlier — not a single between comparison.
    return md >= SEASON_START or md <= SEASON_END


def in_active_hours(moment: datetime | None = None) -> bool:
    hour = (moment or now_local()).astimezone(LEAGUE_TZ).hour
    return not (QUIET_START_HOUR <= hour < QUIET_END_HOUR)


def should_poll(moment: datetime | None = None) -> bool:
    moment = moment or now_local()
    return in_season(moment) and in_active_hours(moment)
