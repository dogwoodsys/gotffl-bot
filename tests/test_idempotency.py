"""Dedup. moto DynamoDB with the real key schema — a MagicMock table would
accept any malformed ConditionExpression and ship the duplicate-post bug green.

Assertions read state back from the table rather than checking call args."""

import importlib

import boto3
import pytest
from moto import mock_aws

TABLE = "gotffl-state-test"


@pytest.fixture
def idem(monkeypatch):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="ca-central-1")
        ddb.create_table(
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
        import shared.idempotency as module

        importlib.reload(module)  # drop the cached table handle between tests
        yield module


def row(kind, key):
    table = boto3.resource("dynamodb", region_name="ca-central-1").Table(TABLE)
    return table.get_item(Key={"pk": f"{kind}#{key}", "sk": "CLAIM"}, ConsistentRead=True).get(
        "Item"
    )


def test_first_claim_succeeds(idem):
    assert idem.claim("TXN", "abc") is True
    assert row("TXN", "abc") is not None


def test_second_claim_is_refused(idem):
    idem.claim("TXN", "abc")
    assert idem.claim("TXN", "abc") is False


def test_claim_is_not_done_until_confirmed(idem):
    """The ordering that prevents silent drops: a claim alone is not completion."""
    idem.claim("TXN", "abc")
    assert idem.is_done("TXN", "abc") is False
    idem.confirm("TXN", "abc")
    assert idem.is_done("TXN", "abc") is True


def test_confirm_records_evidence(idem):
    idem.claim("POST", "scores#3")
    idem.confirm("POST", "scores#3", tweet_id="1234567890")
    item = row("POST", "scores#3")
    assert item["tweet_id"] == "1234567890"
    assert "confirmed_at" in item


def test_confirmed_work_is_never_reclaimed(idem):
    """The duplicate-post guard. Must hold even after the claim TTL passes."""
    idem.claim("TXN", "abc")
    idem.confirm("TXN", "abc")
    assert idem.claim("TXN", "abc") is False


def test_release_frees_an_unconfirmed_claim_for_retry(idem):
    idem.claim("TXN", "abc")
    idem.release("TXN", "abc")
    assert idem.claim("TXN", "abc") is True


def test_release_cannot_undo_a_confirmation(idem):
    from botocore.exceptions import ClientError

    idem.claim("TXN", "abc")
    idem.confirm("TXN", "abc")
    with pytest.raises(ClientError):
        idem.release("TXN", "abc")
    assert idem.is_done("TXN", "abc") is True


def test_unknown_key_is_not_done(idem):
    assert idem.is_done("TXN", "never-seen") is False


def test_kinds_are_independent(idem):
    idem.claim("TXN", "1")
    assert idem.claim("POST", "1") is True


def test_confirm_sets_long_ttl(idem):
    idem.claim("TXN", "abc")
    before = row("TXN", "abc")["ttl"]
    idem.confirm("TXN", "abc")
    assert row("TXN", "abc")["ttl"] > before
