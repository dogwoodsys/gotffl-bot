"""Renderers produce the bot's public output. 100% coverage required."""

import pytest
from shared.models import (
    MatchupPairing,
    MatchupResult,
    PlayerMove,
    PostType,
    TeamStanding,
    Transaction,
    TransactionType,
)
from shared.render import render_matchups, render_scores, render_standings, render_transaction
from shared.text import fits


def txn(type_, moves, key="461.l.1.tr.1"):
    return Transaction(transaction_key=key, type=type_, timestamp=1_700_000_000, moves=tuple(moves))


class TestRenderTransaction:
    def test_add(self):
        post = render_transaction(
            txn(TransactionType.ADD, [PlayerMove("Puka Nacua", None, "Team Alpha")])
        )
        assert post.segments == ("Team Alpha adds Puka Nacua",)
        assert post.post_type is PostType.TRANSACTION

    def test_drop(self):
        post = render_transaction(
            txn(TransactionType.DROP, [PlayerMove("Zach Ertz", "Team Beta", None)])
        )
        assert post.segments == ("Team Beta drops Zach Ertz",)

    def test_add_drop_shows_both_moves(self):
        post = render_transaction(
            txn(
                TransactionType.ADD_DROP,
                [
                    PlayerMove("Puka Nacua", None, "Team Alpha"),
                    PlayerMove("Zach Ertz", "Team Alpha", None),
                ],
            )
        )
        body = post.segments[0]
        assert "adds Puka Nacua" in body
        assert "drops Zach Ertz" in body

    def test_trade_groups_by_receiving_team(self):
        post = render_transaction(
            txn(
                TransactionType.TRADE,
                [
                    PlayerMove("Player One", "Team B", "Team A"),
                    PlayerMove("Player Two", "Team B", "Team A"),
                    PlayerMove("Player Three", "Team A", "Team B"),
                ],
            )
        )
        body = post.segments[0]
        assert body.startswith("Trade:")
        assert "Team A gets Player One, Player Two" in body
        assert "Team B gets Player Three" in body

    def test_move_with_neither_side_renders_the_name_only(self):
        """Yahoo shouldn't emit this; if it does, don't invent a team."""
        post = render_transaction(txn(TransactionType.COMMISH, [PlayerMove("Someone", None, None)]))
        assert post.segments == ("Someone",)

    def test_idempotency_key_is_the_transaction_key(self):
        post = render_transaction(
            txn(TransactionType.ADD, [PlayerMove("X", None, "T")], key="461.l.99.tr.42")
        )
        assert post.idempotency_key == "461.l.99.tr.42"

    def test_large_trade_threads_and_every_segment_fits(self):
        moves = [PlayerMove(f"Player Number {i}", "Team B", "Team A") for i in range(1, 15)]
        moves += [PlayerMove(f"Other Player {i}", "Team A", "Team B") for i in range(1, 15)]
        post = render_transaction(txn(TransactionType.TRADE, moves))
        for segment in post.segments:
            assert fits(segment)


class TestRenderScores:
    def test_winner_is_listed_first_regardless_of_pairing_order(self):
        post = render_scores(3, [MatchupResult(3, "Team A", 88.20, "Team B", 121.40)])
        assert post.segments[0].endswith("Team B 121.40 def. Team A 88.20")

    def test_tie_is_marked_not_arbitrarily_ordered(self):
        post = render_scores(3, [MatchupResult(3, "Team A", 100.00, "Team B", 100.00)])
        assert "(TIE)" in post.segments[0]

    def test_scores_always_show_two_decimals(self):
        post = render_scores(1, [MatchupResult(1, "A", 98.0, "B", 90.5)])
        assert "98.00" in post.segments[0]
        assert "90.50" in post.segments[0]

    def test_header_names_the_week(self):
        post = render_scores(7, [MatchupResult(7, "A", 1.0, "B", 2.0)])
        assert post.segments[0].startswith("Week 7 final scores")

    def test_idempotency_key_is_type_and_week(self):
        assert render_scores(9, []).idempotency_key == "scores#9"

    def test_full_twelve_team_week_fits(self):
        results = [
            MatchupResult(10, f"Team Number {i}", 110.25 + i, f"Team Number {i + 6}", 99.75 + i)
            for i in range(1, 7)
        ]
        for segment in render_scores(10, results).segments:
            assert fits(segment)


class TestRenderStandings:
    def test_record_omits_ties_when_zero(self):
        post = render_standings(5, [TeamStanding(1, "A", 4, 1, 0, 500.5, 400.0)])
        assert "4-1 " in post.segments[0]
        assert "4-1-0" not in post.segments[0]

    def test_record_includes_ties_when_present(self):
        post = render_standings(5, [TeamStanding(1, "A", 4, 1, 1, 500.5, 400.0)])
        assert "4-1-1" in post.segments[0]

    def test_rank_order_preserved(self):
        standings = [
            TeamStanding(i, f"Team {i}", 10 - i, i, 0, 100.0 * i, 90.0) for i in range(1, 6)
        ]
        joined = " ".join(render_standings(6, standings).segments)
        assert joined.index("1. Team 1") < joined.index("5. Team 5")

    def test_idempotency_key(self):
        assert render_standings(12, []).idempotency_key == "standings#12"

    @pytest.mark.parametrize("size", [4, 8, 10, 12, 14, 16])
    def test_every_league_size_fits(self, size):
        standings = [
            TeamStanding(i, f"Team Number {i}", 10, 4, 0, 1499.99, 1301.55)
            for i in range(1, size + 1)
        ]
        for segment in render_standings(17, standings).segments:
            assert fits(segment)


class TestRenderMatchups:
    def test_pairings_render_as_vs(self):
        post = render_matchups(4, [MatchupPairing(4, "Team A", "Team B")])
        assert "Team A vs Team B" in post.segments[0]

    def test_header_and_key(self):
        post = render_matchups(4, [MatchupPairing(4, "A", "B")])
        assert post.segments[0].startswith("Week 4 matchups")
        assert post.idempotency_key == "matchups#4"
        assert post.post_type is PostType.MATCHUPS


class TestRoundTrip:
    def test_message_serialisation_survives_sqs(self):
        from shared.models import RenderedPost

        original = render_scores(3, [MatchupResult(3, "A", 1.0, "B", 2.0)])
        assert RenderedPost.from_message(original.to_message()) == original
