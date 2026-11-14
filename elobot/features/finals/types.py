from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from elobot.db import Match

FINAL_DISTRICTS: Final = (
    "Barbarian Camp", "Balloon Lagoon", "Builder's Workshop", "Dragon Cliffs",
    "Golem Quarry", "Skeleton Park", "Goblin Mines",
)
NODE_IDS: Final = ("QF1", "QF2", "QF3", "QF4", "SF1", "SF2", "F1")
ROUND_NODES: Final = {"Quarterfinals": NODE_IDS[:4], "Semifinals": NODE_IDS[4:6], "Final": NODE_IDS[6:]}
type MatchFormat = Literal["ordinary_three", "elo_final_seven"]
type FinalState = Literal["REGISTRATION", "BRACKET_READY", "RUNNING", "FINALIZING", "COMPLETED",
                          "CANCELLING", "CANCELLED"]
type PredictionState = Literal["NEVER_OPENED", "OPEN", "CLOSED"]
type NodeState = Literal["WAITING", "READY", "BYE", "PLAYING", "DECIDED", "TIED", "NEEDS_MANAGER"]
type Outcome = Literal["Played", "Forfeit"]


@dataclass(slots=True)
class FinalError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


def is_final_match(match: "Match") -> bool:
    return getattr(match, "format", None) == "elo_final_seven"
