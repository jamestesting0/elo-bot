import interactions as ipy
from pydantic import JsonValue

from elobot.db import BaseStore, Match
from elobot.discord.mentions import mention_user


def live_base_snapshot_filter(match: Match) -> dict[str, JsonValue]:
    # Mongo compares whole embedded documents by field order as well as values.
    query: dict[str, JsonValue] = {}
    for side, participant in enumerate(match.participants):
        prefix = f"participants.{side}.bases"
        query[prefix] = {"$size": len(participant.bases)}
        for index, base in enumerate(participant.bases):
            if base is None:
                query[f"{prefix}.{index}"] = None
            else:
                query.update({f"{prefix}.{index}.{key}": value for key, value in base.model_dump().items()})
    return query


def live_stores(match: Match, reservation_id: str | None = None) -> list[tuple[int, BaseStore]]:
    return [
        (index, participant.base_store)
        for index, participant in enumerate(match.participants[:2])
        if participant.base_store is not None and participant.base_store.is_live
        and (reservation_id is None or participant.base_store.reservation_id == reservation_id)
    ]


def format_live_slot_line(user_id: str, slot: str, names: dict[str, str] | None = None) -> str:
    return f"{(names or {}).get(user_id, mention_user(None, user_id))} — {slot}"


def live_pair_ready(match: Match, reservation_id: str) -> bool:
    stores = live_stores(match, reservation_id)
    return (
        len(stores) == 2
        and stores[0][1].clan_tag == stores[1][1].clan_tag
        and [store.slot for _, store in stores] == ["Slot 1", "Slot 2"]
        and all(store.ready is True for _, store in stores)
    )


def ready_live_reservation_id(match: Match, reservation_id: str | None = None) -> str | None:
    if reservation_id is None:
        stores = live_stores(match)
        reservation_id = stores[0][1].reservation_id if stores else None
    if reservation_id and live_pair_ready(match, reservation_id):
        return reservation_id
    return None


def live_store_components(match: Match, reservation_id: str, *, allow_start: bool = False) -> list[ipy.Button]:
    buttons = []
    if allow_start:
        buttons.append(ipy.Button(
            style=ipy.ButtonStyle.GRAY, label="Start Match", emoji="⚔️",
            custom_id=f"helper_stored_live_start|{match.token}|{reservation_id}",
        ))
    if live_stores(match, reservation_id):
        buttons.append(ipy.Button(
            style=ipy.ButtonStyle.DANGER, label="Undo", emoji=ipy.PartialEmoji(name="🛑"),
            custom_id=f"helper_stored_live_undo|{match.token}|{reservation_id}",
        ))
    return buttons
