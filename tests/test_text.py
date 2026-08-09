"""Length and threading. 100% coverage required — this is what makes a post
too long or correctly split, and it runs on every public post the bot sends."""

import pytest
from shared.text import MAX_WEIGHTED, SegmentTooLong, fits, split_thread, weighted_length


class TestWeightedLength:
    def test_ascii_weighs_one_each(self):
        assert weighted_length("hello") == 5

    def test_empty(self):
        assert weighted_length("") == 0

    @pytest.mark.parametrize("char", ["漢", "あ", "한", "："])
    def test_cjk_and_fullwidth_weigh_two(self, char):
        assert weighted_length(char) == 2

    def test_emoji_weighs_two(self):
        assert weighted_length("🏈") == 2

    def test_combining_mark_is_free(self):
        # "é" as e + combining acute normalizes to one precomposed char.
        assert weighted_length("é") == 1

    def test_zwj_sequence_is_not_counted_per_component(self):
        # A ZWJ family emoji must not be billed as four separate emoji.
        assert weighted_length("👨‍👩‍👧") < 8

    def test_mixed_string(self):
        assert weighted_length("Team 🏈") == weighted_length("Team ") + 2


class TestFits:
    def test_exactly_at_limit_fits(self):
        assert fits("a" * MAX_WEIGHTED)

    def test_one_over_does_not_fit(self):
        assert not fits("a" * (MAX_WEIGHTED + 1))

    def test_emoji_can_exceed_where_len_would_not(self):
        """The bug this module exists to prevent: 200 chars, 400 weighted."""
        text = "🏈" * 200
        assert len(text) == 200
        assert not fits(text)


class TestSplitThread:
    def test_empty_items(self):
        assert split_thread([]) == []

    def test_short_content_is_one_unnumbered_post(self):
        out = split_thread(["a vs b", "c vs d"], header="Week 1")
        assert len(out) == 1
        assert "1/1" not in out[0]
        assert out[0].startswith("Week 1")

    def test_header_appears_only_on_first_segment(self):
        items = [f"team{i:02d} " + "x" * 60 for i in range(12)]
        out = split_thread(items, header="Standings")
        assert out[0].startswith("Standings")
        assert not any(s.startswith("Standings") for s in out[1:])

    def test_all_segments_fit(self):
        items = [f"{i}. Team Number {i} 10-4 (1450.55 PF)" for i in range(1, 17)]
        for segment in split_thread(items, header="Standings after Week 14"):
            assert fits(segment), f"{weighted_length(segment)} > {MAX_WEIGHTED}"

    def test_numbering_is_sequential_and_total_is_correct(self):
        items = [f"row {i} " + "y" * 60 for i in range(10)]
        out = split_thread(items, header="H")
        for i, segment in enumerate(out, 1):
            assert segment.endswith(f" {i}/{len(out)}")

    def test_items_are_never_split_across_segments(self):
        items = [f"UNIQUE{i}" + "z" * 50 for i in range(12)]
        joined = "\n".join(split_thread(items))
        for item in items:
            assert item in joined

    def test_order_is_preserved(self):
        items = [f"item{i:02d}" + "w" * 60 for i in range(10)]
        joined = " ".join(split_thread(items))
        positions = [joined.index(f"item{i:02d}") for i in range(10)]
        assert positions == sorted(positions)

    def test_indivisible_oversized_item_raises(self):
        with pytest.raises(SegmentTooLong):
            split_thread(["q" * (MAX_WEIGHTED + 50)])

    def test_numbering_reserve_does_not_overflow_at_boundary(self):
        """The packing/numbering feedback loop: adding " 9/9" must not push a
        segment over the limit that fit before numbering was known."""
        items = [f"{i}. " + "m" * 66 for i in range(1, 40)]
        out = split_thread(items, header="Standings")
        assert len(out) > 9  # forces a two-digit total
        for segment in out:
            assert fits(segment)

    @pytest.mark.parametrize("team_count", range(4, 17))
    def test_property_every_league_size_produces_valid_segments(self, team_count):
        """Property test across every plausible league size."""
        items = [
            f"{i}. Really Long Team Name {i} 10-4-1 (1499.99 PF)"
            for i in range(1, team_count + 1)
        ]
        out = split_thread(items, header="Standings after Week 17")
        assert out
        for segment in out:
            assert fits(segment)

    def test_property_emoji_team_names_still_fit(self):
        """Emoji in team names is the realistic path to an over-limit post."""
        items = [f"{i}. 🏈🔥 Team {i} 🏆 8-6 (1200.00 PF)" for i in range(1, 13)]
        for segment in split_thread(items, header="Standings 🏈"):
            assert fits(segment)
