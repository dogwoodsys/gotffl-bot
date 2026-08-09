# Runbook

## What is blocked, and on whom

| # | Step | Owner | Blocked by |
|---|---|---|---|
| 1 | `cdk deploy` | Mallory | Claude Code's auto-mode classifier refuses IAM-creating deploys. Approve interactively or add a Bash allow-rule. |
| 2 | Yahoo app approval | Matthew | Submitted; awaiting Yahoo |
| 3 | X developer account | Matthew | Application drafted |
| 4 | League key | Matthew | See "Finding the league key" below |
| 5 | Cost Explorer enable | Mallory | Console only; ~24h to populate |

Everything else is built, tested, and committed. Nothing runs until step 1, and
even then every schedule ships **disabled**.

## Finding the league key

The key is `nfl.l.<league id>`. The league ID is the number in the URL when the
league is open in a browser (not the phone app):

    https://football.fantasysports.yahoo.com/f1/482910   ->   nfl.l.482910

`nfl` is Yahoo's shorthand for the current season, which avoids looking up the
season-specific game code that changes every year.

If Matthew is in more than one league, confirm which is which — a league ID
alone gives no way to tell them apart, and the wrong one posts another league's
trades. After the Yahoo bootstrap, this lists them all by name:

```bash
AWS_PROFILE=gotffl-admin python scripts/find_league_key.py
```

## Deploy

```bash
cd ~/projects/gotffl-bot
export AWS_PROFILE=gotffl-admin AWS_REGION=ca-central-1
./node_modules/.bin/cdk diff -c league_key=461.l.XXXXXX      # review first
./node_modules/.bin/cdk deploy -c league_key=461.l.XXXXXX
```

Without `-c league_key`, the stack deploys with `UNSET` and every reader
refuses at validation rather than guessing a league.

## Parameters

Created by hand or by the bootstrap script — never by CDK, so a `cdk destroy`
cannot delete a credential.

| Parameter | Type | Set by |
|---|---|---|
| `/gotffl/yahoo/client_id` | SecureString | `scripts/yahoo_bootstrap.py` |
| `/gotffl/yahoo/client_secret` | SecureString | `scripts/yahoo_bootstrap.py` |
| `/gotffl/yahoo/refresh_token` | SecureString | `scripts/yahoo_bootstrap.py` |
| `/gotffl/yahoo/access_token` | SecureString | written by the poller |
| `/gotffl/yahoo/access_token_expires_at` | SecureString | written by the poller |
| `/gotffl/x/consumer_key` | SecureString | by hand |
| `/gotffl/x/consumer_secret` | SecureString | by hand |
| `/gotffl/x/access_token` | SecureString | by hand |
| `/gotffl/x/access_token_secret` | SecureString | by hand |
| `/gotffl/shadow_mode` | String | by hand — `true` until launch |

```bash
aws ssm put-parameter --name /gotffl/shadow_mode --value true --type String
for k in consumer_key consumer_secret access_token access_token_secret; do
  read -rsp "X $k: " v && echo
  aws ssm put-parameter --name "/gotffl/x/$k" --value "$v" --type SecureString --overwrite
done
python scripts/yahoo_bootstrap.py
```

## Shadow week

1. Confirm `/gotffl/shadow_mode` is `true`.
2. Enable the schedules (they deploy disabled):
   ```bash
   aws events enable-rule --name gotffl-poll
   for s in scores standings matchups matchups-retry; do
     aws scheduler update-schedule --name "gotffl-$s" --state ENABLED \
       --schedule-expression "$(aws scheduler get-schedule --name gotffl-$s \
         --query ScheduleExpression --output text)" \
       --flexible-time-window Mode=OFF
   done
   ```
3. **Verify the Yahoo parsers against a real payload.** The parsers in
   `shared/yahoo.py` were written from Yahoo's documented shape, not a recorded
   response — this is the single largest untested assumption in the project.
   Capture one real payload per endpoint, commit it under `tests/fixtures/`,
   and re-run the parser tests against it.
4. Read every rendered post:
   ```bash
   aws dynamodb scan --table-name gotffl-state \
     --filter-expression 'begins_with(pk, :p)' \
     --expression-attribute-values '{":p":{"S":"SHADOW#"}}' \
     --query 'Items[].segments.L[].S' --output text
   ```
5. Tune wording in `shared/render.py`, re-run `pytest`, redeploy.

## Going live

```bash
aws ssm put-parameter --name /gotffl/shadow_mode --value false \
  --type String --overwrite
```

No redeploy. To stop immediately, set it back to `true` — the next message is
shadowed. Anything other than the literal `false` means shadow, so a typo
fails safe.

## Alarms

All to SNS `gotffl-alerts`.

| Alarm | Means | Do |
|---|---|---|
| `gotffl-*-errors` | A Lambda threw | Check its log group |
| `gotffl-outbox-dlq-not-empty` | A post will never send | Inspect the message, fix, redrive |
| `gotffl-failure-dlq-not-empty` | A reader or schedule failed after retries | Check the reader's log |
| `gotffl-post-rate-high` | >25 posts/day | **Set shadow_mode=true immediately**, then investigate — this is the dedup-failure signature |
| `gotffl-no-posts-8-days` | Nothing posted in 8 days during season | The dangerous one: healthy, erroring on nothing, silently posting nothing. Check the poller log and Yahoo auth. |

## Known failure modes

**Yahoo returns 401 repeatedly.** The refresh token was revoked or expired.
Re-run `scripts/yahoo_bootstrap.py`. The bot goes quiet rather than posting
garbage, so no cleanup is needed.

**A thread posted partially.** The publisher confirms the key even on partial
failure — re-sending would duplicate what is already public. Post the remainder
by hand, or delete the partial thread and clear
`POST#<key>` from `gotffl-state` to allow a clean retry.

**Yahoo changed its JSON shape.** Parsers raise `YahooParseError` rather than
returning empty, so this surfaces as an error alarm within minutes instead of
silence. Capture the new payload, update the parser, add it as a fixture.
