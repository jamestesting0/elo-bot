import secrets

import interactions as ipy

from common.constants import CROSS_EMOJI, ORDINAL_REPRESENTATIONS, TICK_EMOJI
from database_models import Match, Player, Queue
from database_models.player_models import QueueDistrictSelection


def start_district_selection(player: Player, options: list[list[str]]) -> QueueDistrictSelection:
    return QueueDistrictSelection(
        attempt_id=secrets.token_hex(8),
        districts=[district if district in group else None
                   for district, group in zip(player.district_presets, options)],
    )


def queue_district_components(
        user_id: str, selection: QueueDistrictSelection, options: list[list[str]],
) -> list[ipy.ActionRow]:
    identity = f"{user_id}|{selection.attempt_id}"
    return [
        *[
            ipy.ActionRow(ipy.StringSelectMenu(
                *(ipy.StringSelectOption(label=district, value=district,
                                         default=district == selection.districts[index]) for district in group),
                placeholder=f"{index + 1}️⃣ Select the {ORDINAL_REPRESENTATIONS[index]} district",
                custom_id=f"queued_district|{identity}|{index}",
            )) for index, group in enumerate(options)
        ],
        ipy.ActionRow(
            ipy.Button(style=ipy.ButtonStyle.DANGER, label="Cancel", emoji=CROSS_EMOJI,
                       custom_id=f"queued_cancel|{identity}"),
            ipy.Button(style=ipy.ButtonStyle.SUCCESS, label="Confirm", emoji=TICK_EMOJI,
                       custom_id=f"queued_confirm|{identity}"),
        ),
    ]


async def find_attempt_match(user_id: str, attempt_id: str) -> Match | None:
    return await Match.find_one({"participants": {"$elemMatch": {
        "user_id": user_id, "queue_attempt_id": attempt_id,
    }}})


async def find_attempt_queue(user_id: str, attempt_id: str, tiers: list[int]) -> Queue | None:
    for tier in tiers:
        queue = await Queue.find_one(Queue.tier == tier, fetch_links=True)
        if queue is None:
            continue
        selection = queue.district_selections.get(user_id)
        if (selection is not None and selection.attempt_id == attempt_id
                and any(str(player.user_id) == user_id for player in queue.players)):
            return queue
    return None
