"""Domain types.

Validators return these; processors consume them. Raw dicts never cross a
module boundary — a typo in a Yahoo field name should fail at the parse
boundary, not silently render an empty team name into a public post.
"""

from dataclasses import dataclass, field
from enum import Enum


class TransactionType(str, Enum):
    ADD = "add"
    DROP = "drop"
    ADD_DROP = "add/drop"
    TRADE = "trade"
    COMMISH = "commish"


class PostType(str, Enum):
    TRANSACTION = "transaction"
    SCORES = "scores"
    STANDINGS = "standings"
    MATCHUPS = "matchups"


@dataclass(frozen=True)
class PlayerMove:
    player_name: str
    from_team: str | None
    to_team: str | None


@dataclass(frozen=True)
class Transaction:
    transaction_key: str
    type: TransactionType
    timestamp: int
    moves: tuple[PlayerMove, ...]


@dataclass(frozen=True)
class MatchupResult:
    week: int
    team_a: str
    score_a: float
    team_b: str
    score_b: float

    @property
    def is_tie(self) -> bool:
        return self.score_a == self.score_b

    @property
    def winner(self) -> str | None:
        if self.is_tie:
            return None
        return self.team_a if self.score_a > self.score_b else self.team_b


@dataclass(frozen=True)
class MatchupPairing:
    week: int
    team_a: str
    team_b: str


@dataclass(frozen=True)
class TeamStanding:
    rank: int
    team: str
    wins: int
    losses: int
    ties: int
    points_for: float
    points_against: float


@dataclass(frozen=True)
class RenderedPost:
    """What the publisher receives. Rendering is finished; only sending remains.

    `idempotency_key` is the dedup identity — the transaction key, or
    "<type>#<week>" for scheduled posts. The publisher trusts it completely, so
    producers must derive it from league data, never from a timestamp.
    """

    post_type: PostType
    idempotency_key: str
    segments: tuple[str, ...] = field(default_factory=tuple)

    def to_message(self) -> dict:
        return {
            "post_type": self.post_type.value,
            "idempotency_key": self.idempotency_key,
            "segments": list(self.segments),
        }

    @classmethod
    def from_message(cls, body: dict) -> "RenderedPost":
        return cls(
            post_type=PostType(body["post_type"]),
            idempotency_key=body["idempotency_key"],
            segments=tuple(body["segments"]),
        )
