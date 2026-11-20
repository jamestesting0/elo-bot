import asyncio
import logging
import secrets
from pathlib import Path

import anyio
import interactions as ipy
from beanie import PydanticObjectId

from elo_bot.core.finals import FINAL_DISTRICTS
from elo_bot.db import Match
from elo_bot.features.bases.screenshots import capture_base_screenshot
from elo_bot.features.matches.controls import update_match

_CAPTURES: dict[tuple[PydanticObjectId, int], asyncio.Task[None]] = {}
_CAPTURE_LOCK = anyio.Lock()


async def store_scout_image(bot: ipy.Client, base_link: str, requested_by: str, *,
                            ctx: ipy.InteractionContext | None = None) -> str | None:
    path = Path(f"temp/{secrets.token_hex(8)}.webp")
    if not await capture_base_screenshot(base_link, requested_by, str(path), ctx=ctx, keep_secret=True,
                                         scout_view=True):
        return None
    try:
        message = await bot.owner.send(file=ipy.File(str(path)), silent=True)
    finally:
        path.unlink(missing_ok=True)
    return message.attachments[0].url


async def _capture_scout_views(bot: ipy.Client, match_id: PydanticObjectId, defender: int) -> None:
    # Keep one background Helper capture active while other sides wait their turn.
    async with _CAPTURE_LOCK:
        for index in range(len(FINAL_DISTRICTS)):
            match = await Match.find_one({"_id": match_id, "final_superseded": False, "final_result_id": None,
                                          "final_result_operation": None})
            if match is None:
                return
            base = match.participants[defender].bases[index]
            if base is None or base.scout_view:
                continue
            try:
                image = await store_scout_image(bot, base.link, str(bot.user.id))
            except ipy.errors.HTTPException:
                logging.exception("Could not upload the Final scout image for match %s district %s", match.token, index)
                continue
            if image is None:
                continue
            field = f"participants.{defender}.bases.{index}"
            await update_match(match, {"_id": match.id, f"{field}.link": base.link,
                                       f"{field}.scout_view": {"$ne": True}},
                               {"$set": {f"{field}.image": image, f"{field}.scout_view": True}})


def request_scout_views(bot: ipy.Client, match_id: PydanticObjectId, defender: int) -> None:
    """Queue private captures once per side without waiting for Helper."""
    key = (match_id, defender)
    if key in _CAPTURES:
        return

    def finished(task: asyncio.Task[None]) -> None:
        _CAPTURES.pop(key)
        if not task.cancelled() and (error := task.exception()) is not None:
            logging.error("Could not prepare Final scout images for match %s side %s", *key, exc_info=error)

    task = asyncio.create_task(_capture_scout_views(bot, match_id, defender), name=f"final-scout:{match_id}:{defender}")
    _CAPTURES[key] = task
    task.add_done_callback(finished)
