"""Pure rendering: league data in, post segments out.

No I/O, no clock, no randomness. Given the same input these functions always
produce the same output, which is what makes the public output of this bot
testable. Every function here returns segments that satisfy `text.fits()`, or
raises — it never returns something too long.
"""

from shared.models import (
    MatchupPairing,
    MatchupResult,
    PlayerMove,
    PostType,
    RenderedPost,
    TeamStanding,
    Transaction,
    TransactionType,
)
from shared.text import split_thread


def _fmt_score(value: float) -> str:
    """Fantasy scores are two-decimal. 98.0 reads as unfinished; 98.00 reads as final."""
    return f"{value:.2f}"


def _record(standing: TeamStanding) -> str:
    base = f"{standing.wins}-{standing.losses}"
    return f"{base}-{standing.ties}" if standing.ties else base


def _describe_move(move: PlayerMove) -> str:
    if move.from_team and move.to_team:
        return f"{move.to_team} acquires {move.player_name} from {move.from_team}"
    if move.to_team:
        return f"{move.to_team} adds {move.player_name}"
    if move.from_team:
        return f"{move.from_team} drops {move.player_name}"
    # Yahoo shouldn't produce this, but a move with neither side is not
    # something to guess about — say what is known and nothing more.
    return move.player_name


def render_transaction(txn: Transaction) -> RenderedPost:
    """One transaction, one post (threaded only if a trade is unusually large)."""
    if txn.type is TransactionType.TRADE:
        header = "Trade:"
        # Group by receiving team so both sides of the deal read as sides.
        by_team: dict[str, list[str]] = {}
        for move in txn.moves:
            by_team.setdefault(move.to_team or "—", []).append(move.player_name)
        lines = [f"{team} gets {', '.join(players)}" for team, players in by_team.items()]
    else:
        header = ""
        lines = [_describe_move(m) for m in txn.moves]

    segments = split_thread(lines, header=header)
    return RenderedPost(
        post_type=PostType.TRANSACTION,
        idempotency_key=txn.transaction_key,
        segments=tuple(segments),
    )


def render_scores(week: int, results: list[MatchupResult]) -> RenderedPost:
    lines = []
    for r in results:
        if r.is_tie:
            lines.append(
                f"{r.team_a} {_fmt_score(r.score_a)} — {r.team_b} {_fmt_score(r.score_b)} (TIE)"
            )
            continue
        # Winner first: the result is the story, not Yahoo's pairing order.
        if r.score_a > r.score_b:
            winner, win_score, loser, lose_score = r.team_a, r.score_a, r.team_b, r.score_b
        else:
            winner, win_score, loser, lose_score = r.team_b, r.score_b, r.team_a, r.score_a
        lines.append(
            f"{winner} {_fmt_score(win_score)} def. {loser} {_fmt_score(lose_score)}"
        )
    return RenderedPost(
        post_type=PostType.SCORES,
        idempotency_key=f"scores#{week}",
        segments=tuple(split_thread(lines, header=f"Week {week} final scores")),
    )


def render_standings(week: int, standings: list[TeamStanding]) -> RenderedPost:
    lines = [
        f"{s.rank}. {s.team} {_record(s)} ({_fmt_score(s.points_for)} PF)" for s in standings
    ]
    return RenderedPost(
        post_type=PostType.STANDINGS,
        idempotency_key=f"standings#{week}",
        segments=tuple(split_thread(lines, header=f"Standings after Week {week}")),
    )


def render_matchups(week: int, pairings: list[MatchupPairing]) -> RenderedPost:
    lines = [f"{p.team_a} vs {p.team_b}" for p in pairings]
    return RenderedPost(
        post_type=PostType.MATCHUPS,
        idempotency_key=f"matchups#{week}",
        segments=tuple(split_thread(lines, header=f"Week {week} matchups")),
    )
