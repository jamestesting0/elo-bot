import asyncio

import interactions as ipy
from common.constants import COLOR, FOOTER_IMAGE, FOOTER_TEXT, GUILD_IDS, LVL1_ROLES, SUCCESS_EMOJI
from common.utils import mention_user, member_guild
from database_models import Queue, ServerState


class QueueCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot

    base = ipy.SlashCommand(name="queue", description="", scopes=GUILD_IDS)

    @base.subcommand(sub_cmd_name="view", sub_cmd_description="View the current queue")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def queue_view(self, ctx: ipy.SlashContext, hidden: bool = True):
        await ctx.defer(ephemeral=hidden)

        embed = ipy.Embed(
            title="**Queue**",
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        for i in range(0, 4):
            description = ""
            queue = await Queue.find_one(Queue.tier == i, fetch_links=True)
            if queue and queue.players:
                guild = await member_guild(ctx)
                members = await asyncio.gather(*(guild.fetch_member(player.user_id) for player in queue.players))
                for player, user in zip(queue.players, members):
                    description += f"{mention_user(user, player.user_id)} `{player.elo}`\n"
            
            embed.add_field(name=f"Tier {i}", value=description if description else "This queue is empty...")

        await ctx.send(embed=embed, ephemeral=hidden)

    @base.subcommand(sub_cmd_name="enable", sub_cmd_description="Enable match-making")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    async def queue_enable(self, ctx: ipy.SlashContext):
        await ServerState.update_fields(queue_disabled=False)

        await ctx.send(f"{SUCCESS_EMOJI} Match-making is now **enabled**!", ephemeral=True)

    @base.subcommand(sub_cmd_name="disable", sub_cmd_description="Disable match-making")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    async def queue_disable(self, ctx: ipy.SlashContext):
        await ServerState.update_fields(queue_disabled=True)

        await ctx.send(f"{SUCCESS_EMOJI} Match-making is now **disabled**!", ephemeral=True)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Queue commands loaded")
