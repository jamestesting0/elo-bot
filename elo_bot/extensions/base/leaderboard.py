import math
import re

import interactions as ipy

import elo_bot.extensions.base.groups as groups
from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.config.game import DISTRICT_CHOICES
from elo_bot.config.presentation import (
    COLOR,
    FOOTER_IMAGE,
    FOOTER_TEXT,
    LB_ENTRIES_PER_PAGE,
)
from elo_bot.db import BaseLayout
from elo_bot.discord.pagination import InvalidPageError, build_page_button, build_page_jump_modal, parse_page_number
from elo_bot.extensions.base.groups import BASE_QUALITY_CHOICES
from elo_bot.features.bases.layouts.queries import sort_bases
from elo_bot.features.bases.layouts.ui import _parse_base_lb_title, construct_base_leaderboard_string


class BaseLeaderboardCommands:
    base = groups.base

    @base.subcommand(sub_cmd_name="leaderboard", sub_cmd_description="The leaderboard of top base builders")
    @ipy.slash_option(
        name="district",
        description="The base is for which district?",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="quality",
        description="Filter by base quality",
        opt_type=ipy.OptionType.STRING,
        choices=BASE_QUALITY_CHOICES,
        required=False
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def base_leaderboard(self, ctx: ipy.SlashContext, district: str, quality: str = "Excellent", hidden: bool = False):
        await ctx.defer(ephemeral=hidden)

        bases = BaseLayout.find(BaseLayout.district == district, BaseLayout.quality == quality)
        bases_sorted = await sort_bases(bases)

        leaderboard = await construct_base_leaderboard_string(self.bot, bases_sorted)

        district_type_select = ipy.StringSelectMenu(
            ipy.StringSelectOption(label="Capital Peak", value="Capital Peak"),
            ipy.StringSelectOption(label="Wizard Valley", value="Wizard Valley"),
            ipy.StringSelectOption(label="Barbarian Camp", value="Barbarian Camp"),
            ipy.StringSelectOption(label="Balloon Lagoon", value="Balloon Lagoon"),
            ipy.StringSelectOption(label="Builder's Workshop", value="Builder's Workshop"),
            ipy.StringSelectOption(label="Dragon Cliffs", value="Dragon Cliffs"),
            ipy.StringSelectOption(label="Golem Quarry", value="Golem Quarry"),
            ipy.StringSelectOption(label="Skeleton Park", value="Skeleton Park"),
            ipy.StringSelectOption(label="Goblin Mines", value="Goblin Mines"),
            placeholder="📊 Select the district type",
            custom_id="district_type_select"
        )

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"base_lb_left|{0 - LB_ENTRIES_PER_PAGE}",
            disabled=True,
        )

        total_pages = math.ceil(len(bases_sorted) / LB_ENTRIES_PER_PAGE)
        current_page = int(0 / LB_ENTRIES_PER_PAGE) + 1
        page_number_button = build_page_button(current_page, total_pages, "base_lb_page")

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"base_lb_right|{0 + LB_ENTRIES_PER_PAGE}",
            disabled=(0 + LB_ENTRIES_PER_PAGE) >= len(bases_sorted)
        )

        base_quality_select = ipy.StringSelectMenu(
            ipy.StringSelectOption(label="Average", value="Average"),
            ipy.StringSelectOption(label="Good", value="Good"),
            ipy.StringSelectOption(label="Great", value="Great"),
            ipy.StringSelectOption(label="Excellent", value="Excellent"),
            placeholder="🏷️ Select the base quality",
            custom_id="base_quality_select"
        )

        components = [
            ipy.ActionRow(district_type_select),
            ipy.ActionRow(base_quality_select),
            ipy.ActionRow(left_button, page_number_button, right_button)
        ]

        embed = ipy.Embed(
            title=f"**Best Performing {district} Bases ({quality})**",
            description=leaderboard if leaderboard else "Not enough data.",
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        await ctx.send(embed=embed, components=components, ephemeral=hidden)

    @ipy.component_callback(re.compile(r"base_lb_right\|.*"))
    async def base_lb_right_button(self, ctx: ipy.ComponentContext):
        original_user = ctx.message.interaction.user.id if ctx.message.interaction else ctx.author.id
        if str(original_user) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)

            return

        district, quality = _parse_base_lb_title(ctx.message.embeds[0].title)
        _, starting_index = ctx.custom_id.split("|")
        starting_index = int(starting_index)

        bases_sorted = await sort_bases(BaseLayout.find(BaseLayout.district == district, BaseLayout.quality == quality))
        if not bases_sorted:
            await ctx.send(f"There are no bases submitted for **{district}**...", ephemeral=True)
            return

        leaderboard = await construct_base_leaderboard_string(self.bot, bases_sorted, starting_index)
        ctx.message.embeds[0].description = leaderboard

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"base_lb_left|{starting_index - LB_ENTRIES_PER_PAGE}",
            disabled=False,
        )

        total_pages = math.ceil(len(bases_sorted) / LB_ENTRIES_PER_PAGE)
        current_page = int(starting_index / LB_ENTRIES_PER_PAGE) + 1
        page_number_button = build_page_button(current_page, total_pages, "base_lb_page")

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"base_lb_right|{starting_index + LB_ENTRIES_PER_PAGE}",
            disabled=(starting_index + LB_ENTRIES_PER_PAGE) >= len(bases_sorted)
        )

        ctx.message.components[2] = ipy.ActionRow(left_button, page_number_button, right_button)

        await ctx.edit_origin(embeds=ctx.message.embeds, components=ctx.message.components)

    @ipy.component_callback(re.compile(r"base_lb_left\|.*"))
    async def base_lb_left_button(self, ctx: ipy.ComponentContext):
        original_user = ctx.message.interaction.user.id if ctx.message.interaction else ctx.author.id
        if str(original_user) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)

            return

        district, quality = _parse_base_lb_title(ctx.message.embeds[0].title)
        _, starting_index = ctx.custom_id.split("|")
        starting_index = int(starting_index)

        bases_sorted = await sort_bases(BaseLayout.find(BaseLayout.district == district, BaseLayout.quality == quality))
        if not bases_sorted:
            await ctx.send(f"There are no bases submitted for **{district}**...", ephemeral=True)
            return

        leaderboard = await construct_base_leaderboard_string(self.bot, bases_sorted, starting_index)
        ctx.message.embeds[0].description = leaderboard

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"base_lb_left|{starting_index - LB_ENTRIES_PER_PAGE}",
            disabled=(starting_index - LB_ENTRIES_PER_PAGE) < 0,
        )

        total_pages = math.ceil(len(bases_sorted) / LB_ENTRIES_PER_PAGE)
        current_page = int(starting_index / LB_ENTRIES_PER_PAGE) + 1
        page_number_button = build_page_button(current_page, total_pages, "base_lb_page")

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"base_lb_right|{starting_index + LB_ENTRIES_PER_PAGE}",
            disabled=False
        )

        ctx.message.components[2] = ipy.ActionRow(left_button, page_number_button, right_button)

        await ctx.edit_origin(embeds=ctx.message.embeds, components=ctx.message.components)

    @ipy.component_callback("district_type_select")
    async def district_type_select(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        original_user = ctx.message.interaction.user.id if ctx.message.interaction else ctx.author.id
        if str(original_user) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)

            return

        district = ctx.values[0]
        _, quality = _parse_base_lb_title(ctx.message.embeds[0].title)

        bases_sorted = await sort_bases(BaseLayout.find(BaseLayout.district == district, BaseLayout.quality == quality))
        if not bases_sorted:
            await ctx.send(f"There are not bases submitted for **{district}**...", ephemeral=True)
            return

        leaderboard = await construct_base_leaderboard_string(self.bot, bases_sorted)

        ctx.message.embeds[0].title = f"**Best Performing {district} Bases ({quality})**"
        ctx.message.embeds[0].description = leaderboard

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"base_lb_left|{0 - LB_ENTRIES_PER_PAGE}",
            disabled=True,
        )

        total_pages = math.ceil(len(bases_sorted) / LB_ENTRIES_PER_PAGE)
        current_page = int(0 / LB_ENTRIES_PER_PAGE) + 1
        page_number_button = build_page_button(current_page, total_pages, "base_lb_page")

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"base_lb_right|{0 + LB_ENTRIES_PER_PAGE}",
            disabled=(0 + LB_ENTRIES_PER_PAGE) >= len(bases_sorted)
        )

        ctx.message.components[2] = ipy.ActionRow(left_button, page_number_button, right_button)

        await ctx.edit(ctx.message, embeds=ctx.message.embeds, components=ctx.message.components)

        await ctx.send(f"{SUCCESS_EMOJI} The district type is changed to `{district}`.", ephemeral=True)

    @ipy.component_callback("base_quality_select")
    async def base_quality_select(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        original_user = ctx.message.interaction.user.id if ctx.message.interaction else ctx.author.id
        if str(original_user) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)

            return

        district, _ = _parse_base_lb_title(ctx.message.embeds[0].title)
        quality = ctx.values[0]

        bases_sorted = await sort_bases(BaseLayout.find(BaseLayout.district == district, BaseLayout.quality == quality))
        if not bases_sorted:
            await ctx.send(f"There are no bases submitted for **{district}** with quality **{quality}**...", ephemeral=True)
            return

        leaderboard = await construct_base_leaderboard_string(self.bot, bases_sorted)

        ctx.message.embeds[0].title = f"**Best Performing {district} Bases ({quality})**"
        ctx.message.embeds[0].description = leaderboard

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"base_lb_left|{0 - LB_ENTRIES_PER_PAGE}",
            disabled=True,
        )

        total_pages = math.ceil(len(bases_sorted) / LB_ENTRIES_PER_PAGE)
        current_page = int(0 / LB_ENTRIES_PER_PAGE) + 1
        page_number_button = build_page_button(current_page, total_pages, "base_lb_page")

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"base_lb_right|{0 + LB_ENTRIES_PER_PAGE}",
            disabled=(0 + LB_ENTRIES_PER_PAGE) >= len(bases_sorted)
        )

        ctx.message.components[2] = ipy.ActionRow(left_button, page_number_button, right_button)

        await ctx.edit(ctx.message, embeds=ctx.message.embeds, components=ctx.message.components)

        await ctx.send(f"{SUCCESS_EMOJI} The base quality is changed to `{quality}`.", ephemeral=True)

    @ipy.component_callback("base_lb_page")
    async def base_lb_page_button(self, ctx: ipy.ComponentContext):
        original_user = ctx.message.interaction.user.id if ctx.message.interaction else ctx.author.id
        if str(original_user) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)
            return

        await ctx.send_modal(build_page_jump_modal("base_lb_page_modal"))

    @ipy.modal_callback("base_lb_page_modal")
    async def base_lb_page_modal(self, ctx: ipy.ModalContext, **response):
        message = ctx.message
        if message is None:
            await ctx.send(f"{ERROR_EMOJI} The paginated message is no longer available.", ephemeral=True)
            return

        original_user = message.interaction.user.id if message.interaction else ctx.author.id
        if str(original_user) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)
            return

        district, quality = _parse_base_lb_title(message.embeds[0].title)
        bases_sorted = await sort_bases(
            BaseLayout.find(BaseLayout.district == district, BaseLayout.quality == quality)
        )
        if not bases_sorted:
            await ctx.send(f"{ERROR_EMOJI} There are no bases submitted for **{district}** anymore.",
                           ephemeral=True)
            return

        total_pages = math.ceil(len(bases_sorted) / LB_ENTRIES_PER_PAGE)
        try:
            page = parse_page_number(response["page"], total_pages)
        except InvalidPageError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return

        starting_index = (page - 1) * LB_ENTRIES_PER_PAGE
        leaderboard = await construct_base_leaderboard_string(self.bot, bases_sorted, starting_index)
        message.embeds[0].description = leaderboard

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"base_lb_left|{starting_index - LB_ENTRIES_PER_PAGE}",
            disabled=starting_index == 0,
        )
        page_number_button = build_page_button(page, total_pages, "base_lb_page")
        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"base_lb_right|{starting_index + LB_ENTRIES_PER_PAGE}",
            disabled=(starting_index + LB_ENTRIES_PER_PAGE) >= len(bases_sorted),
        )

        message.components[2] = ipy.ActionRow(left_button, page_number_button, right_button)
        await ctx.edit(message, embeds=message.embeds, components=message.components)
