"""Reader processors end to end: moto for DynamoDB/SQS, a stub for Yahoo
(a non-AWS external moto cannot simulate)."""

import importlib
import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import load_function  # noqa: E402
from shared.models import (  # noqa: E402
    MatchupPairing,
    MatchupResult,
    PlayerMove,
    TeamStanding,
    Transaction,
    TransactionType,
)

TABLE = "gotffl-state-test"
QUEUE = "gotffl-outbox-test.fifo"


class StubYahoo:
    """Records calls; raises where configured."""

    def __init__(self, week=5, transactions=None, fail=None):
        self._week = week
        self._transactions = transactions or []
        self._fail = fail
        self.calls: list[str] = []

    def current_week(self):
        self.calls.append("current_week")
        return self._week

    def transactions(self):
        self.calls.append("transactions")
        if self._fail == "transactions":
            raise RuntimeError("yahoo down")
        return self._transactions

    def scoreboard(self, week):
        self.calls.append(f"scoreboard:{week}")
        if self._fail == "scoreboard":
            raise RuntimeError("yahoo down")
        return [MatchupResult(week, "Team A", 110.5, "Team B", 99.25)]

    def standings(self):
        self.calls.append("standings")
        return [TeamStanding(1, "Team A", 4, 1, 0, 500.0, 400.0)]

    def matchups(self, week):
        self.calls.append(f"matchups:{week}")
        return [MatchupPairing(week, "Team A", "Team B")]


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
        sqs = boto3.client("sqs", region_name="ca-central-1")
        url = sqs.create_queue(
            QueueName=QUEUE, Attributes={"FifoQueue": "true"}
        )["QueueUrl"]

        monkeypatch.setenv("STATE_TABLE", TABLE)
        monkeypatch.setenv("OUTBOX_URL", url)
        monkeypatch.setenv("YAHOO_LEAGUE_KEY", "461.l.1")

        for mod in ("shared.idempotency", "shared.progress", "shared.outbox"):
            importlib.reload(importlib.import_module(mod))
        yield url


def drain(url):
    sqs = boto3.client("sqs", region_name="ca-central-1")
    out = []
    while True:
        got = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10)
        msgs = got.get("Messages", [])
        if not msgs:
            return out
        for m in msgs:
            out.append(json.loads(m["Body"]))
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])


def txn(key, name="Puka Nacua"):
    return Transaction(
        transaction_key=key,
        type=TransactionType.ADD,
        timestamp=1_700_000_000,
        moves=(PlayerMove(name, None, "Team Alpha"),),
    )


# ------------------------------------------------------------ poll_transactions


@pytest.fixture
def poller(aws):
    processor, _validator, _handler = load_function("poll_transactions")
    return processor


def poll_request():
    _processor, validator, _handler = load_function("poll_transactions")
    return validator.validate_input({})


from datetime import datetime  # noqa: E402

from shared.season import LEAGUE_TZ  # noqa: E402

IN_SEASON = datetime(2026, 10, 1, 12, tzinfo=LEAGUE_TZ)
OFF_SEASON = datetime(2026, 5, 1, 12, tzinfo=LEAGUE_TZ)


def test_poller_no_ops_out_of_season_without_calling_yahoo(poller, aws):
    api = StubYahoo(transactions=[txn("t1")])
    result = poller.process(poll_request(), client=api, now=OFF_SEASON)
    assert result.skipped_off_season is True
    assert api.calls == [], "off-season must not cost a Yahoo call"
    assert drain(aws) == []


def test_poller_enqueues_new_transactions(poller, aws):
    result = poller.process(
        poll_request(), client=StubYahoo(transactions=[txn("t1"), txn("t2")]), now=IN_SEASON
    )
    assert result.enqueued == 2
    assert {m["idempotency_key"] for m in drain(aws)} == {"t1", "t2"}


def test_poller_does_not_re_enqueue_seen_transactions(poller, aws):
    api = StubYahoo(transactions=[txn("t1")])
    poller.process(poll_request(), client=api, now=IN_SEASON)
    drain(aws)
    result = poller.process(poll_request(), client=api, now=IN_SEASON)
    assert result.enqueued == 0
    assert drain(aws) == []


def test_poller_enqueues_only_the_new_one_in_a_mixed_batch(poller, aws):
    poller.process(poll_request(), client=StubYahoo(transactions=[txn("t1")]), now=IN_SEASON)
    drain(aws)
    result = poller.process(
        poll_request(), client=StubYahoo(transactions=[txn("t1"), txn("t2")]), now=IN_SEASON
    )
    assert result.enqueued == 1
    assert [m["idempotency_key"] for m in drain(aws)] == ["t2"]


def test_poller_releases_claim_when_enqueue_fails(poller, aws, monkeypatch):
    """A failed enqueue must leave the transaction retryable, not lost."""
    request = poll_request()

    def boom(*args, **kwargs):
        raise RuntimeError("sqs down")

    monkeypatch.setattr(poller, "enqueue", boom)
    with pytest.raises(RuntimeError):
        poller.process(request, client=StubYahoo(transactions=[txn("t1")]), now=IN_SEASON)

    monkeypatch.undo()
    result = poller.process(request, client=StubYahoo(transactions=[txn("t1")]), now=IN_SEASON)
    assert result.enqueued == 1, "a released claim must be retryable"


