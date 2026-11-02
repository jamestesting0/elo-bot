import random

from database_models import BaseLayout, Match


def district_base_quality(district: str | None, quality: str) -> str:
    if district == "Wizard Valley":
        return {"Average": "Good", "Good": "Great", "Great": "Excellent", "Excellent": "Excellent"}.get(
            quality, quality,
        )
    if district == "Capital Peak":
        return {"Average": "Average", "Good": "Good", "Great": "Good", "Excellent": "Excellent"}.get(
            quality, quality,
        )
    return quality


async def ensure_secondary_base_index(match: Match, side: int, primary: str, secondary: str) -> Match:
    participant = match.participants[side]
    if participant.secondary_base_index is not None or all(participant.bases):
        return match

    candidates = list(range(len(match.final_districts)))
    # Older partial selections have no saved assignment. Preserve their generated base qualities.
    links = [base.link for base in participant.bases if base and base.expiration_date is None]
    layouts = await BaseLayout.find({"link": {"$in": links}}).to_list() if links else []
    qualities = {layout.link: layout.quality for layout in layouts}
    for index, base in enumerate(participant.bases):
        if base is None or base.expiration_date is not None or base.link not in qualities:
            continue
        district = match.final_districts[index]
        primary_quality = district_base_quality(district, primary)
        secondary_quality = district_base_quality(district, secondary)
        if qualities[base.link] not in (primary_quality, secondary_quality):
            continue
        candidates = [seed for seed in candidates if qualities[base.link] == (
            secondary_quality if seed == index else primary_quality
        )]
    if not candidates:
        raise ValueError("The existing generated bases do not match this match's primary and secondary quality mix.")

    seed = candidates[random.randint(0, len(candidates) - 1)]
    field = f"participants.{side}.secondary_base_index"
    identity = {"_id": match.id, f"participants.{side}.user_id": participant.user_id}
    await match.get_pymongo_collection().update_one(
        {**identity, field: None}, {"$set": {field: seed}},
    )
    # Reload the winning assignment, including when another selector claimed it first.
    fresh = await Match.find_one(identity)
    if fresh is None or fresh.participants[side].secondary_base_index is None:
        raise RuntimeError("The match changed before its base quality assignment could be saved.")
    return fresh
