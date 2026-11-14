from collections.abc import Mapping

from beanie import PydanticObjectId
from pydantic import JsonValue


def _participant_id(participant: JsonValue) -> JsonValue:
    assert isinstance(participant, dict)
    return participant["user_id"]


def match_slot_changes(
        changes: dict[str, JsonValue | PydanticObjectId],
        saved_state: Mapping[str, JsonValue | PydanticObjectId],
) -> dict[str, JsonValue | PydanticObjectId]:
    """Split fixed match slots without splitting base links, reservations, or structural replacements."""
    for field in (
            "submitters", "hosts", "host_clans", "persistent_host_clans", "times", "old_msgs", "final_districts",
            "message_publications",
    ):
        if field not in changes:
            continue
        previous = saved_state[field]
        current = changes[field]
        assert isinstance(previous, list) and isinstance(current, list)
        if len(previous) != len(current):
            continue
        del changes[field]
        for index, (before, after) in enumerate(zip(previous, current, strict=True)):
            if before != after:
                changes[f"{field}.{index}"] = after

    if "participants" not in changes:
        return changes

    previous = saved_state["participants"]
    current = changes["participants"]
    assert isinstance(previous, list) and isinstance(current, list)
    previous_ids = [_participant_id(participant) for participant in previous]
    current_ids = [_participant_id(participant) for participant in current]
    if previous_ids != current_ids:
        return changes

    del changes["participants"]
    for index, (before, after) in enumerate(zip(previous, current, strict=True)):
        assert isinstance(before, dict) and isinstance(after, dict)
        for field, value in after.items():
            if field not in before or before[field] != value:
                path = f"participants.{index}.{field}"
                if field in {"districts", "bases", "result"} and field in before:
                    previous_slots = before[field]
                    assert isinstance(previous_slots, list) and isinstance(value, list)
                    if len(previous_slots) == len(value):
                        for slot, (old_value, new_value) in enumerate(zip(previous_slots, value, strict=True)):
                            if old_value != new_value:
                                changes[f"{path}.{slot}"] = new_value
                        continue
                changes[path] = value
    return changes
