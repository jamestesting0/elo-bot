import logging
import asyncio
import re
from copy import deepcopy
from typing import Literal

from pymongo.errors import PyMongoError

import cogs.match_commands as match_commands
import interactions as ipy
from common.constants import (
    ALEX_EMOJI, BANNER_IMAGE, CHAT_CHANNEL, CLASHING_SWORD_EMOJI, COLOR, ERROR_EMOJI, FOOTER_IMAGE, GUILD_IDS,
    HOST_CLAN_OPTIONS, INFO_CHANNEL, LVL1_ROLES, MANAGER_ROLE, PLAYER_ROLE, REWARDS_IMAGE, STAFF_ROLE, SUCCESS_EMOJI,
)
from common.discord.embeds import embed_edit_payload
from database_models import Clan, Match, Player, ServerState
from common.utils import collect_base_store_slot_owners, format_clan_availability_status, mention_user, member_guild


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
            await message.edit(embed=embed_edit_payload(embed), components=components or [])
        except (PyMongoError, ipy.errors.HTTPException, RuntimeError):
            logging.exception("Dashboard publication failed in channel %s", ctx.channel.id)
            status_note = "Its controls are unavailable."
            if message is not None:
                unavailable = deepcopy(embed)
                unavailable.title += " `(Unavailable)`"
                unavailable.description = "This dashboard could not be activated. Please create it again."
                unavailable.fields = []
                try:
                    await message.edit(embed=unavailable, components=[])
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
            ipy.SlashCommandChoice(name="HOTW Information", value="HOTW Information"),
            ipy.SlashCommandChoice(name="GTBB Information", value="GTBB Information"),
            ipy.SlashCommandChoice(name="Hosting Queue", value="Hosting Queue"),
            ipy.SlashCommandChoice(name="Clan Availability", value="Clan Availability")
        ],
        opt_type=ipy.OptionType.STRING
    )
    async def embed(self, ctx: ipy.SlashContext, embed_type: str):
        await ctx.defer(ephemeral=True)

        if embed_type == "Elo Information":
            description = open("assets/information.md", "r").read()

            embed = ipy.Embed(
                title="**Clan Capital Elo System**",
                description=description,
                footer=ipy.EmbedFooter(text="Clan capital elo system", icon_url=FOOTER_IMAGE),
                images=[ipy.EmbedAttachment(url=BANNER_IMAGE)],
                color=COLOR,
            )

            participate_button = ipy.Button(
                style=ipy.ButtonStyle.GRAY,
                label="Participate",
                custom_id="participate_button",
                emoji=ipy.PartialEmoji(name="war", id=1050832305410805760)
            )

            build_button = ipy.Button(
                style=ipy.ButtonStyle.GRAY,
                label="Base Building",
                custom_id="build_button",
                emoji=ipy.PartialEmoji(name="grandbuilder", id=1294298302069739642)
            )

            rewards_button = ipy.Button(
                style=ipy.ButtonStyle.GRAY,
                label="Rewards",
                custom_id="rewards_button",
                emoji=ipy.PartialEmoji(name="goldpass", id=947797613825105950)
            )

            support_button = ipy.Button(
                style=ipy.ButtonStyle.GRAY,
                label="Support",
                custom_id="support_button",
                emoji="🛠️"
            )

            rules_button = ipy.Button(
                style=ipy.ButtonStyle.GRAY,
                label="Rules",
                custom_id="rules_button",
                emoji="📜"
            )

            docs_button = ipy.Button(
                style=ipy.ButtonStyle.URL,
                label="Documentation",
                url="https://docs.google.com/document/d/1__Ufp_KCFrTaiFMvEouafyGn08quPKkIo50OFvswkUw/edit#heading=h"
                    ".kz8yj8uavt8i",
                emoji=ipy.PartialEmoji(name="googledocs", id=1294293700775641109)
            )

            video_button = ipy.Button(
                style=ipy.ButtonStyle.URL,
                label="Youtube",
                url="https://www.youtube.com/watch?v=1bUDs5g-YOE&list=PLCBSjHpUFbfGiwqK-xf1R5FdGqrFBf3rk&pp=gAQB",
                emoji=ipy.PartialEmoji(name="youtube", id=1124367780788056074)
            )

            await ctx.channel.send(
                embed=embed,
                components=[
                    ipy.ActionRow(participate_button, support_button),
                    ipy.ActionRow(rewards_button, build_button),
                    ipy.ActionRow(rules_button, docs_button),
                    ipy.ActionRow(video_button)
                ]
            )

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

    @ipy.component_callback("rules_button")
    async def rules_button(self, ctx: ipy.ComponentContext):
        description = open("assets/rules.md", "r").read()

        embed = ipy.Embed(
            title="**Elo Rules**",
            description=description,
            footer=ipy.EmbedFooter(text="Clan capital elo system", icon_url=FOOTER_IMAGE),
            color=COLOR,
        )

        await ctx.send(embed=embed, ephemeral=True)

    @ipy.component_callback("build_button")
    async def build_button(self, ctx: ipy.ComponentContext):
        description = open("assets/base_building.md", "r").read()

        embed = ipy.Embed(
            title="**Base Building Contest**",
            description=description,
            footer=ipy.EmbedFooter(text="Clan capital elo system", icon_url=FOOTER_IMAGE),
            color=COLOR,
        )

        await ctx.send(embed=embed, ephemeral=True)

    @ipy.component_callback("rewards_button")
    async def rewards_button(self, ctx: ipy.ComponentContext):
        description = open("assets/rewards.md", "r").read()

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
                emoji=ipy.PartialEmoji(name="elomanager", id=1294394779487961118)
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
                emoji=ipy.PartialEmoji(name="botdeveloper", id=1294394777411915796)
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
        if int(event.guild.id) not in GUILD_IDS:
            return

        before_roles = {int(role.id) for role in event.before.roles}
        after_roles = {int(role.id) for role in event.after.roles}
        if not (PLAYER_ROLE not in before_roles and PLAYER_ROLE in after_roles):
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
            emoji=ipy.PartialEmoji(name="war", id=1050832305410805760)
        )

        guide = open("assets/quickstart.md", "r").read()
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
                f"{await match_commands.available_resource_clans_text()}",
                ephemeral=True,
            )
            return

        reservation = await match_commands.try_reserve_resource_clan(clan_tag)
        if not reservation:
            await ctx.send(
                f"{ERROR_EMOJI} The clan availability is currently changing.\n\n"
                f"{await match_commands.available_resource_clans_text()}",
                ephemeral=True,
            )
            return
        released = await match_commands.free_clan(
            clan_tag,
            expected_availability=clan.availability_str,
        )
        if not released:
            await ctx.send(
                f"{ERROR_EMOJI} The clan availability changed before it could be freed.\n\n"
                f"{await match_commands.available_resource_clans_text()}",
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
