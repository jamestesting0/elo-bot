import logging
import time

import interactions as ipy
from interactions import OptionType, SlashContext, slash_option
from pymongo.errors import PyMongoError

from elo_bot.config.emojis import IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.config.ids import LVL1_ROLES
from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal
from elo_bot.extensions.final.checks import final_action
from elo_bot.extensions.final.groups import final
from elo_bot.features.base_store.match_adapters import clear_match_base_stores
from elo_bot.features.finals.header_refresh import refresh_final_headers
from elo_bot.features.finals.lifecycle import reload_final
from elo_bot.features.finals.recovery import recover_final
from elo_bot.features.finals.registration_posts import registration_posts_pending
from elo_bot.features.finals.scouting import prepare_open_scouting
from elo_bot.features.matches.resources import free_match_clans, remove_helper_task
from elo_bot.features.tokens.wallets import backfill_wallets


class Status:
    final = final

    async def _recover(self, edition, *, audit_betting_posts=False):
        async def release(source):
            await clear_match_base_stores(source, reset_stored_flags=True, bot=self.bot, reason="finished")
            await free_match_clans(source)
        async def deleted(source):
            await clear_match_base_stores(source, reset_stored_flags=True, bot=self.bot, reason="thread_deleted")
            await free_match_clans(source)
            for participant in source.participants:
                await remove_helper_task(source.thread, participant.user_id)
        return await recover_final(edition, bot=self.bot, release_completed=release, release_deleted=deleted,
                                   audit_betting_posts=audit_betting_posts)

    async def _delete_expired_threads(self):
        due = await EloFinal.find({"thread_cleanup_at": {"$lte": int(time.time())}}).to_list()
        for edition in due:
            remaining = 0
            for thread in await self._open_final_threads(edition):
                try:
                    await thread.delete(reason=f"Season {edition.season_number} Final threads expired")
                except ipy.errors.NotFound:
                    continue
                except ipy.errors.HTTPException:
                    logging.getLogger(__name__).exception("Could not delete Final thread %s", thread.id)
                    remaining += 1
            if not remaining:
                await EloFinal.get_pymongo_collection().update_one({"_id": edition.id},
                                                                   {"$set": {"thread_cleanup_at": None}})

    @final.subcommand(sub_cmd_name="recover", sub_cmd_description="Resume unfinished Final updates")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_recover(self, ctx: SlashContext):
        await ctx.defer(ephemeral=True)
        edition = await self._ongoing_edition(ctx, access="manager")
        pending = await self._recover(edition, audit_betting_posts=True)
        pending.extend(await refresh_final_headers(ctx, self.bot, await reload_final(edition)))
        await ctx.send(f"{IMPORTANT_EMOJI} " + "\n".join(pending) if pending else
                       f"{SUCCESS_EMOJI} Final updates are complete.", ephemeral=True)

    @final.subcommand(sub_cmd_name="end", sub_cmd_description="Archive the completed Final")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_end(self, ctx: SlashContext):
        await ctx.defer(ephemeral=True)
        edition = await self._ongoing_edition(ctx, access="manager")
        await self._confirm_final_action(ctx, edition, "end")

    @final.subcommand(sub_cmd_name="cancel", sub_cmd_description="Cancel new work and refund undecided markets")
    @slash_option(name="reason", description="Cancellation reason", opt_type=OptionType.STRING, required=True, max_length=300)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_cancel(self, ctx: SlashContext, reason: str):
        await ctx.defer(ephemeral=True)
        edition = await self._ongoing_edition(ctx, access="manager")
        await self._confirm_final_action(ctx, edition, "cancel", reason=reason)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=2))
    async def recover_pending_finals(self):
        for edition in await EloFinal.find({"schema_version": 2}).to_list():
            if (edition.active or edition.repair_pending or edition.publication_pending
                    or any(post.error for post in edition.betting_posts.values())
                    or registration_posts_pending(edition)
                    or any(progress.error for progress in edition.result_progress.values())):
                try:
                    pending = await self._recover(edition)
                    if pending:
                        logging.getLogger(__name__).info("Final %s recovery: %s", edition.id, pending)
                except (FinalError, PyMongoError, ipy.errors.HTTPException, RuntimeError, LookupError):
                    logging.getLogger(__name__).exception("Final %s recovery failed", edition.id)
        await self._retire_invitations()
        await self._delete_expired_threads()

    @ipy.Task.create(ipy.IntervalTrigger(minutes=1))
    async def prepare_final_scouting(self):
        await prepare_open_scouting(self.bot)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        try:
            await backfill_wallets()
        except FinalError:
            logging.getLogger(__name__).exception("Token backfill is unavailable; registration and views may retry it")
        await self.recover_pending_finals()
        self.recover_pending_finals.start()
        self.prepare_final_scouting.start()

    @final.subcommand(sub_cmd_name="dashboard", sub_cmd_description="Open the Final dashboard")
    @final_action
    async def final_dashboard(self, ctx: SlashContext):
        await ctx.defer(ephemeral=True)
        edition = await self._edition(ctx)
        await self._show_dashboard(ctx, edition)
