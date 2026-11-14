import difflib
import logging
import re
from time import time as unix_time
from typing import assert_never

import interactions as ipy
from pymongo.errors import PyMongoError

from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT, LB_ENTRIES_PER_PAGE
from elo_bot.config.game import DISTRICT_CHOICES
from elo_bot.config.ids import GUILD_IDS, LVL1_ROLES
from elo_bot.config.emojis import RANK_EMOJIS, SUCCESS_EMOJI
from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.features.players.data_boundary import PLAYER_DATA_BOUNDARY, PlayerDataReset
from elo_bot.db import BaseLayout, Season, Player, DistrictSeasonData, PlayerSeasonData
from elo_bot.discord.pagination import InvalidPageError, build_page_button, build_page_jump_modal, parse_page_number
from elo_bot.features.seasons.presentation import stats_controls, stats_embed
from elo_bot.features.seasons.stats import SeasonStatsError, SeasonWindow, season_windows
from elo_bot.features.seasons.stats_data import load_season_stats
import math
from datetime import datetime
from elo_bot.core.parsing import extract_integer
from elo_bot.discord.mentions import mention_user
from elo_bot.features.bases.layouts.queries import sort_bases


_stats_logger = logging.getLogger(__name__)


async def construct_season_leaderboard_string(bot, district_data_list, starting_index=0):
    leaderboard_str = ""
    # Slice the list
    current_page_data = district_data_list[starting_index:starting_index + LB_ENTRIES_PER_PAGE]

    for index, data in enumerate(current_page_data):
        rank_index = starting_index + index
        if rank_index > 2:
            emoji = f"**#{rank_index + 1}**"
        else:
            emoji = RANK_EMOJIS[rank_index]

        if not data.builder_id:
            builder_mention = mention_user(None, data.builder_id, mention_type="Builder")
        else:
            builder = await bot.fetch_user(data.builder_id)
            builder_mention = mention_user(builder)

        leaderboard_str += (f"{emoji} {builder_mention} `{data.attacks}` → "
                            f"`{data.average_stars:.2f}⭐ {data.average_destruction:.2f}%`\n")
    return leaderboard_str


class SeasonCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot

    base = ipy.SlashCommand(name="season", description="", scopes=GUILD_IDS)

    async def _stats_windows(self) -> list[SeasonWindow]:
        read_time = int(unix_time())
        seasons = await Season.find_all().sort("-season_number").to_list()
        return season_windows(seasons, read_time)

    async def _stats_embed(self, window: SeasonWindow) -> ipy.Embed:
        stats = await load_season_stats(window)
        return stats_embed(window, stats)

    @base.subcommand(sub_cmd_name="stats", sub_cmd_description="View activity of each season")
    @ipy.slash_option(
        name="hidden", description="Make the bot response hidden or not", opt_type=ipy.OptionType.BOOLEAN,
    )
    async def season_stats(self, ctx: ipy.SlashContext, hidden: bool = False) -> None:
        await ctx.defer(ephemeral=hidden)
        try:
            windows = await self._stats_windows()
            embed = await self._stats_embed(windows[0])
        except (PyMongoError, SeasonStatsError):
            _stats_logger.exception("Unable to load initial season stats")
            await ctx.send(f"{ERROR_EMOJI} Unable to load season stats. Please try again.", ephemeral=hidden)
            return
        await ctx.send(embed=embed, components=stats_controls(windows, 0), ephemeral=hidden,
                       allowed_mentions=ipy.AllowedMentions.none())

    async def _stats_error(self, ctx: ipy.ComponentContext | ipy.ModalContext, message: str) -> None:
        await ctx.client.http.post_followup(
            {"content": f"{ERROR_EMOJI} {message}", "flags": int(ipy.MessageFlags.EPHEMERAL)},
            ctx.client.app.id, ctx.token,
        )

    async def _stats_source(self, ctx: ipy.ComponentContext | ipy.ModalContext) -> str | None:
        message = ctx.message
        if message is None or message.interaction is None:
            await ctx.send(f"{ERROR_EMOJI} The paginated message is no longer available.", ephemeral=True)
            return None
        if str(message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.", ephemeral=True)
            return None
        key = ctx.custom_id.partition("|")[2]
        valid_key = re.fullmatch(r"current|[0-9]+", key)
        title = f"**{SeasonWindow(key, None, 0).label} Stats**"
        if (not valid_key or not message.embeds or message.embeds[0].title != title
                or not message.components or len(message.components[0].components) < 2
                or message.components[0].components[1].custom_id != f"season_stats_page|{key}"):
            await ctx.send(f"{ERROR_EMOJI} This page has changed. Use its current controls.", ephemeral=True)
            return None
        return key

    async def _stats_update(self, ctx: ipy.ComponentContext | ipy.ModalContext, page: str | None = None) -> None:
        key = await self._stats_source(ctx)
        if key is None:
            return
        await ctx.defer(edit_origin=True)
        try:
            windows = await self._stats_windows()
            index = next((i for i, window in enumerate(windows) if window.key == key), None)
            if index is None:
                raise SeasonStatsError("The season is no longer available.")
            if page is not None:
                old_total = ctx.message.components[0].components[1].label.rsplit("/", 1)[-1]
                if old_total != str(len(windows)):
                    raise SeasonStatsError("The season list changed. Open the page selector again.")
                index = parse_page_number(page, len(windows)) - 1
            else:
                index += -1 if ctx.custom_id.startswith("season_stats_left|") else 1
                if not 0 <= index < len(windows):
                    raise SeasonStatsError("No season is available in that direction.")
            embed = await self._stats_embed(windows[index])
            components = stats_controls(windows, index)
        except (InvalidPageError, SeasonStatsError) as error:
            await self._stats_error(ctx, str(error))
            return
        except PyMongoError:
            _stats_logger.exception("Unable to load season stats page")
            await self._stats_error(ctx, "Unable to load season stats. Please try again.")
            return
        try:
            match ctx:
                case ipy.ModalContext():
                    await ctx.edit(ctx.message, embed=embed, components=components,
                                   allowed_mentions=ipy.AllowedMentions.none())
                case ipy.ComponentContext():
                    await ctx.edit_origin(embed=embed, components=components,
                                          allowed_mentions=ipy.AllowedMentions.none())
                case unreachable:
                    assert_never(unreachable)
        except ipy.errors.HTTPException:
            _stats_logger.exception("Unable to publish season stats page")
            await self._stats_error(ctx, "Unable to update the season stats message. Please try again.")

    @ipy.component_callback(re.compile(r"^season_stats_(left|right)\|(current|[0-9]+)$"))
    async def season_stats_navigation(self, ctx: ipy.ComponentContext) -> None:
        await self._stats_update(ctx)

    @ipy.component_callback(re.compile(r"^season_stats_page\|(current|[0-9]+)$"))
    async def season_stats_page_button(self, ctx: ipy.ComponentContext) -> None:
        key = await self._stats_source(ctx)
        if key is not None:
            await ctx.send_modal(build_page_jump_modal(f"season_stats_page_modal|{key}"))

    @ipy.modal_callback(re.compile(r"^season_stats_page_modal\|(current|[0-9]+)$"))
    async def season_stats_page_modal(self, ctx: ipy.ModalContext, **response: str) -> None:
        await self._stats_update(ctx, response.get("page", ""))

    @base.subcommand(sub_cmd_name="reset", sub_cmd_description="Reset the season")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="start_date",
        description="Discord/Unix timestamp of the start date",
        opt_type=ipy.OptionType.STRING,
        required=False
    )
    @ipy.slash_option(
        name="end_date",
        description="Discord/Unix timestamp of the end date",
        opt_type=ipy.OptionType.STRING,
        required=False
    )
    async def season_reset(self, ctx: ipy.SlashContext, start_date: str = None, end_date: str = None):
        await ctx.defer(ephemeral=True)

        try:
            async with PLAYER_DATA_BOUNDARY.reset():
                # Get last season to determine start date if not provided
                last_season = await Season.find_all().sort("-season_number").first_or_none()

                if not start_date:
                    start_date = last_season.end if last_season else int(datetime.now().timestamp())

                if not end_date:
                    end_date = int(datetime.now().timestamp())

                start_date = extract_integer(start_date)
                end_date = extract_integer(end_date)

                if not start_date or not end_date:
                    await ctx.send(f"{ERROR_EMOJI} The timestamps you provided are not valid.", ephemeral=True)

                    return

                if start_date > 32536799999 or end_date > 32536799999:
                    await ctx.send(f"{ERROR_EMOJI} The given timestamps are out of range.", ephemeral=True)

                    return

                # process players
                players = await Player.find_all().to_list()
                player_season_data_list = []

                for player in players:
                    # store current ELO
                    player_season_data_list.append(PlayerSeasonData(
                        player_id=player.user_id,
                        elo=player.elo
                    ))

                    if player.highest_elo >= 1200:
                        anchor = 1150
                    elif player.highest_elo > 1150:
                        anchor = 1050
                    else:
                        anchor = 950

                    decay_rate = 0.6

                    if player.elo > 1300:
                        anchor = 1250

                    while player.elo < anchor and anchor > 950:
                        anchor -= 100

                    if player.elo > anchor:
                        new_elo = int(anchor + (player.elo - anchor) * decay_rate)
                        new_elo = min(new_elo, player.elo)
                        new_elo = min(new_elo, 1400)  # post-reset elo capped at 1400
                        player.elo = new_elo

                        await player.save_changes()

                    if player.elo < 950:
                        player.elo = 950

                        await player.save_changes()

                # create new season
                new_season_number = (last_season.season_number + 1) if last_season else 1
                new_season = Season(
                    season_number=new_season_number,
                    start=start_date,
                    end=end_date,
                    players=player_season_data_list,
                    districts={}
                )
                await new_season.insert()

                leaderboard_ext = self.bot.get_ext("LeaderboardCommands")
                if leaderboard_ext:
                    await leaderboard_ext.refresh_cache()

                await ctx.send(f"{SUCCESS_EMOJI} All player's elo has been reset and season {new_season_number} is recorded!",
                               ephemeral=True)
        except PlayerDataReset as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.global_autocomplete(option_name="season")
    async def season_autocomplete(self, ctx: ipy.AutocompleteContext):
        seasons = await Season.find_all().sort("season_number").to_list()
        choices = {}
        leaderboard_type = ctx.kwargs.get("lb_type", "Elo") if str(ctx.command.name) == "leaderboard" else None
        if str(ctx.command.name) != "season":
            if leaderboard_type not in {"Elo", "Elo Reversed", "Highest Elo", "Tokens"}:
                choices["all seasons"] = {"name": "All Seasons", "value": "All Seasons"}
            choices["current"] = {"name": "Current Season", "value": "Current Season"}

        if leaderboard_type not in {"Highest Elo", "Tokens"}:
            for season in seasons:
                choices[f"season {season.season_number}"] = {
                    "name": f"Season {season.season_number}", "value": season.season_number
                }

        user_input = ctx.input_text
        if not user_input:
            return await ctx.send(list(choices.values())[:25])

        returns = difflib.get_close_matches(
            user_input.lower(),
            choices.keys(),
            n=25,
            cutoff=0.10,
        )

        results = []
        for item in returns:
            results.append(choices[str(item)])

        await ctx.send(results)

    base_group = base.group(name="base", description="")

    @base_group.subcommand(sub_cmd_name="record", sub_cmd_description="Record base leaderboard of a district")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="district",
        description="The district of the base",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES,
        required=True
    )
    async def season_base_record(self, ctx: ipy.SlashContext, district: str):
        current_season = await Season.find_all().sort("-season_number").first_or_none()
        if not current_season:
            await ctx.send(f"{ERROR_EMOJI} No active season found.", ephemeral=True)

            return

        # fetch bases for the district, excluding Private ones
        query = BaseLayout.find(BaseLayout.district == district, BaseLayout.quality != "Private")
        bases_sorted = await sort_bases(query)

        # calculate stats for top bases (e.g., top 10)
        top_bases = bases_sorted[:10]
        district_data_list = []

        for base in top_bases:
            entries = len(base.records)
            if entries > 0:
                avg_stars = sum(record[1] for record in base.records) / entries
                avg_destruction = sum(record[2] for record in base.records) / entries

                builder_id = base.builder_id if base.builder_id else "Unknown"

                district_data_list.append(DistrictSeasonData(
                    builder_id=builder_id,
                    attacks=entries,
                    average_stars=round(avg_stars, 2),
                    average_destruction=round(avg_destruction, 2)
                ))

        current_season.districts[district] = district_data_list

        await current_season.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} Successfully recorded the base leaderboard for **{district}**.",
                       ephemeral=True)

    @base_group.subcommand(sub_cmd_name="leaderboard", sub_cmd_description="View the base leaderboards of a season")
    @ipy.slash_option(
        name="season",
        description="The season to check",
        opt_type=ipy.OptionType.INTEGER,
        autocomplete=True,
        min_value=0,
        required=True
    )
    @ipy.slash_option(
        name="district",
        description="The district of the base",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def season_base_leaderboard(self, ctx: ipy.SlashContext, season: int, district: str, hidden: bool = False):
        await ctx.defer(ephemeral=hidden)

        season_doc = await Season.find_one(Season.season_number == season)

        if not season_doc:
            await ctx.send(f"{ERROR_EMOJI} The season is not valid!", ephemeral=True)

            return

        if district not in season_doc.districts:
            await ctx.send(f"{ERROR_EMOJI} There are not enough data for this leaderboard.", ephemeral=True)

            return

        district_data_list = season_doc.districts[district]

        # Reconstruct leaderboard string
        leaderboard_str = await construct_season_leaderboard_string(self.bot, district_data_list)

        start_date = ipy.Timestamp.fromtimestamp(season_doc.start).format(style=ipy.TimestampStyles.LongDateTime)
        end_date = ipy.Timestamp.fromtimestamp(season_doc.end).format(style=ipy.TimestampStyles.LongDateTime)

        embed = ipy.Embed(
            title=f"**Season {season_doc.season_number} Best Performing {district} Bases**",
            description=f"Start: {start_date}\n"
                        f"End: {end_date}\n\n"
                        f"{leaderboard_str}",
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"season_lb_left|{0 - LB_ENTRIES_PER_PAGE}",
            disabled=True,
        )

        total_pages = math.ceil(len(district_data_list) / LB_ENTRIES_PER_PAGE)
        current_page = 1
        page_number_button = build_page_button(current_page, total_pages, "season_lb_page")

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"season_lb_right|{0 + LB_ENTRIES_PER_PAGE}",
            disabled=(0 + LB_ENTRIES_PER_PAGE) >= len(district_data_list)
        )

        components = [ipy.ActionRow(left_button, page_number_button, right_button)]

        await ctx.send(embed=embed, components=components, ephemeral=hidden)

    @ipy.component_callback(re.compile(r"season_lb_right\|.*"))
    async def season_lb_right_button(self, ctx: ipy.ComponentContext):
        if str(ctx.message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)
            return

        district = ctx.message.embeds[0].title.replace("**", "").replace(" Best Performing ", "|")
        district = district.replace(" Bases", "").split("|")[1]
        season_num = int(ctx.message.embeds[0].title.split("Season ")[1].split(" ")[0])

        _, starting_index = ctx.custom_id.split("|")
        starting_index = int(starting_index)

        season_doc = await Season.find_one(Season.season_number == season_num)
        district_data_list = season_doc.districts[district]

        leaderboard_str = await construct_season_leaderboard_string(self.bot, district_data_list, starting_index)

        header = ctx.message.embeds[0].description.split("\n\n")[0]
        ctx.message.embeds[0].description = f"{header}\n\n{leaderboard_str}"

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"season_lb_left|{starting_index - LB_ENTRIES_PER_PAGE}",
            disabled=False,
        )

        total_pages = math.ceil(len(district_data_list) / LB_ENTRIES_PER_PAGE)
        current_page = int(starting_index / LB_ENTRIES_PER_PAGE) + 1
        page_number_button = build_page_button(current_page, total_pages, "season_lb_page")

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"season_lb_right|{starting_index + LB_ENTRIES_PER_PAGE}",
            disabled=(starting_index + LB_ENTRIES_PER_PAGE) >= len(district_data_list)
        )

        ctx.message.components[0] = ipy.ActionRow(left_button, page_number_button, right_button)

        await ctx.edit_origin(embeds=ctx.message.embeds, components=ctx.message.components)

    @ipy.component_callback(re.compile(r"season_lb_left\|.*"))
    async def season_lb_left_button(self, ctx: ipy.ComponentContext):
        if str(ctx.message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)
            return

        district = ctx.message.embeds[0].title.replace("**", "").replace(" Best Performing ", "|")
        district = district.replace(" Bases", "").split("|")[1]
        season_num = int(ctx.message.embeds[0].title.split("Season ")[1].split(" ")[0])

        _, starting_index = ctx.custom_id.split("|")
        starting_index = int(starting_index)

        season_doc = await Season.find_one(Season.season_number == season_num)
        district_data_list = season_doc.districts[district]

        leaderboard_str = await construct_season_leaderboard_string(self.bot, district_data_list, starting_index)

        header = ctx.message.embeds[0].description.split("\n\n")[0]
        ctx.message.embeds[0].description = f"{header}\n\n{leaderboard_str}"

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"season_lb_left|{starting_index - LB_ENTRIES_PER_PAGE}",
            disabled=(starting_index - LB_ENTRIES_PER_PAGE) < 0,
        )

        total_pages = math.ceil(len(district_data_list) / LB_ENTRIES_PER_PAGE)
        current_page = int(starting_index / LB_ENTRIES_PER_PAGE) + 1
        page_number_button = build_page_button(current_page, total_pages, "season_lb_page")

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"season_lb_right|{starting_index + LB_ENTRIES_PER_PAGE}",
            disabled=False
        )

        ctx.message.components[0] = ipy.ActionRow(left_button, page_number_button, right_button)

        await ctx.edit_origin(embeds=ctx.message.embeds, components=ctx.message.components)

    @ipy.component_callback("season_lb_page")
    async def season_lb_page_button(self, ctx: ipy.ComponentContext):
        if str(ctx.message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)
            return

        await ctx.send_modal(build_page_jump_modal("season_lb_page_modal"))

    @ipy.modal_callback("season_lb_page_modal")
    async def season_lb_page_modal(self, ctx: ipy.ModalContext, **response):
        message = ctx.message
        if message is None:
            await ctx.send(f"{ERROR_EMOJI} The paginated message is no longer available.", ephemeral=True)
            return

        if str(message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)
            return

        district = message.embeds[0].title.replace("**", "").replace(" Best Performing ", "|")
        district = district.replace(" Bases", "").split("|")[1]
        season_num = int(message.embeds[0].title.split("Season ")[1].split(" ")[0])
        season_doc = await Season.find_one(Season.season_number == season_num)
        if not season_doc:
            await ctx.send(f"{ERROR_EMOJI} The season is no longer available.", ephemeral=True)
            return
        if district not in season_doc.districts or not season_doc.districts[district]:
            await ctx.send(f"{ERROR_EMOJI} There are not enough data for this leaderboard.", ephemeral=True)
            return

        district_data_list = season_doc.districts[district]
        total_pages = math.ceil(len(district_data_list) / LB_ENTRIES_PER_PAGE)
        try:
            page = parse_page_number(response["page"], total_pages)
        except InvalidPageError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return

        starting_index = (page - 1) * LB_ENTRIES_PER_PAGE
        leaderboard = await construct_season_leaderboard_string(
            self.bot, district_data_list, starting_index
        )
        header = message.embeds[0].description.split("\n\n")[0]
        message.embeds[0].description = f"{header}\n\n{leaderboard}"

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"season_lb_left|{starting_index - LB_ENTRIES_PER_PAGE}",
            disabled=starting_index == 0,
        )
        page_number_button = build_page_button(page, total_pages, "season_lb_page")
        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"season_lb_right|{starting_index + LB_ENTRIES_PER_PAGE}",
            disabled=(starting_index + LB_ENTRIES_PER_PAGE) >= len(district_data_list),
        )

        message.components[0] = ipy.ActionRow(left_button, page_number_button, right_button)
        await ctx.edit(message, embeds=message.embeds, components=message.components)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Season commands loaded")
