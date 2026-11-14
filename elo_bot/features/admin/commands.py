import interactions as ipy

from elo_bot.config.emojis import SUCCESS_EMOJI
from elo_bot.config.ids import ELO_BLACKLIST_ROLE, GUILD_IDS, LVL1_ROLES
from elo_bot.db import OverrideCode


@ipy.slash_command(name="ban", description="Ban a member from using Elo commands.", scopes=GUILD_IDS,
                   dm_permission=False)
@ipy.check(ipy.has_any_role(*LVL1_ROLES))
@ipy.slash_option(name="user", description="The member to ban", opt_type=ipy.OptionType.USER, required=True)
async def ban(ctx: ipy.SlashContext, user: ipy.Member) -> None:
    await ctx.defer(ephemeral=True)
    await user.add_role(ELO_BLACKLIST_ROLE)
    await ctx.send(f"{SUCCESS_EMOJI} {user.mention} is now banned from using Elo commands.", ephemeral=True)


@ipy.slash_command(name="unban", description="Allow a banned member to use Elo commands again.", scopes=GUILD_IDS,
                   dm_permission=False)
@ipy.check(ipy.has_any_role(*LVL1_ROLES))
@ipy.slash_option(name="user", description="The member to unban", opt_type=ipy.OptionType.USER, required=True)
async def unban(ctx: ipy.SlashContext, user: ipy.Member) -> None:
    await ctx.defer(ephemeral=True)
    await user.remove_role(ELO_BLACKLIST_ROLE)
    await ctx.send(f"{SUCCESS_EMOJI} {user.mention} can now use Elo commands again.", ephemeral=True)


@ipy.slash_command(name="override", description="To override a restricted actions.", scopes=GUILD_IDS)
@ipy.check(ipy.has_any_role(*LVL1_ROLES))
@ipy.slash_option(
    name="override_type",
    description="The type of action to override",
    opt_type=ipy.OptionType.STRING,
    choices=[
        ipy.SlashCommandChoice(name="Challenge", value="challenge"),
        ipy.SlashCommandChoice(name="Match", value="match")
    ],
    required=True
)
async def override(ctx: ipy.SlashContext, override_type: str):
    await ctx.defer(ephemeral=True)

    override_code = await OverrideCode.issue(override_type)

    await ctx.send(f"{SUCCESS_EMOJI} The override code for the {override_type} is `{override_code}`.",
                   ephemeral=True)
