import logging

import anyio

import interactions as ipy
from elobot.config.emojis import CROSS_EMOJI, ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI, TICK_EMOJI
from elobot.config.ids import FINAL_CHANNEL, GUILD_IDS, LVL1_ROLES, MATCH_CHANNEL
from elobot.db import Match
import asyncio
from elobot.features.matches.cleanup import delete_unreserved_match, has_pending_match_operation, has_reserved_match_slots
from elobot.features.base_store.preparation import BASE_STORE_RESERVATION_LOCK
from elobot.features.base_store.storage import clear_match_base_stores
from elobot.features.hosting.war import result_store
from elobot.features.matches.resources import free_match_clans, remove_helper_task


class ThreadCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot

    base = ipy.SlashCommand(name="thread", description="", scopes=GUILD_IDS)

    @base.subcommand(sub_cmd_name="delete", sub_cmd_description="Delete a thread")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    async def thread_delete(self, ctx: ipy.SlashContext):
        await ctx.defer(ephemeral=True)

        if not ctx.channel.parent_channel:
            await ctx.send(f"{ERROR_EMOJI} This is not a thread!", ephemeral=True)
            return

        if "🔴" not in ctx.channel.name and int(ctx.channel.parent_channel.id) in {MATCH_CHANNEL, FINAL_CHANNEL}:
            await ctx.send(f"{ERROR_EMOJI} The match is not yet finished!", ephemeral=True)
            return

        cancel_button = ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Cancel",
            custom_id="cancel",
            emoji=CROSS_EMOJI
        )
        confirm_button = ipy.Button(
            style=ipy.ButtonStyle.SUCCESS,
            label="Confirm",
            custom_id="confirm",
            emoji=TICK_EMOJI
        )
        button_actionrow = ipy.ActionRow(cancel_button, confirm_button)

        msg = await ctx.send(
            "Are you sure that you would like to **delete** the thread? If the match is not "
            "finished, deleting it will invalidate the match.\n\n"
            "*Note: you cannot undo this action.*",
            components=button_actionrow,
            ephemeral=True
        )

        try:
            res: ipy.ComponentContext = (
                await self.bot.wait_for_component(
                    components=button_actionrow,
                    messages=int(msg.id),
                    timeout=180
                )
            ).ctx
        except asyncio.TimeoutError:
            await ctx.delete(msg)
            return

        if res.custom_id == "confirm":
            match = await Match.find_one(Match.thread == str(res.channel.id))
            lock_key = match.token if match else f"thread:{res.channel.id}"
            lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault(lock_key, anyio.Lock())
            async with lock:
                match = await Match.find_one(Match.thread == str(res.channel.id))
                if match and has_pending_match_operation(match):
                    await ctx.send(
                        f"{ERROR_EMOJI} A result or auto-win submission is unfinished. "
                        "Complete it before deleting this thread.",
                        ephemeral=True,
                    )
                    return
                if match and has_reserved_match_slots(match):
                    await ctx.send(
                        f"{ERROR_EMOJI} This match still has reserved bases. "
                        "Finish the match or use Undo before deleting this thread.", ephemeral=True,
                    )
                    return

                await res.edit_origin(components=ipy.utils.misc_utils.disable_components(button_actionrow))
                await ctx.send(f"{LOADING_EMOJI} The thread will be deleted in 5 seconds.", ephemeral=True)
                await asyncio.sleep(5)

                async with BASE_STORE_RESERVATION_LOCK:
                    match = await Match.find_one(Match.thread == str(res.channel.id))
                    if match and (has_pending_match_operation(match) or has_reserved_match_slots(match)):
                        await ctx.send(
                            f"{ERROR_EMOJI} This match now has reserved bases or an unfinished submission. "
                            "Finish the match, or use Undo for its reservation, before deleting this thread.",
                            ephemeral=True,
                        )
                        return
                    await ctx.channel.delete(reason=f"Deleted by: {ctx.author} ({ctx.author.id})")
                    if match and not await delete_unreserved_match(match):
                        logging.warning("Match %s changed before deleted-thread cleanup could finish", match.token)
                        return
                if match:
                    await free_match_clans(match)
                    for participant in match.participants:
                        await remove_helper_task(match.thread, participant.user_id)

        if res.custom_id == "cancel":
            await res.edit_origin(components=ipy.utils.misc_utils.disable_components(button_actionrow))
            await ctx.send(f"{SUCCESS_EMOJI} The action has been canceled.", ephemeral=True)

    @ipy.listen(ipy.events.ThreadDelete)
    async def on_thread_delete(self, event: ipy.events.ThreadDelete):
        # interactions.py dispatches a Thread object when cached, otherwise the raw snowflake ID.
        thread_id = getattr(event.thread, "id", event.thread)
        if not thread_id:
            return

        async with BASE_STORE_RESERVATION_LOCK:
            match = await Match.find_one(Match.thread == str(thread_id))
            if not match:
                return
            await clear_match_base_stores(match, reset_stored_flags=True, bot=self.bot, reason="thread_deleted")
            await self._delete_match_record(match)

    async def _delete_match_record(self, match: Match) -> None:
        if not await delete_unreserved_match(match):
            logging.warning("Match %s changed before deleted-thread cleanup could finish", match.token)
            return
        await free_match_clans(match)
        for participant in match.participants:
            await remove_helper_task(match.thread, participant.user_id)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Thread commands loaded")
