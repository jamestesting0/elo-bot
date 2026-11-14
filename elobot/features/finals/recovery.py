import asyncio
from collections.abc import Awaitable, Callable
from hashlib import sha256

import interactions as ipy
import anyio
from beanie import PydanticObjectId
from pymongo.errors import PyMongoError

from elobot.discord.message_edits import edit_channel_message
from elobot.features.finals.lifecycle import resolve_final
from elobot.features.finals.persistence import transaction
from elobot.features.finals.predictions import rescore_predictions
from elobot.features.finals.profiles import rebuild_profiles
from elobot.features.finals.publication import PUBLICATION_LOCKS, publish_bracket
from elobot.features.finals.result_ui import result_embed
from elobot.core.finals import FinalError
from elobot.features.results.helper_credits import award_helper_once
from elobot.features.hosting.self_hosted.invitations import invite_eligible_players
from elobot.features.tokens.settlement import recover_market, set_settlement_intent
from elobot.features.tokens.presentation import market_signature, publish_market
from elobot.discord.mentions import mention_user
from elobot.db import EloFinal, Match
from elobot.db.final_models import FinalMatchResult
from elobot.db.token_models import FinalBettingMarket
from elobot.features.base_store.storage import BaseStoreReleaseConflict, BaseStoreReleaseDenied
from elobot.features.finals.matches import bind_thread


async def select_financial_work(final_id: PydanticObjectId) -> tuple[EloFinal, list[str]]:
    async def select(session):
        collection = EloFinal.get_pymongo_collection()
        final = EloFinal.model_validate(await collection.find_one({"_id": final_id}, session=session))
        markets = await FinalBettingMarket.get_pymongo_collection().find({"final_id": str(final.id)}, session=session).to_list()
        selected = []
        for row in markets:
            market = FinalBettingMarket.model_validate(row)
            node = final.nodes.get(market.node_id)
            invalidated = node is None or node.contestant_revision != market.contestant_revision
            cancelled = final.state in {"CANCELLING", "CANCELLED"} and (node is None or not node.winner)
            if market.void_reason or invalidated or cancelled or node.result_id and node.winner:
                refund = bool(market.void_reason) or invalidated or cancelled or node.outcome == "Forfeit"
                await set_settlement_intent(market.id, final.result_revision, winner=None if refund else node.winner,
                                            refund=refund, session=session)
                selected.append(market.id)
            elif node.result_id and not node.winner:
                await FinalBettingMarket.get_pymongo_collection().update_one({"_id": market.id, "state": "OPEN"},
                    {"$set": {"state": "CLOSED"}, "$inc": {"version": 1}}, session=session)
        await rescore_predictions(final, session)
        await rebuild_profiles(final, session)
        await collection.update_one({"_id": final.id}, {"$set": {"predictions_scoring_revision": final.result_revision},
                                                        "$inc": {"revision": 1}}, session=session)
        return final, selected

    return await transaction(select)


async def mark_deleted_attempt(final, node, source) -> None:
    async def mark(session):
        updated = await EloFinal.get_pymongo_collection().update_one({"_id": final.id,
            f"nodes.{node.id}.match_id": node.match_id, f"nodes.{node.id}.result_id": None,
            f"nodes.{node.id}.contestant_revision": node.contestant_revision}, {"$set": {
                f"nodes.{node.id}.match_id": None, f"nodes.{node.id}.thread_id": None,
                f"nodes.{node.id}.provisioning": False, f"nodes.{node.id}.state": "NEEDS_MANAGER",
                f"nodes.{node.id}.provisioning_error": "Thread deleted. A manager must arrange this pairing."},
            "$inc": {"revision": 1}}, session=session)
        if updated.matched_count and source:
            await Match.get_pymongo_collection().update_one({"_id": source.id},
                {"$set": {"final_superseded": True}}, session=session)
    await transaction(mark)


