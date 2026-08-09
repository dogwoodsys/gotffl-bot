"""Yahoo Fantasy Sports client — read-only.

The application never writes to Yahoo. Only GET is implemented, so a future
change that tries to submit a claim fails loudly instead of quietly doing
something the API registration said this app would not do.

⚠️ PARSING IS UNVERIFIED. Yahoo's JSON is deeply nested and uses numeric string
keys with mixed list/dict collections. The parsers below are written from the
documented shape, NOT from a recorded response — no credentials existed when
they were written, and `testing.md` is explicit that hand-written fixtures for
an idiosyncratic API will lie. Before go-live: capture one real payload per
endpoint, commit it under tests/fixtures/, and re-run these parsers against it.
The shadow week exists partly to catch exactly this.
"""

import os
from typing import Any

import requests

from shared.logger import get_logger
from shared.models import (
    MatchupPairing,
    MatchupResult,
    PlayerMove,
    TeamStanding,
    Transaction,
    TransactionType,
)
from shared.yahoo_auth import TokenManager

log = get_logger(__name__)

BASE_URL = "https://fantasysports.yahooapis.com/fantasy/v2"
_TIMEOUT = (5, 20)


class YahooRateLimited(RuntimeError):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"rate limited; retry after {retry_after}s")


class YahooParseError(ValueError):
    """The payload did not match the expected shape.

    Never swallowed. A parser that returns [] on an unrecognised payload turns
    a schema change into a bot that is silently, healthily posting nothing.
    """


def _walk(node: Any) -> Any:
    """Yield every dict in the tree. Yahoo mixes dicts keyed by numeric strings
    with lists, so parsers must not hard-code container types."""
    if isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key != "count":
                yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _entities(payload: Any, key: str) -> list[Any]:
    """Every value stored under `key`, anywhere in the tree.

    Yahoo nests an entity's fields as *siblings* inside a list — a team is
    `[[{"name": ...}], {"team_standings": ...}]`. Searching for the node that
    contains "team_standings" therefore cannot see the name. Grabbing the whole
    entity subtree and searching inside it can.
    """
    return [node[key] for node in _walk(payload) if isinstance(node, dict) and key in node]


def _first(entity: Any, key: str) -> Any:
    for node in _walk(entity):
        if isinstance(node, dict) and key in node:
            return node[key]
    return None


class YahooClient:
    """Read-only Yahoo Fantasy client bound to one league."""

    def __init__(self, league_key: str | None = None, tokens: TokenManager | None = None):
        self.league_key = league_key or os.environ["YAHOO_LEAGUE_KEY"]
        self._tokens = tokens or TokenManager()

    def _get(self, path: str) -> dict:
        response = requests.get(
            f"{BASE_URL}/{path}",
            params={"format": "json"},
            headers={"Authorization": f"Bearer {self._tokens.access_token()}"},
            timeout=_TIMEOUT,
        )
        if response.status_code == 429:
            raise YahooRateLimited(int(response.headers.get("Retry-After", "60")))
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------- endpoints

    def current_week(self) -> int:
        payload = self._get(f"league/{self.league_key}")
        for node in _walk(payload):
            if isinstance(node, dict) and "current_week" in node:
                return int(node["current_week"])
        raise YahooParseError("current_week not present in league payload")

    def transactions(self) -> list[Transaction]:
        payload = self._get(f"league/{self.league_key}/transactions;types=add,drop,trade")
        out = [self._parse_transaction(entity) for entity in _entities(payload, "transaction")]
        log.info("fetched transactions", extra={"source": "yahoo", "count": len(out)})
        return out

    def scoreboard(self, week: int) -> list[MatchupResult]:
        payload = self._get(f"league/{self.league_key}/scoreboard;week={week}")
        results = []
        for entity in _entities(payload, "matchup"):
            teams = self._teams_with_points(entity)
            if len(teams) != 2:
                continue
            (a_name, a_pts), (b_name, b_pts) = teams
            results.append(MatchupResult(week, a_name, a_pts, b_name, b_pts))
        if not results:
            raise YahooParseError(f"no matchups parsed from scoreboard for week {week}")
        log.info(
            "fetched scoreboard",
            extra={"source": "yahoo", "count": len(results), "week": week},
        )
        return results

    def matchups(self, week: int) -> list[MatchupPairing]:
        payload = self._get(f"league/{self.league_key}/scoreboard;week={week}")
        pairings = []
        for entity in _entities(payload, "matchup"):
            teams = self._teams_with_points(entity)
            if len(teams) != 2:
                continue
            pairings.append(MatchupPairing(week, teams[0][0], teams[1][0]))
        if not pairings:
            raise YahooParseError(f"no pairings parsed for week {week}")
        return pairings

    def standings(self) -> list[TeamStanding]:
        payload = self._get(f"league/{self.league_key}/standings")
        out = []
        for team in _entities(payload, "team"):
            standings = _first(team, "team_standings")
            if standings is None:
                continue
            outcome = standings.get("outcome_totals", {})
            name = _first(team, "name")
            if name is None:
                raise YahooParseError("team name missing from standings entry")
            out.append(
                TeamStanding(
                    rank=int(standings.get("rank", 0)),
                    team=name,
                    wins=int(outcome.get("wins", 0)),
                    losses=int(outcome.get("losses", 0)),
                    ties=int(outcome.get("ties", 0)),
                    points_for=float(standings.get("points_for", 0) or 0),
                    points_against=float(standings.get("points_against", 0) or 0),
                )
            )
        if not out:
            raise YahooParseError("no standings parsed")
        return sorted(out, key=lambda s: s.rank)

    # --------------------------------------------------------------- parsing

    @staticmethod
    def _teams_with_points(matchup: Any) -> list[tuple[str, float]]:
        teams = []
        for team in _entities(matchup, "team"):
            points = _first(team, "team_points")
            name = _first(team, "name")
            if points is None or name is None:
                continue
            total = points.get("total")
            teams.append((str(name), float(total) if total not in (None, "") else 0.0))
        return teams

    @staticmethod
    def _parse_transaction(entity: Any) -> Transaction:
        raw_type = str(_first(entity, "type") or "").lower()
        try:
            txn_type = TransactionType(raw_type)
        except ValueError as exc:
            raise YahooParseError(f"unknown transaction type {raw_type!r}") from exc

        moves = []
        for player in _entities(entity, "player"):
            data = _first(player, "transaction_data")
            if isinstance(data, list):
                data = data[0] if data else {}
            data = data or {}
            name_node = _first(player, "name")
            name = name_node.get("full") if isinstance(name_node, dict) else name_node
            if not name:
                raise YahooParseError("player name missing from transaction")
            moves.append(
                PlayerMove(
                    player_name=str(name),
                    from_team=data.get("source_team_name"),
                    to_team=data.get("destination_team_name"),
                )
            )

        key = _first(entity, "transaction_key")
        if not key:
            raise YahooParseError("transaction_key missing")
        return Transaction(
            transaction_key=str(key),
            type=txn_type,
            timestamp=int(_first(entity, "timestamp") or 0),
            moves=tuple(moves),
        )
