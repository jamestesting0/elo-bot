import math
import asyncio
import re
from typing import List, assert_never

import coc
from beanie.exceptions import DocumentNotFound
from pymongo.errors import PyMongoError

import interactions as ipy
from common.constants import (
    CLAN_EMOJI, COLOR, ERROR_EMOJI, FOOTER_IMAGE, FOOTER_TEXT, GUILD_IDS, HOST_CLAN_CHOICES, LB_ENTRIES_PER_PAGE,
    LVL1_ROLES, LVL2_ROLES, RANK_EMOJIS, STAFF_ROLE, SUCCESS_EMOJI, TOWNHALL_EMOJIS,
)
from database_models import Helper
from common.discord.pagination import InvalidPageError, build_page_button, build_page_jump_modal, parse_page_number
from common.utils import mention_user, member_guild


async def generate_account_helper_pairs() -> dict:
    """Generate a mapping of account tags to helper user IDs."""
    account_helper_pair = {}
    helpers: List[Helper] = await Helper.find_all().to_list()
    for helper in helpers:
        for acc in helper.accounts:
            account_helper_pair[acc] = helper.user_id
    return account_helper_pair


def replace_special_chars(str_input: str, replacement: str):
    return ''.join(c if c.isalpha() or c.isnumeric() else replacement for c in str_input)


async def extract_player_tags(client: coc.Client, str_input: str, context: ipy.SlashContext) -> list[str]:
    valid_tags = []
    sections = replace_special_chars(str_input, " ").split(" ")

    for s in sections:
        if not coc.utils.is_valid_tag(s):
            continue

        try:
            await client.get_player(s)
        except coc.errors.NotFound:
            if context:
                await context.send(f"<:error:827078558140334100> `{s}` is invalid.", ephemeral=True)

            continue

        valid_tags.append(coc.utils.correct_tag(s))

    return valid_tags


async def construct_helper_leaderboard_string(guild: ipy.Guild, helpers_sorted: List[Helper], lb_type: str,
                                              starting_index: int = 0) -> str:
    description = ""
    index = 0
    for helper in helpers_sorted[starting_index:]:
        if index >= LB_ENTRIES_PER_PAGE:
            break

        rank_index = index + starting_index
        if rank_index > 2:
            emoji = f"**#{rank_index + 1}**"
        else:
            emoji = RANK_EMOJIS[rank_index]

        member = await guild.fetch_member(helper.user_id)
        user_mention = mention_user(member, helper.user_id, mention_type="Helper")

        if lb_type == "Points":
            points = helper.points
        else:
            points = sum(0.5 if h.player2 is None else 1 for h in helper.history)

        description += f"{emoji} {user_mention} `{points:.1f}`\n"

        index += 1

    return description


class HelperCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot

    @property
    def coc_client(self) -> coc.Client:
        return self.bot.coc_client
    
    base = ipy.SlashCommand(name="helper", description="", scopes=GUILD_IDS)

    @base.subcommand(sub_cmd_name="leaderboard", sub_cmd_description="List the number of matches the helpers hosted")
    @ipy.slash_option(
        name="lb_type",
        description="Type of leaderboard",
        choices=[
            ipy.SlashCommandChoice(name="Points", value="Points"),
            ipy.SlashCommandChoice(name="Matches Hosted", value="Matches Host")
        ],
        opt_type=ipy.OptionType.STRING
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def helper_leaderboard(self, ctx: ipy.SlashContext, lb_type: str = "Points", hidden: bool = False):
        await ctx.defer(ephemeral=hidden)

        helpers: List[Helper] = await Helper.find_all().to_list()
        if not helpers:
            await ctx.send(f"{ERROR_EMOJI} There are no helpers.", ephemeral=True)
            return

        if lb_type == "Points":
            helpers_sorted = sorted(helpers, key=lambda h: h.points, reverse=True)
        else:
            helpers_sorted = sorted(
                helpers,
                key=lambda h: sum(0.5 if entry.player2 is None else 1 for entry in h.history),
                reverse=True
            )

        description = await construct_helper_leaderboard_string(ctx.guild, helpers_sorted, lb_type)

        embed = ipy.Embed(
            title=f"**Helper {lb_type} Leaderboard**",
            description=description,
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"helper_lb_left|{0 - LB_ENTRIES_PER_PAGE}",
            disabled=True,
        )

        total_pages = math.ceil(len(helpers_sorted) / LB_ENTRIES_PER_PAGE)
        current_page = int(0 / LB_ENTRIES_PER_PAGE) + 1
        page_number_button = build_page_button(current_page, total_pages, "helper_lb_page")

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"helper_lb_right|{0 + LB_ENTRIES_PER_PAGE}",
            disabled=(0 + LB_ENTRIES_PER_PAGE) >= len(helpers_sorted)
        )

        components = [ipy.ActionRow(left_button, page_number_button, right_button)]

        await ctx.send(embed=embed, components=components, ephemeral=hidden)

    @ipy.component_callback(re.compile(r"helper_lb_right\|.*"))
    async def helper_lb_right_button(self, ctx: ipy.ComponentContext):
        if str(ctx.message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)
            return

        lb_type = ctx.message.embeds[0].title.replace("**Helper ", "").replace(" Leaderboard**", "")
        _, starting_index = ctx.custom_id.split("|")
        starting_index = int(starting_index)

        helpers: List[Helper] = await Helper.find_all().to_list()
        if not helpers:
            await ctx.send(f"{ERROR_EMOJI} No helper have hosted any matches.", ephemeral=True)
            return

        if lb_type == "Points":
            helpers_sorted = sorted(helpers, key=lambda h: h.points, reverse=True)
        else:
            helpers_sorted = sorted(
                helpers,
                key=lambda h: sum(0.5 if entry.player2 is None else 1 for entry in h.history),
                reverse=True
            )

        leaderboard = await construct_helper_leaderboard_string(ctx.guild, helpers_sorted, lb_type, starting_index)
        ctx.message.embeds[0].description = leaderboard

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"helper_lb_left|{starting_index - LB_ENTRIES_PER_PAGE}",
            disabled=False,
        )

        total_pages = math.ceil(len(helpers_sorted) / LB_ENTRIES_PER_PAGE)
        current_page = int(starting_index / LB_ENTRIES_PER_PAGE) + 1
        page_number_button = build_page_button(current_page, total_pages, "helper_lb_page")

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"helper_lb_right|{starting_index + LB_ENTRIES_PER_PAGE}",
            disabled=(starting_index + LB_ENTRIES_PER_PAGE) >= len(helpers_sorted)
        )

        ctx.message.components[0] = ipy.ActionRow(left_button, page_number_button, right_button)

        await ctx.edit_origin(embeds=ctx.message.embeds, components=ctx.message.components)

    @ipy.component_callback(re.compile(r"helper_lb_left\|.*"))
    async def helper_lb_left_button(self, ctx: ipy.ComponentContext):
        if str(ctx.message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)
            return

        lb_type = ctx.message.embeds[0].title.replace("**Helper ", "").replace(" Leaderboard**", "")
        _, starting_index = ctx.custom_id.split("|")
        starting_index = int(starting_index)

        helpers: List[Helper] = await Helper.find_all().to_list()
        if not helpers:
            await ctx.send(f"{ERROR_EMOJI} No helper have hosted any matches.", ephemeral=True)
            return

        if lb_type == "Points":
            helpers_sorted = sorted(helpers, key=lambda h: h.points, reverse=True)
        else:
            helpers_sorted = sorted(
                helpers,
                key=lambda h: sum(0.5 if entry.player2 is None else 1 for entry in h.history),
                reverse=True
            )

        leaderboard = await construct_helper_leaderboard_string(ctx.guild, helpers_sorted, lb_type, starting_index)
        ctx.message.embeds[0].description = leaderboard

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"helper_lb_left|{starting_index - LB_ENTRIES_PER_PAGE}",
            disabled=(starting_index - LB_ENTRIES_PER_PAGE) < 0,
        )

        total_pages = math.ceil(len(helpers_sorted) / LB_ENTRIES_PER_PAGE)
        current_page = int(starting_index / LB_ENTRIES_PER_PAGE) + 1
        page_number_button = build_page_button(current_page, total_pages, "helper_lb_page")

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"helper_lb_right|{starting_index + LB_ENTRIES_PER_PAGE}",
            disabled=False
        )

        ctx.message.components[0] = ipy.ActionRow(left_button, page_number_button, right_button)

        await ctx.edit_origin(embeds=ctx.message.embeds, components=ctx.message.components)

    @ipy.component_callback("helper_lb_page")
    async def helper_lb_page_button(self, ctx: ipy.ComponentContext):
        if str(ctx.message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)
            return

        await ctx.send_modal(build_page_jump_modal("helper_lb_page_modal"))

    @ipy.modal_callback("helper_lb_page_modal")
    async def helper_lb_page_modal(self, ctx: ipy.ModalContext, **response):
        message = ctx.message
        if message is None:
            await ctx.send(f"{ERROR_EMOJI} The paginated message is no longer available.", ephemeral=True)
            return

        if str(message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)
            return

        lb_type = message.embeds[0].title.replace("**Helper ", "").replace(" Leaderboard**", "")
        helpers: List[Helper] = await Helper.find_all().to_list()
        if not helpers:
            await ctx.send(f"{ERROR_EMOJI} No helper have hosted any matches.", ephemeral=True)
            return

        if lb_type == "Points":
            helpers_sorted = sorted(helpers, key=lambda helper: helper.points, reverse=True)
        else:
            helpers_sorted = sorted(
                helpers,
                key=lambda helper: sum(0.5 if entry.player2 is None else 1 for entry in helper.history),
                reverse=True,
            )

        total_pages = math.ceil(len(helpers_sorted) / LB_ENTRIES_PER_PAGE)
        try:
            page = parse_page_number(response["page"], total_pages)
        except InvalidPageError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return

        starting_index = (page - 1) * LB_ENTRIES_PER_PAGE
        leaderboard = await construct_helper_leaderboard_string(
            ctx.guild, helpers_sorted, lb_type, starting_index
        )
        message.embeds[0].description = leaderboard

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"helper_lb_left|{starting_index - LB_ENTRIES_PER_PAGE}",
            disabled=starting_index == 0,
        )
        page_number_button = build_page_button(page, total_pages, "helper_lb_page")
        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"helper_lb_right|{starting_index + LB_ENTRIES_PER_PAGE}",
            disabled=(starting_index + LB_ENTRIES_PER_PAGE) >= len(helpers_sorted),
        )

        message.components[0] = ipy.ActionRow(left_button, page_number_button, right_button)
        await ctx.edit(message, embeds=message.embeds, components=message.components)

    @base.subcommand(sub_cmd_name="info", sub_cmd_description="View a helper's hosting information")
    @ipy.slash_option(
        name="helper",
        description="A 1v1 helper",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def helper_info(self, ctx: ipy.SlashContext, helper: ipy.Member, hidden: bool = False):
        await ctx.defer(ephemeral=hidden)

        if STAFF_ROLE not in [int(role.id) for role in helper.roles]:
            await ctx.send(f"{ERROR_EMOJI} {helper.mention} is not a 1v1 helper.", ephemeral=True)
            return

        helper_doc = await Helper.find_one(Helper.user_id == str(helper.id))
        if not helper_doc:
            helper_doc = Helper(user_id=str(helper.id))

        matches_hosted = sum(0.5 if h.player2 is None else 1 for h in helper_doc.history)
        points = helper_doc.points
        description = ""
        count = 0
        user_ids = {user_id for entry in helper_doc.history[-15:]
                    for user_id in (entry.player1, entry.player2) if user_id}
        guild = await member_guild(ctx)
        members = await asyncio.gather(*(guild.fetch_member(user_id) for user_id in user_ids))
        names = {user_id: mention_user(member, user_id) for user_id, member in zip(user_ids, members)}
        for entry in reversed(helper_doc.history):
            if count >= 15:
                break

            count += 1
            if not entry.time:
                ts = "`Unknown Time`"
            else:
                ts = ipy.Timestamp.fromtimestamp(entry.time).format(style=ipy.TimestampStyles.RelativeTime)

            if entry.player2:
                description += f"{ts} {names[entry.player1]} vs. {names[entry.player2]}\n"
            else:
                description += f"{ts} {names[entry.player1]}\n"

        embed = ipy.Embed(
            title=f"**{helper} Host History**",
            description=description if description else "No matches hosted.",
            fields=[
                ipy.EmbedField(name="**Helper Points**", value=str(points)),
                ipy.EmbedField(name="**Matches Hosted**", value=str(matches_hosted))
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        await ctx.send(embed=embed, ephemeral=hidden)

    point_group = base.group(name="point", description="")

    @point_group.subcommand(sub_cmd_name="remove", sub_cmd_description="Remove points from a helper")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="helper",
        description="A 1v1 helper",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="points",
        description="Points to remove",
        opt_type=ipy.OptionType.INTEGER,
        min_value=0,
        required=True
    )
    async def helper_point_remove(self, ctx: ipy.SlashContext, helper: ipy.Member, points: int):
        await ctx.defer(ephemeral=True)

        if STAFF_ROLE not in [int(role.id) for role in helper.roles]:
            await ctx.send(f"{ERROR_EMOJI} {helper.mention} is not a 1v1 helper.", ephemeral=True)
            return

        helper_doc = await Helper.find_one(Helper.user_id == str(helper.id))
        if not helper_doc:
            await ctx.send(f"{ERROR_EMOJI} {helper.mention} has no helper record.", ephemeral=True)
            return

        await helper_doc.update({"$inc": {"points": -points}})

        await ctx.send(f"{SUCCESS_EMOJI} `{points}` are removed from {helper.mention}.", ephemeral=True)

    @point_group.subcommand(sub_cmd_name="add", sub_cmd_description="Add points from a helper")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="helper",
        description="A 1v1 helper",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="points",
        description="Points to add",
        opt_type=ipy.OptionType.INTEGER,
        min_value=0,
        required=True
    )
    async def helper_point_add(self, ctx: ipy.SlashContext, helper: ipy.Member, points: int):
        await ctx.defer(ephemeral=True)

        if STAFF_ROLE not in [int(role.id) for role in helper.roles]:
            await ctx.send(f"{ERROR_EMOJI} {helper.mention} is not a 1v1 helper.", ephemeral=True)
            return

        helper_doc = await Helper.find_one(Helper.user_id == str(helper.id))
        if not helper_doc:
            helper_doc = Helper(user_id=str(helper.id), points=points)
            await helper_doc.insert()
        else:
            await helper_doc.update({"$inc": {"points": points}})

        await ctx.send(f"{SUCCESS_EMOJI} `{points}` are added to {helper.mention}.", ephemeral=True)

    @base.subcommand(sub_cmd_name="remove", sub_cmd_description="Remove a helper")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="user",
        description="A user to be removed from the helpers.",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    async def helper_remove(self, ctx: ipy.SlashContext, user: ipy.User | ipy.Member):
        await ctx.defer(ephemeral=True)

        helper_doc = await Helper.find_one(Helper.user_id == str(user.id))
        if helper_doc:
            await helper_doc.delete()

        match user:
            case ipy.Member():
                if STAFF_ROLE in [int(role.id) for role in user.roles]:
                    await user.remove_role(STAFF_ROLE)
            case ipy.User():
                pass
            case unreachable:
                assert_never(unreachable)

        await ctx.send(f"{SUCCESS_EMOJI} {user.mention} is removed from the helpers.", ephemeral=True)

    @base.subcommand(sub_cmd_name="clan", sub_cmd_description="Get the link of a helper clan")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="clan",
        description="The clan for helpers to host Elo matches",
        opt_type=ipy.OptionType.STRING,
        choices=HOST_CLAN_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def helper_clan(self, ctx: ipy.SlashContext, clan: str, hidden: bool = False):
        await ctx.defer(ephemeral=hidden)

        helpers: List[Helper] = await Helper.find_all().to_list()
        clan = await self.coc_client.get_clan(clan)
        helpers_str = ""
        for helper_doc in helpers:
            for account_tag in helper_doc.accounts:
                if account_tag in clan.members_dict:
                    try:
                        account = await self.coc_client.get_player(account_tag)
                    except coc.NotFound:
                        continue
                    guild = await member_guild(ctx)
                    member = await guild.fetch_member(helper_doc.user_id)
                    helpers_str += (f"{mention_user(member, helper_doc.user_id, mention_type='Helper')} "
                                    f"[{account.name} ({account.tag})]({account.share_link})\n")

        embed = ipy.Embed(
            title=f"**{clan.name} ({clan.tag})**",
            thumbnail=ipy.EmbedAttachment(url=clan.badge.url),
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )
        embed.add_field(name="**Helpers**", value=helpers_str if helpers_str else "No helpers there...")

        clan_link_button = ipy.Button(
            style=ipy.ButtonStyle.URL,
            label="Open In-game",
            emoji=CLAN_EMOJI,
            url=clan.share_link
        )

        await ctx.send(embed=embed, components=clan_link_button, ephemeral=hidden)

    account_group = base.group(name="account", description="")

    @account_group.subcommand(sub_cmd_name="link", sub_cmd_description="Link accounts to a helper")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="helper",
        description="The helper to link accounts for",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="tags",
        description="Tags to link, separate them with commas",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    async def helper_account_link(self, ctx: ipy.SlashContext, helper: ipy.Member, tags: str):
        await ctx.defer(ephemeral=True)

        if not helper.has_role(STAFF_ROLE):
            await ctx.send(f"{ERROR_EMOJI} {helper.mention} is not a helper.", ephemeral=True)
            return

        valid_tags = await extract_player_tags(self.coc_client, tags, context=ctx)
        if not valid_tags:
            await ctx.send(f"{ERROR_EMOJI} There are no valid tags in your input.", ephemeral=True)
            return

        account_helper_pairs = await generate_account_helper_pairs()
        helper_doc = await Helper.find_one(Helper.user_id == str(helper.id))
        accounts = helper_doc.accounts if helper_doc else []
        accepted_tags = []
        for tag in valid_tags:
            if tag in accounts or tag in accepted_tags:
                await ctx.send(f"{ERROR_EMOJI} `{tag}` is already linked to {helper.mention}.", ephemeral=True)
                continue

            if tag in account_helper_pairs:
                await ctx.send(f"{ERROR_EMOJI} `{tag}` is already linked to <@{account_helper_pairs[tag]}>.",
                               ephemeral=True)
                continue

            accepted_tags.append(tag)

        if not accepted_tags:
            return
        try:
            if helper_doc:
                await helper_doc.update({"$addToSet": {"accounts": {"$each": accepted_tags}}})
            else:
                await Helper(user_id=str(helper.id), accounts=accepted_tags).insert()
        except (PyMongoError, DocumentNotFound):
            await ctx.send(
                f"{ERROR_EMOJI} The account links could not be saved. Please try again.", ephemeral=True,
            )
            return
        for tag in accepted_tags:
            await ctx.send(f"{SUCCESS_EMOJI} `{tag}` is now linked to {helper.mention}.", ephemeral=True)

    @account_group.subcommand(sub_cmd_name="unlink", sub_cmd_description="Unlink accounts from a helper")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="helper",
        description="The helper to unlink accounts for",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="tag",
        description="The tag to unlink",
        opt_type=ipy.OptionType.STRING,
        required=True,
        autocomplete=True
    )
    async def helper_account_unlink(self, ctx: ipy.SlashContext, helper: ipy.Member, tag: str):
        helper_doc = await Helper.find_one(Helper.user_id == str(helper.id))
        if not helper_doc or tag not in helper_doc.accounts:
            await ctx.send(f"{ERROR_EMOJI} `{tag}` is not linked to {helper.mention}.", ephemeral=True)
            return

        await helper_doc.update({"$pull": {"accounts": tag}})

        await ctx.send(f"{SUCCESS_EMOJI} `{tag}` is removed from {helper.mention}.", ephemeral=True)

    @helper_account_unlink.autocomplete(option_name="tag")
    async def tag_autocomplete(self, ctx: ipy.AutocompleteContext):
        if "helper" not in ctx.kwargs:
            return

        helper_id = str(ctx.kwargs["helper"])
        helper_doc = await Helper.find_one(Helper.user_id == helper_id)
        if not helper_doc or not helper_doc.accounts:
            return

        choices = []
        for tag in helper_doc.accounts:
            try:
                player = await self.coc_client.get_player(tag)
            except coc.errors.NotFound:
                continue

            name = f"{player.name} ({player.tag})"
            choices.append({"name": name, "value": tag})

        await ctx.send(choices)

    @account_group.subcommand(sub_cmd_name="view", sub_cmd_description="View accounts of a helper")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="helper",
        description="The helper to view accounts for",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def helper_account_view(self, ctx: ipy.SlashContext, helper: ipy.Member, hidden: bool = False):
        await ctx.defer(ephemeral=hidden)

        if not helper.has_role(STAFF_ROLE):
            await ctx.send(f"{ERROR_EMOJI} {helper.mention} is not a helper.", ephemeral=True)
            return

        helper_doc = await Helper.find_one(Helper.user_id == str(helper.id))
        if not helper_doc or not helper_doc.accounts:
            await ctx.send(f"{ERROR_EMOJI} There are no accounts linked to {helper.mention}.",
                           ephemeral=True)
            return

        description = ""
        for account_tag in helper_doc.accounts:
            account = await self.coc_client.get_player(account_tag)
            description += (f"{TOWNHALL_EMOJIS[account.town_hall]} "
                            f"[{account.name} ({account.tag})]({account.share_link})\n")

            if not account.clan:
                description += "Not in a clan\n\n"
            else:
                description += f"{account.role} of {account.clan}\n\n"

        embed = ipy.Embed(
            title=f"**{helper}'s Linked Accounts**",
            description=description,
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        await ctx.send(embed=embed, ephemeral=hidden)

    @ipy.component_callback("view_accounts")
    async def view_accounts_button(self, ctx: ipy.ComponentContext):
        ctx.kwargs = {"helper": ctx.author, "hidden": True}
        await self.helper_account_view(ctx)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Helper commands loaded")
