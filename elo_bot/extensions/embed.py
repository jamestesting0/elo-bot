import logging
import asyncio
import re
from copy import deepcopy
from typing import Literal

from pymongo.errors import PyMongoError

import interactions as ipy
from elo_bot.discord.message_edits import edit_channel_message
from elo_bot.config.emojis import ALEX_EMOJI, CLASHING_SWORD_EMOJI, ERROR_EMOJI, SUCCESS_EMOJI, application_emoji
from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, REWARDS_IMAGE
from elo_bot.config.ids import CHAT_CHANNEL, GUILD_IDS, INFO_CHANNEL, LVL1_ROLES, MANAGER_ROLE, PLAYER_ROLE, STAFF_ROLE
from elo_bot.config.game import HOST_CLAN_OPTIONS
from elo_bot.discord.embeds import embed_edit_payload
from elo_bot.db import Clan, Match, Player, ServerState
from elo_bot.features.base_store.slots import collect_base_store_slot_owners, format_clan_availability_status
from elo_bot.discord.mentions import mention_user
from elo_bot.discord.members import member_guild
from elo_bot.features.matches import resources
from elo_bot.features.embeds.static import STATIC_EMBED_TYPES, static_embed
from elo_bot.features.embeds.readme import README_SECTIONS, readme_page
from elo_bot.features.sticky.commands import post_embed_sticky


class EmbedCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot

    async def _publish_dashboard(
        self, ctx: ipy.SlashContext, embed: ipy.Embed, *,
        state_field: Literal["hosting_queue_embed", "clan_availability_embed"],
        components: list[ipy.ActionRow] | None = None,
    ) -> bool:
        pending = deepcopy(embed)
        pending.description = "Loading…"
        message = None
        registered = False
        try:
            message = await ctx.channel.send(embed=pending, components=[])
            await ServerState.update_fields(**{
                state_field: {"channel": int(ctx.channel.id), "message": int(message.id)},
            })
            registered = True
            await edit_channel_message(message, embed=embed_edit_payload(embed), components=components or [])
        except (PyMongoError, ipy.errors.HTTPException, RuntimeError):
            logging.exception("Dashboard publication failed in channel %s", ctx.channel.id)
            status_note = "Its controls are unavailable."
            if message is not None:
                unavailable = deepcopy(embed)
                unavailable.title += " `(Unavailable)`"
                unavailable.description = "This dashboard could not be activated. Please create it again."
                unavailable.fields = []
                try:
                    await edit_channel_message(message, embed=unavailable, components=[])
                except ipy.errors.HTTPException:
                    logging.exception("Dashboard failure status could not be published for message %s", message.id)
                    status_note = "The announcement could not be updated to show this failure."
            failure = ("The dashboard was registered, but could not be activated." if registered else
                       "The dashboard could not be registered.")
            await ctx.send(f"{ERROR_EMOJI} {failure} {status_note}", ephemeral=True)
            return False
        return True

    @ipy.slash_command(name="embed", description="Send pre-made embeds", scopes=GUILD_IDS)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="embed_type",
        description="Type of the embed",
        choices=[
            ipy.SlashCommandChoice(name="Elo Information", value="Elo Information"),
            ipy.SlashCommandChoice(name="Elo Final", value="Elo Final"),
            ipy.SlashCommandChoice(name="HOTW Information", value="HOTW Information"),
            ipy.SlashCommandChoice(name="GTBB Information", value="GTBB Information"),
            ipy.SlashCommandChoice(name="Hosting Queue", value="Hosting Queue"),
            ipy.SlashCommandChoice(name="Clan Availability", value="Clan Availability")
        ],
        opt_type=ipy.OptionType.STRING
    )
    @ipy.slash_option(
        name="sticky",
        description="Keep this embed at the bottom of the channel, like /sticky",
        opt_type=ipy.OptionType.BOOLEAN,
    )
    async def embed(self, ctx: ipy.SlashContext, embed_type: str, sticky: bool = False):
        await ctx.defer(ephemeral=True)

        if embed_type in STATIC_EMBED_TYPES:
            if sticky:
                await post_embed_sticky(ctx.channel, embed_type)
            else:
                embed, components = await static_embed(embed_type)
                await ctx.channel.send(embed=embed, components=components)

        elif sticky:
            await ctx.send(f"{ERROR_EMOJI} Only Elo Information and Elo Final can be sticky.", ephemeral=True)
            return

        elif embed_type == "Hosting Queue":
            embed = ipy.Embed(
                title="**Hosting Queue**",
                footer=ipy.EmbedFooter(text="Clan capital elo system", icon_url=FOOTER_IMAGE),
                color=COLOR,
            )

            if not await self._publish_dashboard(ctx, embed, state_field="hosting_queue_embed"):
                return

        elif embed_type == "Clan Availability":
            embed = ipy.Embed(
                title="**Clan Availability**",
                description="Matches started 30 minutes ago are likely to be finished. Please go "
                            "to the corresponding clan and store the results. **0.5 point** will be "
                            "rewarded.",
                footer=ipy.EmbedFooter(text="Clan capital elo system", icon_url=FOOTER_IMAGE),
                color=COLOR,
            )

            clans = await Clan.find_all().to_list()
            matches = await Match.find().to_list()
            slot_owners = collect_base_store_slot_owners(matches)
            statuses = {c.tag: format_clan_availability_status(c, slot_owners) for c in clans}
            user_ids = list(dict.fromkeys(re.findall(r"<@!?(\d+)>", "\n".join(statuses.values()))))
            guild = await member_guild(ctx)
            members = await asyncio.gather(*(guild.fetch_member(user_id) for user_id in user_ids))
            names = {user_id: mention_user(member, user_id) for user_id, member in zip(user_ids, members)}
            for c in clans:
                embed.add_field(
                    name=f"**{str(c.name)} ({str(c.tag)})**",
                    value=re.sub(r"<@!?(\d+)>", lambda mention: names[mention.group(1)], statuses[c.tag]),
                )

            free_clan_select = ipy.StringSelectMenu(
                *HOST_CLAN_OPTIONS,
                placeholder="🛡️Select a clan to free manually",
                custom_id="free_clan_select"
            )

            view_accounts_button = ipy.Button(
                style=ipy.ButtonStyle.GRAY,
                label="View Accounts",
                custom_id="view_accounts",
                emoji="🔎"
            )

            if not await self._publish_dashboard(
                ctx, embed, state_field="clan_availability_embed",
                components=[ipy.ActionRow(free_clan_select), ipy.ActionRow(view_accounts_button)],
            ):
                return

        else:
            await ctx.send(f"{ERROR_EMOJI} This embed is yet to be implemented.", ephemeral=True)

            return

        await ctx.send(f"{SUCCESS_EMOJI} The embed has been sent.", ephemeral=True)

    @ipy.component_callback("elo_final_details")
    async def elo_final_details(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)
        embed, components = readme_page("elo_final", int(ctx.author.id))
        await ctx.send(embed=embed, components=components, ephemeral=True)

    @ipy.component_callback("readme_button")
    async def readme_button(self, ctx: ipy.ComponentContext):
        embed, components = readme_page("getting_started", int(ctx.author.id))
        await ctx.send(embed=embed, components=components, ephemeral=True)

    @ipy.component_callback(re.compile(r"^readme_section\|\d+$"))
    async def readme_section(self, ctx: ipy.ComponentContext):
        if ctx.custom_id.split("|")[1] != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.", ephemeral=True)
            return
        section = ctx.values[0]
        if section not in README_SECTIONS:
            await ctx.send(f"{ERROR_EMOJI} This page no longer exists.", ephemeral=True)
            return
        await ctx.defer(edit_origin=True)
        embed, components = readme_page(section, int(ctx.author.id))
        await ctx.edit_origin(embed=embed, components=components)

    @ipy.component_callback("build_button")
    async def build_button(self, ctx: ipy.ComponentContext):
        description = open("assets/embeds/base_building.md", "r").read()

        embed = ipy.Embed(
            title="**Base Building Contest**",
            description=description,
            footer=ipy.EmbedFooter(text="Clan capital elo system", icon_url=FOOTER_IMAGE),
            color=COLOR,
        )

        await ctx.send(embed=embed, ephemeral=True)

    @ipy.component_callback("rewards_button")
    async def rewards_button(self, ctx: ipy.ComponentContext):
        description = open("assets/embeds/rewards.md", "r").read()

        embed = ipy.Embed(
            title="**Elo Rewards**",
            description=description,
            footer=ipy.EmbedFooter(text="Clan capital elo system", icon_url=FOOTER_IMAGE),
            color=COLOR,
        )
        embed.add_image(REWARDS_IMAGE)

        await ctx.send(embed=embed, ephemeral=True)

    @ipy.component_callback("participate_button")
    async def participate_button(self, ctx: ipy.ComponentContext):
        if isinstance(ctx.author, ipy.Member):
            await ctx.author.add_role(PLAYER_ROLE)

        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if player:
            play_now_button = ipy.Button(
                style=ipy.ButtonStyle.GRAY,
                label="Play Now",
                custom_id="match_queue",
                emoji=CLASHING_SWORD_EMOJI
            )

            await ctx.send(f"{SUCCESS_EMOJI} You are already registered in the Elo system, you may start match-making "
                           f"right now!", components=play_now_button, ephemeral=True)

            return

        modal = ipy.Modal(
            ipy.ShortText(
                label="Enter your in-game account tag",
                placeholder="The account will need to leave and join clans",
                max_length=40,
                custom_id="player_tag"
            ),
            title="Elo Registration",
            custom_id="elo_registration_modal",
        )

        await ctx.send_modal(modal)

    @ipy.component_callback("support_button")
    async def support_button(self, ctx: ipy.ComponentContext):
        select_menu = ipy.StringSelectMenu(
            ipy.StringSelectOption(
                label="Elo Manager",
                value="Elo Manager",
                description="For important topics such as breach of fairplay and suggestions to the system.",
                emoji=application_emoji("elomanager")
            ),
            ipy.StringSelectOption(
                label="1v1 Helper",
                value="1v1 Helper",
                description="Questions about Elo rules and commands.",
                emoji="👨‍💼"

            ),
            ipy.StringSelectOption(
                label="Bot Developer",
                value="Bot Developer",
                description="For issues regarding the Elo bot such as bugs and feature suggestions.",
                emoji=application_emoji("botdeveloper")
            ),
            placeholder="Select the group you wish to consult",
            custom_id="support_select"
        )

        await ctx.send("Please select the **appropriate** group for assistance. "
                       f"For quick questions, try asking in f<#{CHAT_CHANNEL}> first.",
                       components=select_menu, ephemeral=True)

    @ipy.component_callback("support_select")
    async def support_select(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        info_channel = await self.bot.fetch_channel(INFO_CHANNEL)
        for thread in (await info_channel.fetch_all_threads()).threads:
            if thread.name == ctx.author.username:
                await ctx.edit_origin(content=f"{ERROR_EMOJI} You already have an existing thread {thread.mention}.",
                                      components=[])

                return

        support_thread = await info_channel.create_private_thread(
            name=ctx.author.username,
            invitable=False,
            auto_archive_duration=ipy.AutoArchiveDuration.THREE_DAY
        )

        if ctx.values[0] == "Elo Manager":
            mention = f"<@&{MANAGER_ROLE}>"
        elif ctx.values[0] == "1v1 Helper":
            mention = f"<@&{STAFF_ROLE}>"
        else:
            mention = self.bot.owner.mention

        embed = ipy.Embed(
            title="**Private Support Ticket**",
            description="Please clearly write your issues in the chat while waiting for a staff member to arrive.",
            footer=ipy.EmbedFooter(text="Clan capital elo system", icon_url=FOOTER_IMAGE),
            color=COLOR,
        )

        await support_thread.send(f"{ctx.author.mention}\n{mention}", embed=embed)

        await ctx.edit_origin(content=f"{SUCCESS_EMOJI} A private thread has been made for you, please state your "
                              f"needs in {support_thread.mention}!", components=[])

    @ipy.listen(ipy.events.MemberUpdate)
    async def on_member_update(self, event: ipy.events.MemberUpdate):
        # An uncached member has no earlier roles, so a newly granted Player role cannot be told apart.
        if int(event.guild.id) not in GUILD_IDS or event.before is ipy.MISSING:
            return
        if event.before.has_role(PLAYER_ROLE) or not event.after.has_role(PLAYER_ROLE):
            return

        player = await Player.find_one(Player.user_id == str(event.before.id))
        if player:
            return

        # avoid sending the quickstart guide multiple times
        player_dm = await event.before.fetch_dm(force=True)
        messages = await player_dm.fetch_messages(limit=5)
        for message in messages:
            if "Competitive Quickstart" in message.content:
                return

        participate_button = ipy.Button(
            style=ipy.ButtonStyle.GRAY,
            label="Participate",
            custom_id="participate_button",
            emoji=application_emoji("war")
        )

        guide = open("assets/embeds/quickstart.md", "r").read()
        try:
            await event.after.send(guide, components=participate_button)
        except ipy.errors.Forbidden:
            chat_channel = await self.bot.fetch_channel(CHAT_CHANNEL)
            await chat_channel.send(f"{event.after.mention}\n{ALEX_EMOJI} Glad that you are interested "
                                    f"in 1v1 Elo matches, check "
                                    f"out <#{INFO_CHANNEL}> for rules and rewards. Click on the **\"Participate\"** to "
                                    f"get registered and start playing in seconds!\n\n"
                                    f"-# To have the best experience in Elo, we recommend you to "
                                    f"enable direct messages for the bot!")

    @ipy.component_callback("free_clan_select")
    async def free_clan_select(self, ctx: ipy.ComponentContext):
        if not ctx.author.has_role(MANAGER_ROLE):
            await ctx.send(f"{ERROR_EMOJI} Only **Elo Managers** can free clans manually.", ephemeral=True)
            return

        clan_tag = ctx.values[0]
        clan = await Clan.find_one(Clan.tag == clan_tag)
        if not clan:
            await ctx.send(f"{ERROR_EMOJI} Could not find clan with tag `{clan_tag}`.", ephemeral=True)
            return

        if clan.availability_str == "Available":
            await ctx.send(
                f"{ERROR_EMOJI} The clan is already available.\n\n"
                f"{await resources.available_resource_clans_text()}",
                ephemeral=True,
            )
            return

        reservation = await resources.try_reserve_resource_clan(clan_tag)
        if not reservation:
            await ctx.send(
                f"{ERROR_EMOJI} The clan availability is currently changing.\n\n"
                f"{await resources.available_resource_clans_text()}",
                ephemeral=True,
            )
            return
        released = await resources.free_clan(
            clan_tag,
            expected_availability=clan.availability_str,
        )
        if not released:
            await ctx.send(
                f"{ERROR_EMOJI} The clan availability changed before it could be freed.\n\n"
                f"{await resources.available_resource_clans_text()}",
                ephemeral=True,
            )
            return

        await ctx.send(
            f"{SUCCESS_EMOJI} The clan is made available, the change will show in around 1 minute.",
            ephemeral=True,
        )

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Information commands loaded")
