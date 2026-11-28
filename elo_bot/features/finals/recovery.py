from collections.abc import Awaitable, Callable

import interactions as ipy
from beanie import PydanticObjectId
from pymongo.errors import PyMongoError

from elo_bot.features.finals.persistence import transaction
from elo_bot.features.finals.predictions import rescore_predictions
from elo_bot.features.finals.profiles import rebuild_profiles
from elo_bot.features.finals.publication import publish_bracket, bracket_signature
from elo_bot.core.finals import FinalError
from elo_bot.features.results.helper_credits import award_helper_once
from elo_bot.features.hosting.self_hosted.invitations import invite_eligible_players
from elo_bot.features.tokens.settlement import recover_market, set_settlement_intent
from elo_bot.features.tokens.presentation import market_signature, publish_market
from elo_bot.db import EloFinal, Match
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.features.base_store.storage import BaseStoreReleaseConflict, BaseStoreReleaseDenied
from elo_bot.features.finals.matches import bind_thread
from elo_bot.features.finals.lifecycle import reload_final
from elo_bot.features.finals.result_publication import publish_result
from elo_bot.features.finals.registration_posts import sync_registration_posts
from elo_bot.features.tokens.stakes import reconcile_stake


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
                winner = None if refund else node.winner
                if (market.desired_result_version != final.result_revision
                        or market.desired_winner != winner or market.desired_refund != refund):
                    await set_settlement_intent(market.id, final.result_revision, winner=winner,
                                                refund=refund, session=session)
                    selected.append(market.id)
                elif market.state in {"SETTLING", "CORRECTING"}:
                    selected.append(market.id)
            elif node.result_id and not node.winner and market.state == "OPEN":
                await FinalBettingMarket.get_pymongo_collection().update_one({"_id": market.id, "state": "OPEN"},
                    {"$set": {"state": "CLOSED"}, "$inc": {"version": 1}}, session=session)
        if final.predictions_scoring_revision != final.result_revision:
            await rescore_predictions(final, session)
            await rebuild_profiles(final, session)
            await collection.update_one({"_id": final.id}, {"$set": {
                "predictions_scoring_revision": final.result_revision}, "$inc": {"revision": 1}}, session=session)
            final.predictions_scoring_revision = final.result_revision
            final.revision += 1
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
            if source.match_stake_enabled:
                await reconcile_stake(str(source.id), session=session)
    await transaction(mark)


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
        for market in await FinalBettingMarket.find({"final_id": str(final.id)}).to_list():
            if market.announcement_signature != market_signature(market, final) or market.publication_error:
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
            refreshed = await reload_final(final)
            if source and refreshed.result_progress[key].thread_deleted:
                if release_deleted is not None:
                    await release_deleted(source)
                elif any(participant.base_store or participant.opponent_bases_stored for participant in source.participants):
                    pending.append(f"{result.node_id} deleted-thread base cleanup is pending.")
    if final.repair_pending and (not pending or all("publication" in item for item in pending)):
        await collection.update_one({"_id": final.id, "result_revision": final.result_revision},
            {"$set": {"repair_pending": False, "repair_nodes": []}})
    final = await reload_final(final)
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
        if final.state not in {"CANCELLING", "CANCELLED"} and node.provisioning and node.match_id and not node.thread_id:
            source = await Match.get(node.match_id)
            if source and source.thread and source.final_contestant_revision == node.contestant_revision and source.final_attempt == node.attempt:
                await bind_thread(final, source, source.thread)
                continue
        if final.state not in {"CANCELLING", "CANCELLED"} and node.state == "NEEDS_MANAGER":
            pending.append(f"{node.id}: manager-arranged play or a result decision is required.")
        if final.state not in {"CANCELLING", "CANCELLED"} and node.provisioning and not node.thread_id:
            pending.append(f"{node.id}: verify whether its thread was created before retrying.")
    bracket_pending = final.publication_pending or bool(final.publication_error)
    if bot is not None and (bracket_pending or
            (final.active and final.bracket_message_id and final.bracket_signature != await bracket_signature(final))):
        try:
            await publish_bracket(bot, final, allow_create=bracket_pending and bool(final.nodes)
                                  and not final.bracket_message_id)
        except FinalError as error:
            pending.append(str(error))
    if bot is not None:
        pending.extend(await sync_registration_posts(bot, final))
    return pending
