"""Season gating and week-progress tracking."""

import importlib
from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws
from shared.season import LEAGUE_TZ, in_active_hours, in_season, should_poll

TABLE = "gotffl-state-test"


def when(month, day, hour=12):
    return datetime(2026, month, day, hour, tzinfo=LEAGUE_TZ)


class TestSeasonGate:
    @pytest.mark.parametrize("month,day", [(9, 10), (11, 20), (12, 25), (1, 5), (8, 20)])
    def test_in_season(self, month, day):
        assert in_season(when(month, day))

    @pytest.mark.parametrize("month,day", [(3, 1), (5, 15), (7, 4), (8, 1), (1, 20)])
    def test_out_of_season(self, month, day):
        assert not in_season(when(month, day))

    def test_window_wraps_the_new_year(self):
        """Dec 31 and Jan 1 are both in season — a naive start<=x<=end fails here."""
        assert in_season(when(12, 31))
        assert in_season(when(1, 1))

    @pytest.mark.parametrize("hour", [2, 3, 4, 5])
    def test_quiet_hours_are_skipped(self, hour):
        assert not in_active_hours(when(10, 1, hour))

    @pytest.mark.parametrize("hour", [1, 6, 12, 23])
    def test_active_hours(self, hour):
        assert in_active_hours(when(10, 1, hour))

    def test_should_poll_requires_both(self):
        assert should_poll(when(10, 1, 12))
        assert not should_poll(when(10, 1, 3))  # in season, quiet hours
        assert not should_poll(when(5, 1, 12))  # active hours, off season

    def test_utc_input_is_converted_to_league_time(self):
        """A UTC 03:00 is 22:00 the previous day in Toronto — active, not quiet."""

        utc_3am = datetime(2026, 10, 2, 3, tzinfo=UTC)
        assert in_active_hours(utc_3am)


@pytest.fixture
def progress(monkeypatch):
    with mock_aws():
        boto3.resource("dynamodb", region_name="ca-central-1").create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("STATE_TABLE", TABLE)
        import shared.progress as module

        importlib.reload(module)
        yield module


class TestProgress:
    def test_starts_at_zero(self, progress):
        assert progress.last_week("scores") == 0

    def test_records_and_reads_back(self, progress):
        progress.record_week("scores", 3)
        assert progress.last_week("scores") == 3

    def test_pointer_never_moves_backwards(self, progress):
        """A late retry must not rewind the pointer and cause a re-post."""
        progress.record_week("scores", 5)
        progress.record_week("scores", 2)
        assert progress.last_week("scores") == 5

    def test_kinds_are_independent(self, progress):
        progress.record_week("scores", 4)
        assert progress.last_week("standings") == 0

    def test_target_is_next_week_once_yahoo_has_moved_past_it(self, progress):
        progress.record_week("scores", 3)
        assert progress.next_target("scores", current_week=5) == 4

    def test_target_is_none_before_yahoo_rolls_over(self, progress):
        """6am Tuesday, Yahoo still on week 4: week 4 isn't final, so defer."""
        progress.record_week("scores", 3)
        assert progress.next_target("scores", current_week=4) is None

    def test_first_run_targets_week_one(self, progress):
        assert progress.next_target("scores", current_week=2) == 1

    def test_first_run_defers_during_week_one(self, progress):
        assert progress.next_target("scores", current_week=1) is None
