import coc

import interactions as ipy
from elobot.config.emojis import CLAN_EMOJI, ERROR_EMOJI, SUCCESS_EMOJI
from elobot.config.presentation import COLOR, FOOTER_IMAGE
from elobot.config.ids import GUILD_IDS, LVL1_ROLES, RAID_CHANNEL
from elobot.core.raid_weekend import is_raid_weekend
from elobot.discord.mentions import mention_user


class RaidCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot

    @property
    def coc_client(self) -> coc.Client:
        return self.bot.coc_client

    @ipy.slash_command(name="open-raid-post", description="Create a post to advertise your clan")
    @ipy.slash_option(
        name="clan_tag",
        description="The tag of the clan you want to advertise",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="available_slots",
        description="Number of free slots the clan has",
        opt_type=ipy.OptionType.INTEGER,
        min_value=1,
        max_value=50,
        required=True
    )
    @ipy.slash_option(
        name="join_method",
        description="How should the players join the clan",
        choices=[
            ipy.SlashCommandChoice(name="Discord", value="Discord"),
            ipy.SlashCommandChoice(name="In-game Request", value="In-game Request")
        ],
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="skill_requirement",
        description="The skills needed to join",
        choices=[
            ipy.SlashCommandChoice(name="Professional", value="Professional"),
            ipy.SlashCommandChoice(name="Experienced", value="Experienced"),
            ipy.SlashCommandChoice(name="Any", value="Any"),
        ],
        opt_type=ipy.OptionType.STRING
    )
    @ipy.slash_option(
        name="donation_requirement",
        description="The percentage of capital that needs to be deposited",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="100%", value="100%"),
            ipy.SlashCommandChoice(name="75%", value="75%"),
            ipy.SlashCommandChoice(name="50%", value="50%"),
            ipy.SlashCommandChoice(name="25%", value="25%"),
            ipy.SlashCommandChoice(name="0%", value="0%"),
        ]
    )
    @ipy.slash_option(
        name="extra_info",
        description="Anything you want to add (500 characters max)",
        opt_type=ipy.OptionType.STRING,
        max_length=500
    )
    async def open_raid_post(self, ctx: ipy.SlashContext, clan_tag: str, available_slots: int,
                             join_method: str, skill_requirement: str = "Any", donation_requirement: str = "0%",
                             extra_info: str = None):
        if not is_raid_weekend():
            await ctx.send(f"{ERROR_EMOJI} This command can only be run during raid weekends.",
                           ephemeral=True)

            return

        if int(ctx.channel.id) != RAID_CHANNEL:
            await ctx.send(f"{ERROR_EMOJI} This command can only be run in <#{RAID_CHANNEL}>.",
                           ephemeral=True)

            return

        try:
            clan: coc.Clan = await self.coc_client.get_clan(clan_tag)
        except coc.errors.NotFound:
            await ctx.respond(f"{ERROR_EMOJI} `{clan_tag}` is an invalid clan tag.", ephemeral=True)

            return

        if not len(clan.capital_districts):
            await ctx.respond(f"{ERROR_EMOJI} You clan has not unlocked clan capital yet.", ephemeral=True)

            return

        if join_method == "Discord":
            join_method = f"DM {mention_user(ctx.author)}"

        embed = ipy.Embed(title=f"**{clan.name} ({clan.tag})**", color=COLOR)
        embed.add_field(name="**CH Level**", value=str(clan.capital_districts[0].hall_level), inline=True)
        embed.add_field(name="**Capital Trophies**", value=str(clan.capital_points), inline=True)
        embed.add_field(name="**Available Slots**", value=available_slots, inline=True)
        embed.add_field(name="**Join Method**", value=join_method)
        embed.add_field(name="**Skill Requirement**", value=skill_requirement)
        embed.add_field(name="**CG Donation Requirement**", value=f"{donation_requirement}")
        if extra_info:
            embed.add_field(name="**Extra Information**", value=f"{extra_info}")
        embed.set_author(name=ctx.author.username, icon_url=ctx.author.avatar.url)
        embed.set_footer(text="Report any scammers via support tickets.", icon_url=FOOTER_IMAGE)
        if clan.badge:
            embed.set_thumbnail(url=clan.badge.url)

        clan_link_button = ipy.Button(
            style=ipy.ButtonStyle.URL,
            label="Clan Link",
            url=clan.share_link,
            emoji=CLAN_EMOJI
        )

        await ctx.send(embed=embed, components=clan_link_button, ephemeral=False)

    @ipy.message_context_menu(name="Delete Post", scopes=GUILD_IDS)
    async def delete_post_message_command(self, ctx: ipy.ContextMenuContext):
        """Delete your raid post."""
        if int(ctx.channel.id) != RAID_CHANNEL:
            await ctx.send(f"{ERROR_EMOJI} This is not a raid post.", ephemeral=True)

            return

        message = ctx.target
        if not message.interaction or int(message.interaction.bot.user.id) != int(self.bot.user.id):
            await ctx.send(f"{ERROR_EMOJI} This is not a raid post.", ephemeral=True)

            return

        if str(message.interaction.user.id) != str(ctx.author.id):
            await ctx.respond("You are not the author of this post.", ephemeral=True)

            return

        await message.delete()

        await ctx.send(f"{SUCCESS_EMOJI} The post has been deleted.", ephemeral=True)

    @ipy.listen(ipy.events.MessageCreate)
    async def on_message(self, event: ipy.events.MessageCreate):
        message = event.message

        if int(message.channel.id) != RAID_CHANNEL:
            return

        if not isinstance(message.author, ipy.Member):
            return

        if message.author.has_any_role(LVL1_ROLES):
            return

        if message.interaction and int(message.interaction.bot.user.id) == int(self.bot.user.id):
            return

        await message.delete()

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Raid commands loaded")
