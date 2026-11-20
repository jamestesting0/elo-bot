import random
from uuid import uuid4

from elo_bot.core.finals import FinalError
from elo_bot.core.token_items import TokenItem
from elo_bot.db import Match, Player, Queue
from elo_bot.db.token_models import TokenItemUse, TokenWallet
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.tokens.items import finish_use
from elo_bot.features.tokens.ranked import reserve_protections
from elo_bot.features.queue.policy import QUEUE_OPERATION_LOCK


def is_boosted_attempt(attempt: str | None) -> bool:
    return attempt is not None and attempt.startswith("queue:boost:")


async def reserve_queue_items(player: Player) -> tuple[str | None, int]:
    if player.token_items_active is not True:
        return None, 0
    wallet = await TokenWallet.find_one({"user_id": player.user_id})
    if wallet is None:
        return None, 0
    boost = await TokenItemUse.find_one({"wallet_id": wallet.id, "state": "armed", "slot": "queue"})
    target = f"queue:{'boost' if boost else 'district'}:{uuid4().hex}"
    collection = TokenItemUse.get_pymongo_collection()
    slots = [{"slot": "district"}]
    if boost is not None:
        slots.append({"_id": boost.id})
    acquired = await collection.update_many(
        {"wallet_id": wallet.id, "state": "armed", "$or": slots},
        {"$set": {"state": "reserved", "target": target}},
    )
    if not acquired.modified_count:
        return None, 0
    if boost is None:
        return target, 0
    return target, 2 if boost.item is TokenItem.PRIORITY_ULTRA else 1


async def release_queue_items(target: str | None) -> None:
    if target is None:
        return
    async def release(session):
        async for row in TokenItemUse.get_pymongo_collection().find({"target": target, "state": "reserved"},
                                                                    session=session):
            await finish_use(TokenItemUse.model_validate(row), session, consume=False)

    await transaction(release)


async def release_orphan_queue_items() -> None:
    async with QUEUE_OPERATION_LOCK:
        pending = await TokenItemUse.find({"state": "reserved", "target": {"$regex": "^queue:"}}).to_list()
        if not pending:
            return
        queues = await Queue.find_all().to_list()
        active = {attempt for queue in queues for attempt in queue.token_attempts.values()}
        for target in {item.target for item in pending} - active:
            await release_queue_items(target)


async def insert_queued_match(match: Match, options: list[list[str]], attempts: tuple[str | None, str | None]) -> None:
    targets = [target for target in attempts if target is not None]
    async def insert(session):
        uses = TokenItemUse.get_pymongo_collection()
        match.token_protections.clear()
        match.token_districts.clear()
        candidates = [TokenItemUse.model_validate(row) async for row in uses.find(
            {"target": {"$in": targets}, "state": "reserved"}, session=session)]
        for use in candidates:
            if use.slot == "queue":
                await finish_use(use, session, consume=True)
        for group, eligible in enumerate(options):
            choices = [use for use in candidates if use.district in eligible]
            if choices:
                selected = random.choice(choices).district
                match.token_districts[str(group)] = selected
                for use in choices:
                    if use.district == selected:
                        await uses.update_one({"_id": use.id, "state": "reserved", "target": use.target},
                                              {"$set": {"target": str(match.id)}}, session=session)
                    else:
                        await finish_use(use, session, consume=False)
        for use in candidates:
            if use.slot == "district" and all(use.district not in eligible for eligible in options):
                await finish_use(use, session, consume=False)
        await reserve_protections(match, session)
        await match.insert(session=session)

    await transaction(insert)


async def finish_district_items(match: Match, *, consume: bool, session=None) -> None:
    if not match.token_districts:
        return
    async def finish(session):
        async for row in TokenItemUse.get_pymongo_collection().find(
                {"target": str(match.id), "slot": "district", "state": "reserved"}, session=session):
            if not await finish_use(TokenItemUse.model_validate(row), session, consume=consume):
                raise FinalError("The district reservation changed.")
    if session is not None:
        await finish(session)
    else:
        await transaction(finish)
