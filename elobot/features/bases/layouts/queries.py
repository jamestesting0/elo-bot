import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.queries.find import FindMany
from pydantic import BaseModel, ConfigDict, Field

from elobot.db import BaseLayout


class BaseAutocompleteRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    district: str
    builder_id: str | None = None


class BaseExtractionAutocompleteRow(BaseAutocompleteRow):
    id: PydanticObjectId = Field(alias="_id")
    link: str


def _resolve_builder_id(bot: ipy.Client, builder_id: str) -> str | None:
    return None if builder_id == str(bot.user.id) else builder_id


async def sort_bases(query: FindMany[BaseLayout]) -> list[BaseLayout]:
    query = query.find({"records.5": {"$exists": True}})

    bases = await query.to_list()

    bases_sorted = sorted(
        bases,
        key=lambda base: sum((r[1] * 1000 + r[2]) for r in base.records) / len(base.records),
        reverse=False
    )

    return bases_sorted


async def _one_listed_base(
        query: FindMany[BaseLayout], index: int, *, total: int | None = None,
) -> tuple[BaseLayout | None, int]:
    if total is None:
        total = await query.count()
    if index < 0 or index >= total:
        return None, total

    matches = await query.sort("_id").skip(index).limit(1).to_list()
    return (matches[0] if matches else None), total


def _public_list_query(bot: ipy.Client, district: str, base_quality: str, builder_id: str):
    query = BaseLayout.find(BaseLayout.quality != "Private")
    if district != "Any":
        query = query.find(BaseLayout.district == district)
    if base_quality != "Any":
        query = query.find(BaseLayout.quality == base_quality)
    if builder_id != "Any":
        query = query.find(BaseLayout.builder_id == _resolve_builder_id(bot, builder_id))
    return query
