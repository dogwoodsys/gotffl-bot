# Runbook

## What is blocked, and on whom

| # | Step | Owner | Blocked by |
|---|---|---|---|
| 1 | `cdk deploy` | Mallory | Claude Code's auto-mode classifier refuses IAM-creating deploys. Approve interactively or add a Bash allow-rule. |
| 2 | Yahoo app approval | Matthew | Submitted; awaiting Yahoo |
| 3 | X developer account | — | Done — registered, no approval required |
| 4 | League key | — | Done — in git-ignored `.env.deploy` |
| 5 | Cost Explorer enable | — | Done — tag activation possible ~24h later |
| 6 | X credentials into SSM | Mallory | Four SecureString parameters |

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
./scripts/deploy.sh diff      # review first
./scripts/deploy.sh deploy
```

The script reads `LEAGUE_KEY` from `.env.deploy`, which is **git-ignored** —
this repo is public and the league ID identifies a real private league. It is
not a credential, but it does not need to be readable by strangers. Copy
`.env.deploy.example` to `.env.deploy` on any new machine.

Deploying without a league key leaves it as `UNSET`, and every reader refuses
at validation rather than guessing a league.

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
./scripts/put-credentials.sh x       # four X OAuth 1.0a values
python scripts/yahoo_bootstrap.py    # Yahoo, after approval
```

**Use the script, not a bare `aws ssm put-parameter`.** A bare command uses
whatever profile the shell happens to have; an `export` that doesn't carry
between invocations will silently write live credentials to the wrong AWS
account. The script pins the profile from `.env.deploy` and refuses to write
unless the caller is account `159198628641`. This has already happened once.

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
| `gotffl-no-posts-7-days` | Nothing posted in 7 days during season | The dangerous one: healthy, erroring on nothing, silently posting nothing. Check the poller log and Yahoo auth. |

## Recovering from a failed first deploy

The state table carries `RemovalPolicy.RETAIN` so a later `cdk destroy` cannot
take the dedup history with it. On a *failed first create* that same policy
leaves the table behind, and the next deploy then fails with "table already
exists". If the table is empty, drop it and the rolled-back stack:

```bash
source .env.deploy; export AWS_PROFILE AWS_REGION
aws dynamodb describe-table --table-name gotffl-state \
  --query 'Table.{Items:ItemCount,Bytes:TableSizeBytes}'   # confirm it is empty FIRST
aws dynamodb delete-table --table-name gotffl-state
aws cloudformation delete-stack --stack-name gotffl
aws cloudformation wait stack-delete-complete --stack-name gotffl
./scripts/deploy.sh deploy
```

If the table is **not** empty, do not delete it — it holds the record of what
has already been posted, and losing it means the bot re-posts history. Import
it into the new stack instead.

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