def test_poller_propagates_yahoo_failure_so_lambda_retries(poller, aws):
    with pytest.raises(RuntimeError):
        poller.process(poll_request(), client=StubYahoo(fail="transactions"), now=IN_SEASON)


# ------------------------------------------------------------- scheduled posts


def load(fn_dir):
    processor, validator, _handler = load_function(fn_dir)
    return processor, validator.validate_input({})


@pytest.mark.parametrize("fn_dir", ["post_scores", "post_standings"])
def test_tuesday_jobs_defer_until_yahoo_rolls_over(aws, fn_dir):
    processor, request = load(fn_dir)
    result = processor.process(request, client=StubYahoo(week=1))
    assert result.week is None
    assert drain(aws) == []


@pytest.mark.parametrize(
    "fn_dir,expected", [("post_scores", "scores#1"), ("post_standings", "standings#1")]
)
def test_tuesday_jobs_post_the_completed_week(aws, fn_dir, expected):
    processor, request = load(fn_dir)
    result = processor.process(request, client=StubYahoo(week=2))
    assert result.week == 1
    assert [m["idempotency_key"] for m in drain(aws)] == [expected]


@pytest.mark.parametrize("fn_dir", ["post_scores", "post_standings"])
def test_tuesday_jobs_are_idempotent_across_retries(aws, fn_dir):
    """The scheduler retries; the second firing must not enqueue again."""
    processor, request = load(fn_dir)
    processor.process(request, client=StubYahoo(week=2))
    drain(aws)
    result = processor.process(request, client=StubYahoo(week=2))
    assert result.enqueued is False
    assert drain(aws) == []


def test_scores_releases_claim_when_yahoo_fails(aws):
    processor, request = load("post_scores")
    with pytest.raises(RuntimeError):
        processor.process(request, client=StubYahoo(week=2, fail="scoreboard"))
    # Retry must be able to reclaim.
    result = processor.process(request, client=StubYahoo(week=2))
    assert result.enqueued is True


def test_matchups_posts_the_week_under_way(aws):
    processor, request = load("post_matchups")
    result = processor.process(request, client=StubYahoo(week=6))
    assert result.week == 6
    assert [m["idempotency_key"] for m in drain(aws)] == ["matchups#6"]


def test_matchups_retry_does_not_double_post(aws):
    """Wednesday noon and the 3pm retry both fire; only one post."""
    processor, request = load("post_matchups")
    processor.process(request, client=StubYahoo(week=6))
    drain(aws)
    result = processor.process(request, client=StubYahoo(week=6))
    assert result.week is None
    assert drain(aws) == []


def test_matchups_posts_again_once_the_week_advances(aws):
    processor, request = load("post_matchups")
    processor.process(request, client=StubYahoo(week=6))
    drain(aws)
    result = processor.process(request, client=StubYahoo(week=7))
    assert result.week == 7
    assert [m["idempotency_key"] for m in drain(aws)] == ["matchups#7"]


def test_validator_refuses_missing_league_key(aws, monkeypatch):
    monkeypatch.delenv("YAHOO_LEAGUE_KEY")
    _p, validator, _h = load_function("post_scores")
    with pytest.raises(ValueError, match="YAHOO_LEAGUE_KEY"):
        validator.validate_input({})


def test_validator_refuses_missing_outbox_url(aws, monkeypatch):
    monkeypatch.delenv("OUTBOX_URL")
    _p, validator, _h = load_function("post_scores")
    with pytest.raises(ValueError, match="OUTBOX_URL"):
        validator.validate_input({})


# ------------------------------------------------- scheduled job edge branches


@pytest.mark.parametrize(
    "fn_dir,kind,week",
    [
        ("post_scores", "scores", 1),
        ("post_standings", "standings", 1),
        ("post_matchups", "matchups", 6),
    ],
)
def test_scheduled_job_stops_when_another_invocation_holds_the_claim(aws, fn_dir, kind, week):
    """Two schedule firings can overlap. The loser must not enqueue."""
    import shared.idempotency as idem

    processor, request = load(fn_dir)
    assert idem.claim("ENQUEUE", f"{kind}#{week}") is True  # simulate the winner
    result = processor.process(request, client=StubYahoo(week=6 if kind == "matchups" else 2))
    assert result.enqueued is False
    assert drain(aws) == []


@pytest.mark.parametrize(
    "fn_dir,week", [("post_standings", 2), ("post_matchups", 6)]
)
def test_scheduled_job_releases_claim_when_enqueue_fails(aws, monkeypatch, fn_dir, week):
    processor, request = load(fn_dir)

    def boom(*args, **kwargs):
        raise RuntimeError("sqs down")

    monkeypatch.setattr(processor, "enqueue", boom)
    with pytest.raises(RuntimeError):
        processor.process(request, client=StubYahoo(week=week))

    monkeypatch.undo()
    result = processor.process(request, client=StubYahoo(week=week))
    assert result.enqueued is True, "a released claim must be retryable"
