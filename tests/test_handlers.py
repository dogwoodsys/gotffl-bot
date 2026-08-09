"""Handlers and validators.

Standard requires 100% on validators and processors. Handlers are thin
orchestration, but every one is an entry point, so each gets a happy path and
at least two error paths."""

import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import load_function

TABLE = "gotffl-state-test"
QUEUE = "gotffl-outbox-test.fifo"

SCHEDULED = ["post_scores", "post_standings", "post_matchups"]
ALL_READERS = ["poll_transactions", *SCHEDULED]


class Ctx:
    function_name = "gotffl-test"


@pytest.fixture
def aws(monkeypatch):
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
        url = boto3.client("sqs", region_name="ca-central-1").create_queue(
            QueueName=QUEUE, Attributes={"FifoQueue": "true"}
        )["QueueUrl"]
        monkeypatch.setenv("STATE_TABLE", TABLE)
        monkeypatch.setenv("OUTBOX_URL", url)
        monkeypatch.setenv("YAHOO_LEAGUE_KEY", "461.l.1")
        yield url


# ------------------------------------------------------------------ validators


@pytest.mark.parametrize("fn", ALL_READERS)
def test_validator_accepts_complete_config(aws, fn):
    _p, validator, _h = load_function(fn)
    request = validator.validate_input({})
    assert request.league_key == "461.l.1"
    assert request.outbox_url


@pytest.mark.parametrize("fn", ALL_READERS)
@pytest.mark.parametrize("missing", ["YAHOO_LEAGUE_KEY", "OUTBOX_URL"])
def test_validator_refuses_missing_config_rather_than_defaulting(aws, monkeypatch, fn, missing):
    """A silently defaulted league key would post another league's data."""
    monkeypatch.delenv(missing)
    _p, validator, _h = load_function(fn)
    with pytest.raises(ValueError, match=missing):
        validator.validate_input({})


class TestPublishValidator:
    def test_accepts_a_well_formed_record(self):
        _p, validator, _h = load_function("publish")
        body = {"post_type": "scores", "idempotency_key": "scores#1", "segments": ["hi"]}
        records = validator.validate_records(
            {"Records": [{"messageId": "m1", "body": json.dumps(body)}]}
        )
        assert records[0][0] == "m1"
        assert records[0][1].idempotency_key == "scores#1"

    @pytest.mark.parametrize(
        "event,match",
        [
            ({}, "no Records"),
            ({"Records": []}, "no Records"),
            ({"Records": [{"body": "{}"}]}, "messageId"),
            ({"Records": [{"messageId": "m1", "body": "not json"}]}, "unparseable"),
            ({"Records": [{"messageId": "m1"}]}, "unparseable"),
            ({"Records": [{"messageId": "m1", "body": "{}"}]}, "not a rendered post"),
            (
                {"Records": [{"messageId": "m1", "body": json.dumps({"post_type": "nope",
                                                                     "idempotency_key": "k",
                                                                     "segments": []})}]},
                "not a rendered post",
            ),
        ],
    )
    def test_rejects_malformed_records(self, event, match):
        _p, validator, _h = load_function("publish")
        with pytest.raises(ValueError, match=match):
            validator.validate_records(event)


# -------------------------------------------------------------------- handlers


class StubYahoo:
    def __init__(self, week=3):
        self._week = week

    def current_week(self):
        return self._week

    def transactions(self):
        return []

    def scoreboard(self, week):
        from shared.models import MatchupResult

        return [MatchupResult(week, "A", 10.0, "B", 9.0)]

    def standings(self):
        from shared.models import TeamStanding

        return [TeamStanding(1, "A", 1, 0, 0, 10.0, 9.0)]

    def matchups(self, week):
        from shared.models import MatchupPairing

        return [MatchupPairing(week, "A", "B")]


def test_poll_handler_returns_counts(aws, monkeypatch):
    processor, _v, handler_module = load_function("poll_transactions")
    monkeypatch.setattr(
        processor, "YahooClient", lambda *a, **k: StubYahoo()
    )  # Yahoo is not an AWS service; moto cannot stub it
    result = handler_module.handler({}, Ctx())
    assert result == {"fetched": 0, "enqueued": 0}


def test_poll_handler_propagates_config_error(aws, monkeypatch):
    monkeypatch.delenv("YAHOO_LEAGUE_KEY")
    _p, _v, handler_module = load_function("poll_transactions")
    with pytest.raises(ValueError):
        handler_module.handler({}, Ctx())


@pytest.mark.parametrize("fn", SCHEDULED)
def test_scheduled_handler_reports_the_week(aws, monkeypatch, fn):
    processor, _v, handler_module = load_function(fn)
    monkeypatch.setattr(
        processor, "YahooClient", lambda *a, **k: StubYahoo(week=3)
    )  # Yahoo is not an AWS service; moto cannot stub it
    result = handler_module.handler({}, Ctx())
    assert result["week"] is not None
    assert result["enqueued"] is True


@pytest.mark.parametrize("fn", ["post_scores", "post_standings"])
def test_scheduled_handler_reports_deferral(aws, monkeypatch, fn):
    """Yahoo hasn't rolled over: the handler must report a deferral, not fail."""
    processor, _v, handler_module = load_function(fn)
    monkeypatch.setattr(
        processor, "YahooClient", lambda *a, **k: StubYahoo(week=1)
    )  # Yahoo is not an AWS service; moto cannot stub it
    result = handler_module.handler({}, Ctx())
    assert result == {"week": None, "enqueued": False}


@pytest.mark.parametrize("fn", SCHEDULED)
def test_scheduled_handler_propagates_config_error(aws, monkeypatch, fn):
    monkeypatch.delenv("OUTBOX_URL")
    _p, _v, handler_module = load_function(fn)
    with pytest.raises(ValueError):
        handler_module.handler({}, Ctx())
