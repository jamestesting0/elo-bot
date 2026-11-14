from dataclasses import dataclass

import interactions as ipy

from elobot.db import Match
from elobot.features.results.validation import is_match_finished


@dataclass(frozen=True, slots=True)
class PlayerOccupied:
    message: str
    component: ipy.Button | None = None


async def is_player_occupied(player_id: str) -> PlayerOccupied | None:
    matches = await Match.find(
        {"participants.user_id": str(player_id)}
    ).to_list()

    for match in matches:
        if match.is_autowin:
            continue
        
        participant = next((p for p in match.participants if p.user_id == str(player_id)))
        opponent = next(p for p in match.participants if p.user_id != str(player_id))
        # unconfirmed challenge where this player created it
        if not match.thread and match.is_challenge:
            if match.participants[0].user_id == str(player_id):
                return PlayerOccupied(f"You have an unconfirmed challenge request against <@{opponent.user_id}>.")
            if match.participants[1].user_id == str(player_id):
                return PlayerOccupied(f"<@{opponent.user_id}> has challenged to you, please accept or reject it.")

        # unconfirmed normal match (district selection stage)
        if not match.thread and not match.is_challenge:
            if participant.confirmed:
                return PlayerOccupied(f"You have an upcoming match against <@{opponent.user_id}>.")
            else:
                return PlayerOccupied(
                    "You have an unconfirmed match, please check your DM or confirm it now.",
                    ipy.Button(
                        style=ipy.ButtonStyle.GRAY,
                        label="Confirm Match",
                        custom_id=f"confirm_occupied_match|{match.token}"
                    ),
                )

        # regular matches finish together; war matches finish per player
        finished_player = match.participants.index(participant) if match.is_war else None
        if not is_match_finished(match, player=finished_player):
            return PlayerOccupied(f"You have an unfinished match in <#{match.thread}>.")

    return None
