import interactions as ipy
from interactions import OptionType, SlashContext, slash_option

from elo_bot.config.emojis import IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.config.ids import FINAL_CHANNEL, LVL1_ROLES
from elo_bot.discord.members import member_guild
from elo_bot.features.finals.labels import final_member_names
from elo_bot.extensions.final.checks import final_action
from elo_bot.extensions.final.groups import final
from elo_bot.features.finals import lifecycle
from elo_bot.features.finals.matches import refresh_match_names
from elo_bot.features.finals.registration_posts import sync_registration_posts


class Setup:
    final = final

    @final.subcommand(sub_cmd_name="start", sub_cmd_description="Open the Elo Final for the ongoing season")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_start(self, ctx: SlashContext):
        await self._access(ctx, access="manager")
        edition = await lifecycle.start_final(ctx.guild_id, FINAL_CHANNEL)
        await ctx.send(f"{SUCCESS_EMOJI} Season {edition.season_number} Final created. "
                       "Set it up with `/final dashboard`.", ephemeral=True)

    @final.subcommand(
        sub_cmd_name="add",
        sub_cmd_description="Add a player to the Elo Final",
    )
    @slash_option(name="player", description="The player to add", opt_type=OptionType.USER, required=True)
    @slash_option(name="nickname", description="Display nickname", opt_type=OptionType.STRING)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_player_add(self, ctx: SlashContext, player: ipy.User, nickname: str = None):
        await ctx.defer(ephemeral=True)
        edition = await self._ongoing_edition(ctx, access="manager")
        edition = await lifecycle.add_entrant(edition, str(player.id), nickname or player.display_name,
                                             override=bool(nickname and nickname.strip()))
        names = await final_member_names(edition, await member_guild(ctx), [str(player.id)])
        await ctx.send(f"{SUCCESS_EMOJI} Added {names[str(player.id)]} ({len(edition.entrant_ids)} of 8).",
                       ephemeral=True)
        await sync_registration_posts(self.bot, edition)

    @final.subcommand(
        sub_cmd_name="remove",
        sub_cmd_description="Remove a player from the Elo Final",
    )
    @slash_option(name="player", description="The player to remove", opt_type=OptionType.USER, required=True)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_player_remove(self, ctx: SlashContext, player: ipy.User):
        await ctx.defer(ephemeral=True)
        edition = await self._ongoing_edition(ctx, access="manager")
        await lifecycle.remove_entrant(edition, str(player.id))
        names = await final_member_names(edition, await member_guild(ctx), [str(player.id)])
        await ctx.send(f"{SUCCESS_EMOJI} Removed {names[str(player.id)]} ({len(edition.entrant_ids) - 1} of 8).",
                       ephemeral=True)
        await sync_registration_posts(self.bot, edition)

    @final.subcommand(
        sub_cmd_name="nick",
        sub_cmd_description="Set nickname for a player in the Elo Final",
    )
    @slash_option(name="player", description="The player", opt_type=OptionType.USER, required=True)
    @slash_option(name="nickname", description="New nickname", opt_type=OptionType.STRING, required=True)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_player_nick(self, ctx: SlashContext, player: ipy.User, nickname: str):
        await ctx.defer(ephemeral=True)
        elo_final = await self._ongoing_edition(ctx, access="manager")
        user_id = str(player.id)
        old_nick = elo_final.player_nicknames.get(user_id)
        await lifecycle.set_nickname(elo_final, user_id, nickname)
        current = await lifecycle.reload_final(elo_final)
        failed = await refresh_match_names(ctx, current, user_id)
        saved_nick = current.player_nicknames.get(user_id, nickname.strip()[:80])
        content = f"{SUCCESS_EMOJI} Nickname updated: **{old_nick}** → **{saved_nick}**."
        if failed:
            content += f"\n{IMPORTANT_EMOJI} Could not refresh {', '.join(failed)}. Run `/final nick` again to retry."
        await ctx.send(content, ephemeral=True)
        await sync_registration_posts(self.bot, current)
