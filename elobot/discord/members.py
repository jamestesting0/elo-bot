import interactions

from elobot.config.ids import MAIN_GUILD


async def member_guild(ctx: interactions.BaseContext) -> interactions.Guild:
    """Server to look up members in; DMs and other server-less interactions use the main server."""
    return ctx.guild or await ctx.bot.fetch_guild(MAIN_GUILD)


async def fetch_member_otherwise_user(guild: interactions.Guild, bot: interactions.Client, user_id: str):
    member = None
    if guild:
        try:
            member = await guild.fetch_member(user_id)
        except interactions.errors.DiscordError as error:
            if error.status != 503:
                raise
    if not member:
        member = await bot.fetch_user(user_id)

    return member
