"""Publisher. moto for SSM/DynamoDB; the X client is a stub because it is a
non-AWS external that moto cannot simulate.

The load-bearing test here is that shadow mode issues zero HTTP calls."""

import importlib
import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import load_function
from shared.models import PostType, RenderedPost

TABLE = "gotffl-state-test"
SHADOW_PARAM = "/gotffl/shadow_mode"


class StubX:
    """Records calls. Raises if configured to."""

    def __init__(self, fail_on: int | None = None):
        self.calls: list[tuple[str, str | None]] = []
        self.fail_on = fail_on

    def post(self, text: str, in_reply_to: str | None = None) -> str:
        self.calls.append((text, in_reply_to))
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise RuntimeError("X exploded")
        return f"tweet{len(self.calls)}"


def post(key="scores#1", segments=("Week 1 final scores\nA 10.00 def. B 9.00",)):
    return RenderedPost(post_type=PostType.SCORES, idempotency_key=key, segments=segments)


@pytest.fixture
def env(monkeypatch):
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
        ssm = boto3.client("ssm", region_name="ca-central-1")
        ssm.put_parameter(Name=SHADOW_PARAM, Value="true", Type="String")

        monkeypatch.setenv("STATE_TABLE", TABLE)
        monkeypatch.setenv("SHADOW_MODE_PARAM", SHADOW_PARAM)
        monkeypatch.setenv("X_PREFIX", "/gotffl/x")

        import shared.idempotency as idem

        importlib.reload(idem)
        processor, _validator, _handler = load_function("publish")
        processor._ssm = None
        processor._ddb = None
        yield processor, ssm


def go_live(ssm):
    ssm.put_parameter(Name=SHADOW_PARAM, Value="false", Type="String", Overwrite=True)


def table():
    return boto3.resource("dynamodb", region_name="ca-central-1").Table(TABLE)


# ------------------------------------------------------------- shadow mode


def test_shadow_mode_sends_nothing(env):
    processor, _ = env
    client = StubX()
    result = processor.process(post(), client=client)
    assert result.shadowed is True
    assert result.published is False
    assert client.calls == [], "shadow mode must issue zero X calls"


def test_shadow_mode_records_the_rendered_text_for_review(env):
    processor, _ = env
    processor.process(post(key="scores#4"), client=StubX())
    items = table().scan()["Items"]
    shadow = [i for i in items if i["pk"].startswith("SHADOW#")]
    assert len(shadow) == 1
    assert shadow[0]["segments"]  # the reviewable output
    assert "ttl" in shadow[0]


def test_shadow_mode_leaves_no_published_claim(env):
    """A shadowed post must still be publishable once live."""
    processor, ssm = env
    processor.process(post(), client=StubX())
    go_live(ssm)
    client = StubX()
    assert processor.process(post(), client=client).published is True


def test_unreadable_shadow_flag_defaults_to_shadow(env, monkeypatch):
    """If the flag can't be read, post nothing. Silence beats an unintended post."""
    processor, _ = env
    monkeypatch.setenv("SHADOW_MODE_PARAM", "/gotffl/does-not-exist")
    processor._ssm = None
    client = StubX()
    assert processor.process(post(), client=client).shadowed is True
    assert client.calls == []


@pytest.mark.parametrize("value", ["true", "TRUE", "yes", "1", "  ", "flase"])
def test_only_the_literal_false_goes_live(env, value):
    processor, ssm = env
    ssm.put_parameter(Name=SHADOW_PARAM, Value=value, Type="String", Overwrite=True)
    processor._ssm = None
    client = StubX()
    processor.process(post(), client=client)
    assert client.calls == []


def test_false_is_case_insensitive_and_trimmed(env, ssm_value=" False "):
    processor, ssm = env
    ssm.put_parameter(Name=SHADOW_PARAM, Value=ssm_value, Type="String", Overwrite=True)
    processor._ssm = None
    assert processor.process(post(), client=StubX()).published is True


# ---------------------------------------------------------------- live mode


def test_live_mode_posts_and_records_the_tweet_id(env):
    processor, ssm = env
    go_live(ssm)
    client = StubX()
    result = processor.process(post(key="scores#2"), client=client)
    assert result.published is True
    assert result.tweet_ids == ("tweet1",)
    assert len(client.calls) == 1


def test_thread_chains_each_reply_to_the_previous(env):
    processor, ssm = env
    go_live(ssm)
    client = StubX()
    processor.process(post(segments=("one", "two", "three")), client=client)
    assert [c[1] for c in client.calls] == [None, "tweet1", "tweet2"]


