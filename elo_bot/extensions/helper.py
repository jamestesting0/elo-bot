import math
import asyncio
import re
from typing import List, assert_never

import coc
from beanie.exceptions import DocumentNotFound
from pymongo.errors import PyMongoError

import interactions as ipy
from elo_bot.config.emojis import CLAN_EMOJI, ERROR_EMOJI, RANK_EMOJIS, SUCCESS_EMOJI, TOWNHALL_EMOJIS
from elo_bot.config.presentation import FOOTER_TEXT, LB_ENTRIES_PER_PAGE
from elo_bot.discord.embeds import embed_color, footer_image, profile_author
from elo_bot.config.ids import GUILD_IDS, LVL1_ROLES, LVL2_ROLES, STAFF_ROLE
from elo_bot.config.game import HOST_CLAN_CHOICES
from elo_bot.core.finals import FinalError
from elo_bot.db import Helper
from elo_bot.discord.pagination import InvalidPageError, build_page_button, build_page_jump_modal, parse_page_number
from elo_bot.discord.mentions import mention_user
from elo_bot.discord.members import member_guild


HELPER_POINTS_TITLE = "**Helper Points Leaderboard**"
HELPER_HOSTED_TITLE = "**Helper Matches Hosted Leaderboard**"


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
                await context.send(f"{ERROR_EMOJI} `{s}` is invalid.", ephemeral=True)

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


async def _helper_leaderboard_view(guild: ipy.Guild, lb_type: str,
                                   page: str = "1") -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    helpers = await Helper.find_all().to_list()
    if not helpers:
        raise FinalError("There are no helpers.")
    if lb_type == "Points":
        helpers.sort(key=lambda helper: helper.points, reverse=True)
        title = HELPER_POINTS_TITLE
    else:
        helpers.sort(key=lambda helper: sum(0.5 if entry.player2 is None else 1 for entry in helper.history),
                     reverse=True)
        title = HELPER_HOSTED_TITLE
    total_pages = math.ceil(len(helpers) / LB_ENTRIES_PER_PAGE)
    current_page = parse_page_number(page, total_pages)
    offset = (current_page - 1) * LB_ENTRIES_PER_PAGE
    description = await construct_helper_leaderboard_string(guild, helpers, lb_type, offset)
    embed = ipy.Embed(title=title, description=description, color=embed_color(),
                      footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=footer_image()))
    select = ipy.StringSelectMenu(
        ipy.StringSelectOption(label="Helper Points", value="Points", default=lb_type == "Points"),
        ipy.StringSelectOption(label="Matches Hosted", value="Matches Host", default=lb_type == "Matches Host"),
        custom_id="helper_lb_type", placeholder="🔍 Select a leaderboard type",
    )
    controls = ipy.ActionRow(
        ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="◀️️", disabled=current_page == 1,
                   custom_id=f"helper_lb_left|{offset - LB_ENTRIES_PER_PAGE}"),
        build_page_button(current_page, total_pages, "helper_lb_page"),
        ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="▶️", disabled=current_page == total_pages,
                   custom_id=f"helper_lb_right|{offset + LB_ENTRIES_PER_PAGE}"),
    )
    return embed, [ipy.ActionRow(select), controls]


def _helper_leaderboard_type(message: ipy.Message) -> str:
    return "Points" if message.embeds[0].title == HELPER_POINTS_TITLE else "Matches Host"


async def _helper_leaderboard_message(ctx: ipy.ComponentContext | ipy.ModalContext) -> ipy.Message | None:
    message = ctx.message
    if message is None:
        await ctx.send(f"{ERROR_EMOJI} The paginated message is no longer available.", ephemeral=True)
        return None
    if message.interaction.user.id != ctx.author.id:
        await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.", ephemeral=True)
        return None
    return message


async def _helper_view_member(ctx: ipy.SlashContext, helper: ipy.Member | ipy.User | None) -> ipy.Member | None:
    member = helper or ctx.author
    if isinstance(member, ipy.Member) and member.has_role(STAFF_ROLE):
        return member
    if helper is None:
        text = "You’re not a 1v1 helper. Select a helper to view."
    else:
        text = f"{helper.mention} is not a 1v1 helper."
    await ctx.send(f"{ERROR_EMOJI} {text}", ephemeral=True)
    return None


class HelperCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot

    @property
    def coc_client(self) -> coc.Client:
        return self.bot.coc_client

    base = ipy.SlashCommand(name="helper", description="", scopes=GUILD_IDS)

    @base.subcommand(sub_cmd_name="leaderboard", sub_cmd_description="Rank helpers by points or matches hosted")
    @ipy.slash_option(
        name="lb_type",
        description="Type of leaderboard",
        choices=[
            ipy.SlashCommandChoice(name="Helper Points", value="Points"),
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
        try:
            embed, components = await _helper_leaderboard_view(ctx.guild, lb_type)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await ctx.send(embed=embed, components=components, ephemeral=hidden)

    async def _edit_helper_leaderboard(self, ctx: ipy.ComponentContext | ipy.ModalContext,
                                       lb_type: str, page: str = "1") -> None:
        try:
            embed, components = await _helper_leaderboard_view(ctx.guild, lb_type, page)
        except (FinalError, InvalidPageError) as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        if isinstance(ctx, ipy.ModalContext):
            await ctx.edit(ctx.message, embed=embed, components=components)
        else:
            await ctx.edit_origin(embed=embed, components=components)

    @ipy.component_callback("helper_lb_type")
    async def helper_lb_type_select(self, ctx: ipy.ComponentContext):
        if await _helper_leaderboard_message(ctx) is not None:
            await self._edit_helper_leaderboard(ctx, ctx.values[0])

    @ipy.component_callback(re.compile(r"^helper_lb_(left|right)\|-?\d+$"))
    async def helper_lb_arrow(self, ctx: ipy.ComponentContext):
        message = await _helper_leaderboard_message(ctx)
        if message is None:
            return
        _, starting_index = ctx.custom_id.split("|")
        page = int(starting_index) // LB_ENTRIES_PER_PAGE + 1
        await self._edit_helper_leaderboard(ctx, _helper_leaderboard_type(message), str(page))

    @ipy.component_callback("helper_lb_page")
    async def helper_lb_page_button(self, ctx: ipy.ComponentContext):
        if await _helper_leaderboard_message(ctx) is not None:
            await ctx.send_modal(build_page_jump_modal("helper_lb_page_modal"))

    @ipy.modal_callback("helper_lb_page_modal")
    async def helper_lb_page_modal(self, ctx: ipy.ModalContext, page: str):
        message = await _helper_leaderboard_message(ctx)
        if message is not None:
            await self._edit_helper_leaderboard(ctx, _helper_leaderboard_type(message), page)


    @base.subcommand(sub_cmd_name="info", sub_cmd_description="View a helper's hosting information")
    @ipy.slash_option(
        name="helper",
        description="A 1v1 helper",
        opt_type=ipy.OptionType.USER,
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def helper_info(self, ctx: ipy.SlashContext, helper: ipy.Member | ipy.User | None = None, hidden: bool = False):
        await ctx.defer(ephemeral=hidden)

        helper = await _helper_view_member(ctx, helper)
        if helper is None:
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
            title="**Helper Profile**",
            author=profile_author(helper),
            description=description if description else "No matches hosted.",
            fields=[
                ipy.EmbedField(name="**Helper Points**", value=str(points)),
                ipy.EmbedField(name="**Matches Hosted**", value=str(matches_hosted))
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=footer_image()),
            color=embed_color()
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
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=footer_image()),
            color=embed_color()
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
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def helper_account_view(self, ctx: ipy.SlashContext, helper: ipy.Member | ipy.User | None = None,
                                  hidden: bool = False):
        await ctx.defer(ephemeral=hidden)

        helper = await _helper_view_member(ctx, helper)
        if helper is None:
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
            title="**Linked Accounts**",
            author=profile_author(helper),
            description=description,
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=footer_image()),
            color=embed_color()
        )

        await ctx.send(embed=embed, ephemeral=hidden)

    @ipy.component_callback("view_accounts")
    async def view_accounts_button(self, ctx: ipy.ComponentContext):
        ctx.kwargs = {"helper": ctx.author, "hidden": True}
        await self.helper_account_view(ctx)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Helper commands loaded")
