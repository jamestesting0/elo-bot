from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from elo_bot.db import Match

FINAL_DISTRICTS: Final = (
    "Barbarian Camp", "Balloon Lagoon", "Builder's Workshop", "Dragon Cliffs",
    "Golem Quarry", "Skeleton Park", "Goblin Mines",
)
NODE_IDS: Final = ("QF1", "QF2", "QF3", "QF4", "SF1", "SF2", "F1")
ROUND_NODES: Final = {"Quarterfinals": NODE_IDS[:4], "Semifinals": NODE_IDS[4:6], "Final": NODE_IDS[6:]}
type MatchFormat = Literal["ordinary_three", "elo_final_seven"]
FINAL_SEATS = 8
FINAL_PICK_COUNT: Final = 3
FINAL_PICK_WINDOW: Final = 16 * 60 * 60
# A Final player may scout the opponent's bases this long before their scheduled time; those bases lock then.
FINAL_SCOUT_WINDOW = 30 * 60
# Match threads of an ended or cancelled Final are archived, then deleted after this long.
FINAL_THREAD_RETENTION = 7 * 24 * 60 * 60

type FinalState = Literal["REGISTRATION", "BRACKET_READY", "RUNNING", "COMPLETED",
                          "CANCELLING", "CANCELLED"]
type PredictionState = Literal["NEVER_OPENED", "OPEN", "CLOSED"]
type NodeState = Literal["WAITING", "READY", "BYE", "PLAYING", "DECIDED", "NEEDS_MANAGER"]
type Outcome = Literal["Played", "Forfeit"]


@dataclass(slots=True)
class FinalError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


FINAL_SELF_HOSTING_REFUSAL = ("Final matches can't be self-hosted. A host starts them with "
                              "`/match autostart` or `/match war autostart`.")


def is_final_match(match: "Match") -> bool:
    return getattr(match, "format", None) == "elo_final_seven"
