"""Yahoo and X clients.

⚠️ The Yahoo fixtures are hand-built from the documented shape, not recorded
from a live account — no credentials existed when they were written. They prove
the parsers are self-consistent; they do NOT prove the parsers match Yahoo.
Replace them with a recorded payload before go-live (see shared/yahoo.py)."""

import boto3
import pytest
import responses
from moto import mock_aws
from shared.models import TransactionType
from shared.x_client import TWEETS_URL, XAuthError, XClient, XRateLimited
from shared.yahoo import BASE_URL, YahooClient, YahooParseError, YahooRateLimited

LEAGUE = "461.l.123456"


class FixedTokens:
    def access_token(self):
        return "yahoo-access-token"


def client():
    return YahooClient(LEAGUE, tokens=FixedTokens())


def url(path):
    return f"{BASE_URL}/{path}"


# ------------------------------------------------------------------- fixtures

LEAGUE_PAYLOAD = {"fantasy_content": {"league": [{"league_key": LEAGUE, "current_week": "7"}]}}

TRANSACTIONS_PAYLOAD = {
    "fantasy_content": {
        "league": [
            {"league_key": LEAGUE},
            {
                "transactions": {
                    "0": {
                        "transaction": [
                            {
                                "transaction_key": f"{LEAGUE}.tr.101",
                                "type": "add",
                                "timestamp": "1700000000",
                            },
                            {
                                "players": {
                                    "0": {
                                        "player": [
                                            [{"name": {"full": "Puka Nacua"}}],
                                            {
                                                "transaction_data": {
                                                    "type": "add",
                                                    "destination_team_name": "Team Alpha",
                                                }
                                            },
                                        ]
                                    },
                                    "count": 1,
                                }
                            },
                        ]
                    },
                    "count": 1,
                }
            },
        ]
    }
}

SCOREBOARD_PAYLOAD = {
    "fantasy_content": {
        "league": [
            {"league_key": LEAGUE},
            {
                "scoreboard": {
                    "0": {
                        "matchups": {
                            "0": {
                                "matchup": {
                                    "0": {
                                        "teams": {
                                            "0": {
                                                "team": [
                                                    [{"name": "Team Alpha"}],
                                                    {"team_points": {"total": "121.40"}},
                                                ]
                                            },
                                            "1": {
                                                "team": [
                                                    [{"name": "Team Beta"}],
                                                    {"team_points": {"total": "88.20"}},
                                                ]
                                            },
                                            "count": 2,
                                        }
                                    }
                                }
                            },
                            "count": 1,
                        }
                    }
                }
            },
        ]
    }
}

STANDINGS_PAYLOAD = {
    "fantasy_content": {
        "league": [
            {"league_key": LEAGUE},
            {
                "standings": {
                    "0": {
                        "teams": {
                            "0": {
                                "team": [
                                    [{"name": "Team Alpha"}],
                                    {
                                        "team_standings": {
                                            "rank": "1",
                                            "outcome_totals": {
                                                "wins": "5",
                                                "losses": "1",
                                                "ties": "0",
                                            },
                                            "points_for": "812.44",
                                            "points_against": "700.10",
                                        }
                                    },
                                ]
                            },
                            "count": 1,
                        }
                    }
                }
            },
        ]
    }
}


# ---------------------------------------------------------------------- Yahoo


@responses.activate
def test_current_week():
    responses.add(responses.GET, url(f"league/{LEAGUE}"), json=LEAGUE_PAYLOAD)
    assert client().current_week() == 7


@responses.activate
def test_current_week_missing_raises_rather_than_defaulting():
    responses.add(responses.GET, url(f"league/{LEAGUE}"), json={"fantasy_content": {}})
    with pytest.raises(YahooParseError):
        client().current_week()


@responses.activate
def test_bearer_token_is_sent():
    responses.add(responses.GET, url(f"league/{LEAGUE}"), json=LEAGUE_PAYLOAD)
    client().current_week()
    assert responses.calls[0].request.headers["Authorization"] == "Bearer yahoo-access-token"


@responses.activate
def test_transactions_parse():
    responses.add(
        responses.GET,
        url(f"league/{LEAGUE}/transactions;types=add,drop,trade"),
        json=TRANSACTIONS_PAYLOAD,
    )
    txns = client().transactions()
    assert len(txns) == 1
    assert txns[0].transaction_key == f"{LEAGUE}.tr.101"
    assert txns[0].type is TransactionType.ADD
    assert txns[0].moves[0].player_name == "Puka Nacua"
    assert txns[0].moves[0].to_team == "Team Alpha"


@responses.activate
def test_unknown_transaction_type_raises():
    payload = {
        "fantasy_content": {
            "league": [
                {
                    "transactions": {
                        "0": {
                            "transaction": [
                                {"transaction_key": "k", "type": "teleport", "timestamp": "1"}
                            ]
                        }
                    }
                }
            ]
        }
    }
    responses.add(
        responses.GET, url(f"league/{LEAGUE}/transactions;types=add,drop,trade"), json=payload
    )
    with pytest.raises(YahooParseError, match="teleport"):
        client().transactions()


@responses.activate
def test_scoreboard_parse():
    responses.add(
        responses.GET, url(f"league/{LEAGUE}/scoreboard;week=7"), json=SCOREBOARD_PAYLOAD
    )
    results = client().scoreboard(7)
    assert len(results) == 1
    assert {results[0].team_a, results[0].team_b} == {"Team Alpha", "Team Beta"}
    assert results[0].winner == "Team Alpha"


