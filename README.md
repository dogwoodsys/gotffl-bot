# Game of Throws Bot

A private, non-commercial bot that posts activity from a single 12-team recreational
fantasy football league on Yahoo Fantasy Sports to a dedicated social media account,
so league members can follow what's happening in one place.

Built by an individual developer for their own league. Not a product, not a service,
not for sale.

## What it does

The bot reads league data from the Yahoo Fantasy Sports API and posts short text
summaries of four things:

| What | When |
|---|---|
| Transactions — waiver claims, trades, free-agent adds and drops | Within a few minutes of happening |
| Final matchup scores for the completed week | Tuesdays, 6:00 AM ET |
| League standings | Tuesdays, 12:00 PM ET |
| Upcoming week's matchup pairings | Wednesdays, 12:00 PM ET |

A typical post looks like:

```
Trade: Team A sends Player X to Team B for Player Y.
```

The bot is active during the NFL season (September–January) and dormant the rest of
the year.

## What it does not do

- **It never writes to Yahoo.** Read-only access only. It does not modify rosters,
  submit waiver claims, propose trades, or change any league setting.
- **It does not redistribute Yahoo data.** Nothing is published to a website, resold,
  shared with third parties, or displayed anywhere except as brief summaries of this
  one league's own activity, posted for that league's own members.
- **It stores almost nothing.** The only persisted data is the IDs of transactions it
  has already posted, so it doesn't post the same thing twice. These expire after 90
  days.
- **It does not read the social timeline.** The posting account is write-only. It does
  not reply to, mention, follow, search, or message anyone.

## Request volume

Low and seasonal. During the NFL season: one polling request every few minutes to
check for new transactions, plus three scheduled requests per week for scores,
standings, and matchups. Outside the season the bot makes no requests at all.

The polling interval is configurable and will be tuned to stay comfortably inside
whatever rate limits apply.

## How it's built

Python on AWS, in the Montreal region. Deliberately small:

```
  EventBridge (schedules)
        │
        ▼
  Reader functions ──► queue ──► publisher function ──► social API
        │                              │
        ▼                              ▼
   Yahoo Fantasy API            (posts, never reads)
        │
        ▼
   DynamoDB (posted-transaction IDs only)
```

The functions that read Yahoo have no ability to post, and the function that posts has
no ability to read Yahoo. Credentials live in AWS Systems Manager Parameter Store as
encrypted parameters — never in this repository, never in environment variables, never
on disk.

Every post is rendered by a pure template function with no generative AI involved. The
output for a given input is deterministic and testable, which is the point: this bot
should be boring and correct.

### Safety before it goes live

The bot ships in a shadow mode that renders every post and writes it to a log without
sending anything. It runs a full week that way and the output gets reviewed by a human
before a single post is published.

## Status

In development. Not yet live — no post has ever been sent.

The application code and infrastructure are complete and tested (356 tests,
98% coverage, 100% on every handler, processor, and validator). What remains is
credentials: the Yahoo app is awaiting approval, and the social developer
account is not yet registered.

See [docs/RUNBOOK.md](docs/RUNBOOK.md) for deployment and operations.

## License

MIT — see [LICENSE](LICENSE).