def test_duplicate_is_not_republished(env):
    processor, ssm = env
    go_live(ssm)
    first = StubX()
    processor.process(post(key="scores#5"), client=first)
    second = StubX()
    result = processor.process(post(key="scores#5"), client=second)
    assert result.published is False
    assert result.reason == "duplicate"
    assert second.calls == []


def test_partial_thread_failure_is_confirmed_not_replayed(env):
    """Two of three posts are already public. Re-sending from the start would
    duplicate them, so the key is confirmed even though the post failed."""
    processor, ssm = env
    go_live(ssm)
    with pytest.raises(RuntimeError):
        processor.process(post(key="scores#6", segments=("a", "b", "c")), client=StubX(fail_on=3))

    item = table().get_item(Key={"pk": "POST#scores#6", "sk": "CLAIM"})["Item"]
    assert item["partial"] is True
    assert item["tweet_ids"] == ["tweet1", "tweet2"]

    retry = StubX()
    replayed = processor.process(post(key="scores#6", segments=("a", "b", "c")), client=retry)
    assert replayed.published is False
    assert retry.calls == []


def test_failure_before_any_post_releases_the_claim_for_retry(env):
    processor, ssm = env
    go_live(ssm)
    with pytest.raises(RuntimeError):
        processor.process(post(key="scores#7"), client=StubX(fail_on=1))
    # Nothing went public, so a retry must be allowed.
    assert processor.process(post(key="scores#7"), client=StubX()).published is True


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "segments,match",
    [
        ((), "no segments"),
        (("   ",), "empty"),
        (("x" * 400,), "character limit"),
        (("Trade: {team} gets {player}",), "placeholder"),
    ],
)
def test_invalid_posts_are_rejected_before_sending(env, segments, match):
    processor, ssm = env
    go_live(ssm)
    client = StubX()
    with pytest.raises(ValueError, match=match):
        processor.process(post(segments=segments), client=client)
    assert client.calls == []


def test_emoji_overflow_is_rejected(env):
    """len() would pass this at 200; weighted length correctly rejects it."""
    processor, ssm = env
    go_live(ssm)
    with pytest.raises(ValueError, match="character limit"):
        processor.process(post(segments=("🏈" * 200,)), client=StubX())


# ------------------------------------------------------------------ handler


def test_handler_reports_failed_message_for_redrive(env, monkeypatch):
    _processor, ssm = env
    # The handler binds `process` from its own freshly loaded processor, so the
    # stub must go on that instance, not the fixture's.
    proc, _v, handler_module = load_function("publish")
    proc._ssm = None
    proc._ddb = None
    go_live(ssm)
    # X is a non-AWS external moto cannot simulate; stub the narrowest thing.
    monkeypatch.setattr(proc, "XClient", StubX)  # X is not an AWS service; moto cannot stub it

    event = {
        "Records": [
            {"messageId": "m1", "body": json.dumps(post(key="scores#8").to_message())},
            {"messageId": "m2", "body": json.dumps({"post_type": "scores",
                                                    "idempotency_key": "bad",
                                                    "segments": []})},
        ]
    }
    result = handler_module.handler(event, type("Ctx", (), {"function_name": "test"})())
    assert result["batchItemFailures"] == [{"itemIdentifier": "m2"}]


def test_handler_rejects_malformed_event(env):
    _p, _v, handler_module = load_function("publish")
    with pytest.raises(ValueError):
        handler_module.handler({}, type("Ctx", (), {"function_name": "test"})())


def test_concurrent_invocation_losing_the_claim_does_not_post(env):
    """Reserved concurrency is 1, but a redrive can overlap a retry. The loser
    must not send a second copy."""
    processor, ssm = env
    go_live(ssm)
    import shared.idempotency as idem

    assert idem.claim("POST", "scores#9") is True  # simulate the winner
    client = StubX()
    result = processor.process(post(key="scores#9"), client=client)
    assert result.published is False
    assert result.reason == "claimed_elsewhere"
    assert client.calls == []


def test_handler_routes_unexpected_errors_to_the_dlq_too(env, monkeypatch):
    """A non-ValueError failure must still be redriven, not silently dropped."""
    _processor, ssm = env
    proc, _v, handler_module = load_function("publish")
    proc._ssm = None
    proc._ddb = None
    go_live(ssm)

    def boom(*args, **kwargs):
        raise RuntimeError("X unreachable")

    monkeypatch.setattr(proc, "XClient", lambda *a, **k: type("C", (), {"post": boom})())
    body = json.dumps(post(key="scores#10").to_message())
    event = {"Records": [{"messageId": "m9", "body": body}]}
    result = handler_module.handler(event, type("Ctx", (), {"function_name": "test"})())
    assert result["batchItemFailures"] == [{"itemIdentifier": "m9"}]