async def publish_result(bot, final: EloFinal, result: FinalMatchResult) -> None:
    key = result.id.replace(".", "_")
    async with PUBLICATION_LOCKS.setdefault(f"result:{result.id}", anyio.Lock()):
        final = await resolve_final(final.season_number)
        progress = final.result_progress[key]
        if (progress.published and not progress.error
                and progress.published_repair_pending == final.repair_pending):
            return
        collection = EloFinal.get_pymongo_collection()
        path = f"result_progress.{key}"
        channel_id = progress.channel_id or result.source_thread or str(final.channel_id)
        try:
            try:
                channel = await bot.fetch_channel(int(channel_id))
            except ipy.errors.NotFound:
                progress.message_id = None
                progress.send_attempted = False
                progress.thread_deleted = True
                channel_id = str(final.channel_id)
                await collection.update_one({"_id": final.id}, {"$set": {f"{path}.thread_deleted": True,
                    f"{path}.message_id": None, f"{path}.send_attempted": False, f"{path}.channel_id": channel_id}})
                channel = await bot.fetch_channel(final.channel_id)
            nonce = sha256(f"final-result:{result.id}".encode()).hexdigest()[:24]
            message = await channel.fetch_message(int(progress.message_id)) if progress.message_id else None
            if message is None and progress.send_attempted:
                messages = await channel.fetch_messages(limit=100)
                message = next((item for item in messages if str(item.nonce) == nonce and item.author.id == bot.user.id), None)
                if message is None:
                    raise FinalError(f"{result.node_id} result publication is uncertain. Locate its earlier message.")
            members = await asyncio.gather(*(channel.guild.fetch_member(user) for user in result.snapshot.contestants))
            names = {user: mention_user(member, user) for user, member in zip(result.snapshot.contestants, members)}
            await collection.update_one({"_id": final.id}, {"$set": {f"{path}.send_attempted": True,
                                                                         f"{path}.channel_id": channel_id}})
            if message is None:
                message = await channel.send(embed=result_embed(final, result, names), nonce=nonce, enforce_nonce=True)
            else:
                await edit_channel_message(message, embed=result_embed(final, result, names), components=[])
            await collection.update_one({"_id": final.id}, {"$set": {f"{path}.message_id": str(message.id),
                f"{path}.published": True, f"{path}.published_repair_pending": final.repair_pending,
                f"{path}.error": None}})
            if result.source_match_id:
                await Match.get_pymongo_collection().update_one({"_id": PydanticObjectId(result.source_match_id),
                    "final_result_id": result.id}, {"$set": {"final_result_operation.published": True,
                                                            "final_result_operation.publication_error": None}})
        except (ipy.errors.HTTPException, FinalError) as error:
            await collection.update_one({"_id": final.id}, {"$set": {f"{path}.error": str(error)}})
            raise FinalError(f"{result.node_id} result saved; publication remains pending.") from error