@responses.activate
def test_empty_scoreboard_raises_rather_than_returning_nothing():
    """A parser that returns [] on a schema change makes the bot silently quiet."""
    responses.add(
        responses.GET, url(f"league/{LEAGUE}/scoreboard;week=7"), json={"fantasy_content": {}}
    )
    with pytest.raises(YahooParseError):
        client().scoreboard(7)


@responses.activate
def test_matchups_parse():
    responses.add(
        responses.GET, url(f"league/{LEAGUE}/scoreboard;week=7"), json=SCOREBOARD_PAYLOAD
    )
    pairings = client().matchups(7)
    assert len(pairings) == 1
    assert pairings[0].week == 7


@responses.activate
def test_empty_matchups_raises():
    responses.add(
        responses.GET, url(f"league/{LEAGUE}/scoreboard;week=7"), json={"fantasy_content": {}}
    )
    with pytest.raises(YahooParseError):
        client().matchups(7)


@responses.activate
def test_standings_parse_and_sort():
    responses.add(responses.GET, url(f"league/{LEAGUE}/standings"), json=STANDINGS_PAYLOAD)
    standings = client().standings()
    assert standings[0].rank == 1
    assert standings[0].team == "Team Alpha"
    assert standings[0].wins == 5
    assert standings[0].points_for == pytest.approx(812.44)


@responses.activate
def test_empty_standings_raises():
    responses.add(responses.GET, url(f"league/{LEAGUE}/standings"), json={"fantasy_content": {}})
    with pytest.raises(YahooParseError):
        client().standings()


@responses.activate
def test_rate_limit_surfaces_retry_after():
    responses.add(
        responses.GET, url(f"league/{LEAGUE}"), status=429, headers={"Retry-After": "42"}
    )
    with pytest.raises(YahooRateLimited) as exc:
        client().current_week()
    assert exc.value.retry_after == 42


@responses.activate
def test_rate_limit_without_header_defaults():
    responses.add(responses.GET, url(f"league/{LEAGUE}"), status=429)
    with pytest.raises(YahooRateLimited) as exc:
        client().current_week()
    assert exc.value.retry_after == 60


@responses.activate
def test_server_error_raises_for_retry():
    import requests

    responses.add(responses.GET, url(f"league/{LEAGUE}"), status=503)
    with pytest.raises(requests.HTTPError):
        client().current_week()


def test_client_has_no_write_method():
    """Read-only by construction, not by convention."""
    for name in ("post", "put", "add_player", "drop_player", "submit_claim"):
        assert not hasattr(YahooClient, name)


# -------------------------------------------------------------------------- X


@pytest.fixture
def x_ssm():
    with mock_aws():
        ssm = boto3.client("ssm", region_name="ca-central-1")
        for name in ("consumer_key", "consumer_secret", "access_token", "access_token_secret"):
            ssm.put_parameter(Name=f"/gotffl/x/{name}", Value=f"v-{name}", Type="SecureString")
        yield ssm


class FakeOAuth:
    def __init__(self, status=201, body=None, headers=None):
        self.status = status
        self.body = body if body is not None else {"data": {"id": "1799999999"}}
        self.headers = headers or {}
        self.requests: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.requests.append(json)
        outer = self

        class Response:
            status_code = outer.status
            headers = outer.headers

            @staticmethod
            def json():
                return outer.body

            @staticmethod
            def raise_for_status():
                if outer.status >= 400:
                    import requests

                    raise requests.HTTPError(f"{outer.status}")

        return Response()


def test_x_post_returns_tweet_id(x_ssm):
    session = FakeOAuth()
    assert XClient("/gotffl/x", ssm=x_ssm, session=session).post("hello") == "1799999999"
    assert session.requests[0] == {"text": "hello"}


def test_x_reply_sets_in_reply_to(x_ssm):
    session = FakeOAuth()
    XClient("/gotffl/x", ssm=x_ssm, session=session).post("second", in_reply_to="123")
    assert session.requests[0]["reply"] == {"in_reply_to_tweet_id": "123"}


def test_x_rate_limit(x_ssm):
    session = FakeOAuth(status=429, headers={"retry-after": "300"})
    with pytest.raises(XRateLimited) as exc:
        XClient("/gotffl/x", ssm=x_ssm, session=session).post("hi")
    assert exc.value.retry_after == 300


@pytest.mark.parametrize("status", [401, 403])
def test_x_auth_failure_is_not_retryable(x_ssm, status):
    session = FakeOAuth(status=status)
    with pytest.raises(XAuthError):
        XClient("/gotffl/x", ssm=x_ssm, session=session).post("hi")


def test_x_server_error_raises_for_retry(x_ssm):
    import requests

    session = FakeOAuth(status=503)
    with pytest.raises(requests.HTTPError):
        XClient("/gotffl/x", ssm=x_ssm, session=session).post("hi")


def test_x_missing_credentials_raises(x_ssm):
    x_ssm.delete_parameter(Name="/gotffl/x/access_token")
    with pytest.raises(XAuthError, match="access_token"):
        XClient("/gotffl/x", ssm=x_ssm).post("hi")


def test_x_builds_oauth_session_from_parameter_store(x_ssm):
    """Credentials are read from SSM, never from the environment."""
    client_obj = XClient("/gotffl/x", ssm=x_ssm)
    session = client_obj._oauth()
    assert session.auth.client.client_key == "v-consumer_key"


def test_x_client_has_no_read_method():
    """Reads cost money and the developer application said this bot doesn't."""
    for name in ("get", "search", "timeline", "read", "mentions"):
        assert not hasattr(XClient, name)


def test_tweets_url_is_the_v2_endpoint():
    assert TWEETS_URL.endswith("/2/tweets")
