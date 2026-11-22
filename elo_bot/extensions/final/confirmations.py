import logging
import re

import interactions as ipy
from beanie import PydanticObjectId
from pymongo.errors import PyMongoError

from elo_bot.config.emojis import CROSS_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI, TICK_EMOJI
from elo_bot.config.ids import LVL1_ROLES
from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.discord.embeds import embed_color, footer_image
from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal, Match
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.discord.members import member_guild
from elo_bot.discord.mentions import member_names
from elo_bot.extensions.final.checks import final_action
from elo_bot.features.finals import lifecycle
from elo_bot.features.finals.dashboard import FINAL_CHANGED, available_actions
from elo_bot.features.tokens.awards import recover_awards


class Confirmations:
    async def _open_final_threads(self, edition):
        sources = await Match.find({"final_id": str(edition.id), "thread": {"$ne": None}}).to_list()
        threads = {}
        for source in sources:
            if source.thread in threads:
                continue
            try:
                threads[source.thread] = await self.bot.fetch_channel(int(source.thread))
            except ipy.errors.NotFound:
                continue
        return list(threads.values())

    async def _archive_threads(self, threads, cleanup_at: int) -> str:
        """Archive and lock the threads; returns the note for the end or cancel reply."""
        failed = 0
        for thread in threads:
            try:
                await thread.edit(archived=True, locked=True)
            except ipy.errors.HTTPException:
                logging.exception("Could not archive Final thread %s", thread.id)
                failed += 1
        if not threads:
            return ""
        note = f" Match threads are archived and will be deleted <t:{cleanup_at}:R>."
        if failed:
            note += f" {IMPORTANT_EMOJI} {failed} could not be archived."
        return note

    async def _confirm_final_action(self, ctx, edition, action, *, reason="", edit=False):
        owner = str(ctx.author.id)
        prefix = f"finalmanage|{action}|{edition.id}|{owner}|{edition.result_revision}"
        if action == "end":
            if "end" not in dict(available_actions(await self._dashboard_state(edition))):
                raise FinalError("Resolve every pairing and finish pending updates before ending the Final.")
            names = await member_names(await member_guild(ctx), [edition.champion])
            embed = ipy.Embed(title=f"**End the Season {edition.season_number} Final?**", color=embed_color(),
                description=f"Champion: {names[edition.champion]}. Results can't be changed afterwards.")
            button = ipy.Button(label="End Final", style=ipy.ButtonStyle.SUCCESS, emoji=TICK_EMOJI, custom_id=prefix)
        else:
            threads = await self._open_final_threads(edition)
            description = "Bets on undecided matches are refunded. Results stay in history."
            if threads:
                description += (f"\n{len(threads)} match {'thread is' if len(threads) == 1 else 'threads are'} still open. "
                                "Stored bases stay reserved until the threads are deleted.")
            embed = ipy.Embed(title=f"**Cancel the Season {edition.season_number} Final?**",
                              description=description, color=embed_color())
            embed.add_field(name="**Reason**", value=reason)
            button = ipy.Button(label="Cancel Final", style=ipy.ButtonStyle.DANGER, emoji=CROSS_EMOJI, custom_id=prefix)
        embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
        controls = [button, ipy.Button(label="Back", style=ipy.ButtonStyle.SECONDARY,
            custom_id=f"finalmanage|back|{edition.id}|{owner}|{edition.result_revision}")]
        if edit:
            await ctx.edit_origin(content="", embed=embed, components=controls)
        else:
            await ctx.send(embed=embed, components=controls, ephemeral=True)

    @ipy.component_callback(re.compile(r"^finalmanage\|(cancel|end|back)\|[a-f0-9]{24}\|\d+\|\d+$"))
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_management_confirmation(self, ctx: ipy.ComponentContext):
        _, action, final_id, owner, revision = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            raise FinalError("You cannot interact with another user's components.")
        await ctx.defer(edit_origin=True)
        edition = await self._ongoing_edition(ctx, final_id, access="manager")
        if action == "back":
            await self._show_dashboard(ctx, edition, edit=True)
            return
        if int(revision) != edition.result_revision:
            # A result changed after this prompt was drawn, so the manager confirms against the current Final.
            await self._show_dashboard(ctx, edition, edit=True, outcome=FINAL_CHANGED)
            return
        if action == "end":
            await self._recover(edition)
            await lifecycle.end_final(edition)
            current = await lifecycle.reload_final(edition)
            names = await member_names(await member_guild(ctx), [current.champion])
            content = f"{SUCCESS_EMOJI} Season {current.season_number} Final ended. Champion: {names[current.champion]}."
            if not await recover_awards(final_id):
                content += f" {IMPORTANT_EMOJI} Prediction rewards are pending and retry automatically."
        else:
            reason = next(field.value for field in ctx.message.embeds[0].fields if field.name == "**Reason**")
            await lifecycle.request_cancellation(edition, reason)
            content = ""
        try:
            pending = await self._recover(await lifecycle.reload_final(edition))
        except (FinalError, PyMongoError) as error:
            pending = [str(error)]
        current = await lifecycle.reload_final(edition)
        threads = await self._open_final_threads(current)
        archive_note = await self._archive_threads(threads, current.thread_cleanup_at)
        controls = []
        if action == "cancel":
            count = await FinalBettingMarket.find({"final_id": final_id, "state": "REFUNDED"}).count()
            content = (f"{IMPORTANT_EMOJI} Cancellation saved. Refunds are still being paid and retry automatically."
                       if current.state != "CANCELLED" else
                       f"{SUCCESS_EMOJI} Final cancelled. {count} {'market' if count == 1 else 'markets'} refunded.")
            if threads:
                controls = [ipy.Button(label=f"Delete {len(threads)} Match {'Thread' if len(threads) == 1 else 'Threads'}", emoji=CROSS_EMOJI,
                    style=ipy.ButtonStyle.DANGER, custom_id=f"finalthreads|{final_id}|{owner}")]
        content += archive_note
        if pending and (action == "end" or current.state == "CANCELLED"):
            content += f" {IMPORTANT_EMOJI} Public updates are pending and retry automatically."
        await ctx.edit_origin(content=content, embeds=[], components=controls)

    @ipy.component_callback(re.compile(r"^finalthreads\|[a-f0-9]{24}\|\d+$"))
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_delete_threads(self, ctx: ipy.ComponentContext):
        _, final_id, owner = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            raise FinalError("You cannot interact with another user's components.")
        await ctx.defer(edit_origin=True)
        edition = await EloFinal.get(PydanticObjectId(final_id))
        if edition is None or edition.state not in {"CANCELLING", "CANCELLED"}:
            raise FinalError("This view is outdated. Run `/final dashboard` again.")
        await self._access(ctx, edition, access="manager")
        deleted = 0
        failed = []
        for thread in await self._open_final_threads(edition):
            try:
                await thread.delete(reason=f"Cancelled Final: {ctx.author.id}")
                deleted += 1
            except ipy.errors.NotFound:
                continue
            except ipy.errors.HTTPException as error:
                failed.append(str(error))
        content = f"{SUCCESS_EMOJI} Deleted {deleted} match {'thread' if deleted == 1 else 'threads'}."
        controls = []
        if failed:
            content += f" {IMPORTANT_EMOJI} {len(failed)} threads could not be deleted. Their stored bases remain reserved."
            controls = [ipy.Button(label=f"Delete {len(failed)} Match Threads", style=ipy.ButtonStyle.DANGER,
                emoji=CROSS_EMOJI, custom_id=ctx.custom_id)]
        await ctx.edit_origin(content=content, embeds=[], components=controls)