async def recover_final(final: EloFinal, *, bot=None,
                        release_completed: Callable[[Match], Awaitable[None]] | None = None,
                        release_deleted: Callable[[Match], Awaitable[None]] | None = None) -> list[str]:
    final, markets = await select_financial_work(final.id)
    pending = []
    for market_id in markets:
        try:
            if not await recover_market(market_id):
                pending.append(f"{market_id.split(':')[1]} payout correction is pending.")
        except (PyMongoError, FinalError) as error:
            pending.append(f"{market_id.split(':')[1]} payout correction is pending: {error}")
    if bot is not None:
        for market in await FinalBettingMarket.find({"final_id": str(final.id), "announcement_attempted": True}).to_list():
            if market.announcement_signature != market_signature(market) or market.publication_error:
                try:
                    await publish_market(bot, final, market)
                except FinalError as error:
                    pending.append(str(error))
    collection = EloFinal.get_pymongo_collection()
    results = await FinalMatchResult.find({"_id": {"$in": [node.result_id for node in final.nodes.values() if node.result_id]}}).to_list()
    for result in results:
        key = result.id.replace(".", "_")
        progress = final.result_progress[key]
        path = f"result_progress.{key}"
        completed_now = not progress.helper_completed
        if not progress.helper_completed:
            try:
                for credit in result.helper_credits:
                    await award_helper_once(credit.user_id, credit.receipt, credit.points, credit.history)
            except (PyMongoError, LookupError) as error:
                pending.append(f"{result.node_id} result saved; Helper bookkeeping is pending: {error}")
                continue
            await collection.update_one({"_id": final.id}, {"$set": {f"{path}.helper_completed": True,
                                                                     f"{path}.completed": True}})
        if bot is not None and result.snapshot.outcome == "Played":
            await invite_eligible_players(bot, result.snapshot.contestants, pending_only=not completed_now)
        source = await Match.get(PydanticObjectId(result.source_match_id)) if result.source_match_id else None
        if source and source.final_result_id == result.id:
            await Match.get_pymongo_collection().update_one({"_id": source.id, "final_result_id": result.id},
                {"$set": {"final_result_operation.helper_completed": True, "final_result_operation.completed": True}})
            source = await Match.get(source.id)
            reserved = any(participant.base_store or participant.opponent_bases_stored for participant in source.participants)
            if result.snapshot.outcome == "Played" and reserved and not progress.release_completed:
                if release_completed is None:
                    pending.append(f"{result.node_id} completed base storage cleanup is pending.")
                else:
                    try:
                        await release_completed(source)
                    except (PyMongoError, ipy.errors.HTTPException, FinalError,
                            BaseStoreReleaseConflict, BaseStoreReleaseDenied) as error:
                        pending.append(f"{result.node_id} result saved; base storage cleanup is pending: {error}")
                    else:
                        await collection.update_one({"_id": final.id}, {"$set": {f"{path}.release_completed": True}})
        if bot is not None:
            try:
                await publish_result(bot, final, result)
            except FinalError as error:
                pending.append(str(error))
            refreshed = await resolve_final(final.season_number)
            if source and refreshed.result_progress[key].thread_deleted:
                if release_deleted is not None:
                    await release_deleted(source)
                elif any(participant.base_store or participant.opponent_bases_stored for participant in source.participants):
                    pending.append(f"{result.node_id} deleted-thread base cleanup is pending.")
    if not pending or all("publication" in item for item in pending):
        await collection.update_one({"_id": final.id, "result_revision": final.result_revision},
            {"$set": {"repair_pending": False, "repair_nodes": []}})
    final = await resolve_final(final.season_number)
    if bot is not None and not final.repair_pending:
        for result in results:
            progress = final.result_progress[result.id.replace(".", "_")]
            if not progress.published or progress.error:
                continue
            try:
                await publish_result(bot, final, result)
            except FinalError as error:
                if str(error) not in pending:
                    pending.append(str(error))
    if final.state == "CANCELLING" and not final.repair_pending:
        unfinished = await FinalBettingMarket.find_one({"final_id": str(final.id), "$or": [
            {"escrow": {"$ne": 0}}, {"state": {"$nin": ["PAID", "REFUNDED"]}}]})
        if unfinished is None:
            await collection.update_one({"_id": final.id, "state": "CANCELLING", "repair_pending": False},
                {"$set": {"state": "CANCELLED", "active": False}, "$inc": {"revision": 1}})
    if final.reopening_pending:
        from pymongo.errors import DuplicateKeyError
        try:
            other = await EloFinal.find_one({"active": True, "_id": {"$ne": final.id}})
            if other is None:
                await collection.update_one({"_id": final.id, "reopening_pending": True}, {"$set": {
                    "active": True, "state": "RUNNING", "reopening_pending": False}, "$inc": {"revision": 1}})
            else:
                pending.append("Finish the ongoing Final before reopening this edition for corrected play.")
        except DuplicateKeyError:
            pending.append("Another Final owns the ongoing edition. Reopening remains pending.")
    for node in final.nodes.values():
        if bot is not None and node.match_id and node.thread_id and not node.result_id:
            try:
                await bot.fetch_channel(int(node.thread_id))
            except ipy.errors.NotFound:
                source = await Match.get(node.match_id)
                if source and release_deleted is not None:
                    await release_deleted(source)
                elif source and any(participant.base_store or participant.opponent_bases_stored for participant in source.participants):
                    pending.append(f"{node.id} deleted-thread base cleanup is pending.")
                    continue
                await mark_deleted_attempt(final, node, source)
                pending.append(f"{node.id}: thread deleted; use `/final round start` or `/final match result`.")
            except ipy.errors.HTTPException:
                pending.append(f"{node.id}: thread access could not be verified. Its reservations remain owned.")
        if node.provisioning and node.match_id and not node.thread_id:
            source = await Match.get(node.match_id)
            if source and source.thread and source.final_contestant_revision == node.contestant_revision and source.final_attempt == node.attempt:
                await bind_thread(final, source, source.thread)
                continue
        if node.state in {"TIED", "NEEDS_MANAGER"}:
            pending.append(f"{node.id}: manager-arranged play or a result decision is required.")
        if node.provisioning and not node.thread_id:
            pending.append(f"{node.id}: verify whether its thread was created before retrying.")
    if bot is not None and final.bracket_send_attempted and final.publication_pending:
        try:
            await publish_bracket(bot, final)
        except FinalError as error:
            pending.append(str(error))
    return pending
