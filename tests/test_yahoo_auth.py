"""Token lifecycle. moto for SSM (never MagicMock for an AWS service);
`responses` for Yahoo's HTTP, which moto cannot simulate."""

import boto3
import pytest
import responses
from moto import mock_aws
from shared.yahoo_auth import TOKEN_URL, TokenManager, YahooAuthError

PREFIX = "/gotffl/yahoo"
NOW = 1_700_000_000


@pytest.fixture
def ssm():
    with mock_aws():
        client = boto3.client("ssm", region_name="ca-central-1")
        for name, value in [
            ("refresh_token", "refresh-original"),
            ("client_id", "cid"),
            ("client_secret", "csecret"),
        ]:
            client.put_parameter(Name=f"{PREFIX}/{name}", Value=value, Type="SecureString")
        yield client


def param(ssm, name):
    return ssm.get_parameter(Name=f"{PREFIX}/{name}", WithDecryption=True)["Parameter"]["Value"]


def stub_refresh(**overrides):
    body = {"access_token": "access-new", "expires_in": 3600}
    body.update(overrides)
    responses.add(responses.POST, TOKEN_URL, json=body, status=200)


@responses.activate
def test_refreshes_when_no_cached_token(ssm):
    stub_refresh()
    assert TokenManager(PREFIX, ssm).access_token(now=NOW) == "access-new"
    assert param(ssm, "access_token") == "access-new"
    assert param(ssm, "access_token_expires_at") == str(NOW + 3600)


@responses.activate
def test_uses_cached_token_without_calling_yahoo(ssm):
    ssm.put_parameter(Name=f"{PREFIX}/access_token", Value="cached", Type="SecureString")
    ssm.put_parameter(
        Name=f"{PREFIX}/access_token_expires_at", Value=str(NOW + 3600), Type="SecureString"
    )
    assert TokenManager(PREFIX, ssm).access_token(now=NOW) == "cached"
    assert len(responses.calls) == 0, "a valid cached token must not trigger a refresh"


@responses.activate
def test_refreshes_inside_the_expiry_margin(ssm):
    """Within 5 minutes of expiry, refresh — a token that dies mid-request is
    an avoidable 401."""
    ssm.put_parameter(Name=f"{PREFIX}/access_token", Value="stale", Type="SecureString")
    ssm.put_parameter(
        Name=f"{PREFIX}/access_token_expires_at", Value=str(NOW + 60), Type="SecureString"
    )
    stub_refresh()
    assert TokenManager(PREFIX, ssm).access_token(now=NOW) == "access-new"


@responses.activate
def test_expired_token_refreshes(ssm):
    ssm.put_parameter(Name=f"{PREFIX}/access_token", Value="old", Type="SecureString")
    ssm.put_parameter(
        Name=f"{PREFIX}/access_token_expires_at", Value=str(NOW - 10), Type="SecureString"
    )
    stub_refresh()
    assert TokenManager(PREFIX, ssm).access_token(now=NOW) == "access-new"


@responses.activate
def test_rotated_refresh_token_is_written_back(ssm):
    """Losing a rotated refresh token costs all future access."""
    stub_refresh(refresh_token="refresh-rotated")
    TokenManager(PREFIX, ssm).access_token(now=NOW)
    assert param(ssm, "refresh_token") == "refresh-rotated"


@responses.activate
def test_unrotated_refresh_token_is_left_alone(ssm):
    stub_refresh(refresh_token="refresh-original")
    TokenManager(PREFIX, ssm).access_token(now=NOW)
    assert param(ssm, "refresh_token") == "refresh-original"


@responses.activate
def test_corrupt_expiry_forces_refresh_rather_than_trusting_it(ssm):
    ssm.put_parameter(Name=f"{PREFIX}/access_token", Value="cached", Type="SecureString")
    ssm.put_parameter(
        Name=f"{PREFIX}/access_token_expires_at", Value="not-a-number", Type="SecureString"
    )
    stub_refresh()
    assert TokenManager(PREFIX, ssm).access_token(now=NOW) == "access-new"


@responses.activate
@pytest.mark.parametrize("status", [400, 401])
def test_rejected_refresh_raises_auth_error_and_does_not_retry(ssm, status):
    responses.add(responses.POST, TOKEN_URL, json={"error": "invalid_grant"}, status=status)
    with pytest.raises(YahooAuthError, match="re-authorization"):
        TokenManager(PREFIX, ssm).access_token(now=NOW)


@responses.activate
def test_server_error_raises_for_status_so_lambda_retries(ssm):
    import requests

    responses.add(responses.POST, TOKEN_URL, status=503)
    with pytest.raises(requests.HTTPError):
        TokenManager(PREFIX, ssm).access_token(now=NOW)


@responses.activate
def test_missing_credentials_raises_auth_error(ssm):
    ssm.delete_parameter(Name=f"{PREFIX}/refresh_token")
    with pytest.raises(YahooAuthError, match="missing"):
        TokenManager(PREFIX, ssm).access_token(now=NOW)


# ------------------------------------------------------- concurrent refresh

@responses.activate
def test_concurrent_refresh_is_serialised_by_the_lock(ssm, monkeypatch):
    """Two invocations refresh at once. Yahoo rotates on use, so the loser must
    reuse the winner's token rather than obtaining a second one that
    invalidates it."""
    import shared.yahoo_auth as auth

    calls = {"n": 0}

    def one_winner(kind, key):
        calls["n"] += 1
        return calls["n"] == 1

    monkeypatch.setattr(auth.TokenManager, "_acquire_refresh_lock",
                        staticmethod(lambda now: one_winner("YAHOO_REFRESH", now)))
    stub_refresh()

    winner = TokenManager(PREFIX, ssm).access_token(now=NOW)
    loser = TokenManager(PREFIX, ssm).access_token(now=NOW)

    assert winner == loser == "access-new"
    assert len(responses.calls) == 1, "the loser must not call Yahoo a second time"


@responses.activate
def test_lock_loser_refreshes_anyway_if_no_fresh_token_appeared(ssm, monkeypatch):
    """If the lock is held but the winner has not written yet, refusing to
    refresh would return no token at all. Refresh and accept the small risk."""
    import shared.yahoo_auth as auth

    monkeypatch.setattr(auth.TokenManager, "_acquire_refresh_lock",
                        staticmethod(lambda now: False))
    stub_refresh()
    assert TokenManager(PREFIX, ssm).access_token(now=NOW) == "access-new"


@responses.activate
def test_lock_failure_does_not_block_authentication(ssm, monkeypatch):
    """No state table (or no permission) must not mean no token."""
    import shared.idempotency as idem

    def explode(*args, **kwargs):
        raise RuntimeError("no table")

    monkeypatch.setattr(idem, "claim", explode)
    stub_refresh()
    assert TokenManager(PREFIX, ssm).access_token(now=NOW) == "access-new"
