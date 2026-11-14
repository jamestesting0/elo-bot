import asyncio
import math
import re
import traceback
from copy import copy
from datetime import datetime

import interactions as ipy
from elobot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT, LB_ENTRIES_PER_PAGE, LB_TYPE_OPTIONS
from elobot.config.emojis import ERROR_EMOJI, RANK_EMOJIS, RANK_REVERSED_EMOJIS, SUCCESS_EMOJI
from elobot.config.ids import GUILD_IDS
from elobot.db import RecordedMatch, Player, Season
from elobot.discord.pagination import InvalidPageError, build_page_button, build_page_jump_modal, parse_page_number
from elobot.features.seasons.selection import get_season_by_number, interpret_season_string
from elobot.discord.mentions import mention_user
from elobot.features.leaderboards.stats import get_avg_destruction
from elobot.features.leaderboards.stats import get_avg_stars
from elobot.features.leaderboards.stats import get_district_avg_destruction
from elobot.features.leaderboards.stats import get_district_avg_stars
from elobot.features.leaderboards.stats import get_district_hitrate
from elobot.features.leaderboards.stats import get_district_matches
from elobot.features.leaderboards.stats import get_hitrate
from elobot.features.leaderboards.stats import get_season_matches
from elobot.features.leaderboards.stats import get_winrate
from elobot.features.finals.leaderboards import final_board_text, final_board_rows


def _selected_options(options, selected):
    result = [copy(option) for option in options]
    for option in result:
        option.default = option.value == selected
    return result


_HITRATE_TIERS = ("Overall", "Average", "Good", "Great", "Excellent")


def _is_hitrate(lb_type: str) -> bool:
    return lb_type == "Hitrate" or lb_type.endswith(" Hitrate")


def _view_tier(message) -> str:
    custom_id = message.components[0].components[0].custom_id
    if isinstance(custom_id, str) and "|" in custom_id:
        tier = custom_id.split("|", 1)[1]
        if tier in _HITRATE_TIERS:
            return tier
    return "Overall"


def _tier_menu(tier: str) -> ipy.ActionRow:
    return ipy.ActionRow(ipy.StringSelectMenu(
        *(ipy.StringSelectOption(label=value, value=value, default=value == tier) for value in _HITRATE_TIERS),
        placeholder="Select base quality",
        custom_id="lb_tier_select",
    ))


def _ranking_type(lb_type: str, tier: str) -> str:
    return f"{lb_type}|{tier}" if _is_hitrate(lb_type) and tier != "Overall" else lb_type


async def _board_text(guild, players, lb_type, season_start, season_end, starting_index=0, *, paged=False,
                      tier="Overall"):
    if not players:
        return "No eligible players in this leaderboard."
    if _is_hitrate(lb_type) and tier != "Overall":
        return await construct_leaderboard_string(
            guild, players, lb_type, season_start, season_end, starting_index, paged=paged, tier=tier
        )
    return await construct_leaderboard_string(
        guild, players, lb_type, season_start, season_end, starting_index, paged=paged
    )


def _normalize_season(lb_type: str, season: str | None) -> str:
    if lb_type in {"Highest Elo", "Tokens"} or season == "Lifetime":
        return "Current Season"
    if lb_type in {"Elo", "Elo Reversed"} and season == "All Seasons":
        return "Current Season"
    return "Current Season" if season is None else season


def _scope_window(lb_type: str, season: str, seasons: list[Season]) -> tuple[str, int | float, int | float]:
    if lb_type in {"Tokens", "Predictions"}:
        return (f"Season {season} Final" if season.isdigit() else season), 0, 0
    return interpret_season_string(seasons, season)


def _season_options(lb_type: str, seasons: list[Season], selected: str) -> list[ipy.StringSelectOption]:
    options = []
    if lb_type not in {"Elo", "Elo Reversed"}:
        options.append(ipy.StringSelectOption(label="All Seasons", value="All Seasons"))
    options.append(ipy.StringSelectOption(label="Current Season", value="Current Season"))
    options.extend(
        ipy.StringSelectOption(label=f"Season {season.season_number}", value=str(season.season_number))
        for season in seasons
    )
    if lb_type == "Predictions" and selected.isdigit() and not any(option.value == selected for option in options):
        options.append(ipy.StringSelectOption(label=f"Season {selected}", value=selected))
    return options


def _rank_match_board(players: list[Player], lb_type: str,
                      season_matches: dict[int, list[RecordedMatch]]) -> list:
    if lb_type == "Addiction":
        return sorted(players, key=lambda player: len(season_matches[id(player)]), reverse=True)
    if lb_type == "Winrate":
        eligible = [player for player in players if len(season_matches[id(player)]) >= 5]
        return sorted(
            eligible,
            key=lambda player: get_winrate(season_matches[id(player)], player.user_id),
            reverse=True,
        )
    if lb_type == "Hitrate":
        eligible = [player for player in players if len(season_matches[id(player)]) >= 5]
        return sorted(
            eligible,
            key=lambda player: get_hitrate(season_matches[id(player)], player.user_id),
            reverse=True,
        )

    district = lb_type.replace(" Hitrate", "")
    district_matches = {
        id(player): get_district_matches(season_matches[id(player)], district, player.user_id)
        for player in players
    }
    eligible = [player for player in players if len(district_matches[id(player)]) >= 5]
    return sorted(
        eligible,
        key=lambda player: get_district_hitrate(district_matches[id(player)], district, player.user_id),
        reverse=True,
    )


async def sort_players(players: list[Player], lb_type: str,
                       season_start: int | float, season_end: int | float) -> list:
    if "|" in lb_type:
        return _order_players(
            players, lb_type,
            lambda player: _player_scope_stats(player, [("selected", season_start, season_end)])["selected"],
        )
    if lb_type == "Highest Elo":
        return sorted(players, key=lambda player: player.highest_elo, reverse=True)
    if lb_type == "Elo Reversed":
        return sorted(players, key=lambda player: player.elo, reverse=False)
    if lb_type == "Elo":
        return sorted(players, key=lambda player: player.elo, reverse=True)

    season_matches = {
        id(player): get_season_matches(player.matches, season_start, season_end)
        for player in players
    }
    return _rank_match_board(players, lb_type, season_matches)


async def construct_leaderboard_string(guild: ipy.Guild, players_sorted: list[Player], lb_type: str,
                                       season_start: int | float, season_end: int | float,
                                       starting_index: int = 0, *, paged: bool = False,
                                       tier: str = "Overall") -> str:
    if paged:
        page = players_sorted
    else:
        page = players_sorted[starting_index:starting_index + LB_ENTRIES_PER_PAGE]
    if lb_type in {"Tokens", "Predictions"}:
        members = await asyncio.gather(*(guild.fetch_member(row.user_id) for row in page))
        return final_board_text(page, lb_type, {row.user_id: mention_user(member, row.user_id, mention_type="Player")
            for row, member in zip(page, members)})
    members = await asyncio.gather(*(guild.fetch_member(player.user_id) for player in page))
    leaderboard = ""
    for index, (player, user) in enumerate(zip(page, members)):
        user_mention = mention_user(user, player.user_id, mention_type="Player")

        rank_index = starting_index + index
        if rank_index > 2:
            emoji = f"**#{rank_index + 1}**"
        else:
            emoji = RANK_REVERSED_EMOJIS[rank_index] if lb_type == "Elo Reversed" else RANK_EMOJIS[rank_index]

        if _is_hitrate(lb_type) and tier != "Overall":
            stats = _player_scope_stats(player, [("selected", season_start, season_end)])["selected"]
            district = "" if lb_type == "Hitrate" else lb_type.removesuffix(" Hitrate")
            count, stars, destruction = stats.qualities[(tier, district)]
            leaderboard += (f"{emoji} {user_mention} `{count}` → "
                            f"`{stars / count:.2f}⭐ {destruction / count:.2f}%`\n")
        elif lb_type == "Highest Elo":
            leaderboard += f"{emoji} {user_mention} `{player.highest_elo}`\n"
        elif lb_type == "Elo Reversed":
            leaderboard += f"{emoji} {user_mention} `{player.elo}`\n"
        elif lb_type == "Elo":
            leaderboard += f"{emoji} {user_mention} `{player.elo}`\n"
        elif lb_type == "Addiction":
            season_matches = get_season_matches(player.matches, season_start, season_end)
            leaderboard += f"{emoji} {user_mention} `{len(season_matches)}`\n"
        elif lb_type == "Winrate":
            season_matches = get_season_matches(player.matches, season_start, season_end)
            winrate = get_winrate(season_matches, player.user_id)
            leaderboard += (f"{emoji} {user_mention} `{len(season_matches)}` → "
                            f"`{winrate:.2f}%`\n")
        elif lb_type == "Hitrate":
            season_matches = get_season_matches(player.matches, season_start, season_end)
            avg_stars = get_avg_stars(season_matches, player.user_id)
            avg_destruction = get_avg_destruction(season_matches, player.user_id)
            hitrate = f"{avg_stars:.2f}⭐ {avg_destruction:.2f}%"
            leaderboard += (f"{emoji} {user_mention} `{len(season_matches) * 3}` → "
                            f"`{hitrate}`\n")
        else:
            district = lb_type.replace(" Hitrate", "")
            district_matches = get_district_matches(
                get_season_matches(player.matches, season_start, season_end),
                district,
                player.user_id
            )
            avg_stars = get_district_avg_stars(district_matches, district, player.user_id)
            avg_destruction = get_district_avg_destruction(district_matches, district, player.user_id)
            hitrate = f"{avg_stars:.2f}⭐ {avg_destruction:.2f}%"
            leaderboard += (f"{emoji} {user_mention} `{len(district_matches)}` → "
                            f"`{hitrate}`\n")

    return leaderboard


_LIVE_ELO_SORT = {
    "Elo": "-elo",
    "Elo Reversed": "+elo",
    "Highest Elo": "-highest_elo",
}


def _is_live_elo_board(lb_type: str, season_key: str) -> bool:
    if lb_type not in _LIVE_ELO_SORT:
        return False
    return not (lb_type in ("Elo", "Elo Reversed") and season_key.isdigit())


_MATCH_LEADERBOARD_TYPES = tuple(
    option.value for option in LB_TYPE_OPTIONS if option.value not in _LIVE_ELO_SORT
    and option.value not in {"Tokens", "Predictions"}
)
_RANK_SCAN_CHUNK = 25


class _SeasonStats:
    __slots__ = ("match_count", "wins", "stars", "destruction", "districts", "qualities")

    def __init__(self) -> None:
        self.match_count = 0
        self.wins = 0
        self.stars = 0
        self.destruction = 0
        self.districts: dict[str, list] = {}
        self.qualities: dict[tuple[str, str], list[int]] = {}


_ZERO_STATS = _SeasonStats()


def _season_scopes(seasons: list[Season]) -> list[tuple[str, int | float, int | float]]:
    scopes = [("All Seasons", *interpret_season_string(seasons, "All Seasons")[1:])]
    scopes.append(("Current Season", *interpret_season_string(seasons, "Current Season")[1:]))
    if seasons:
        for season in seasons:
            season_key = str(season.season_number)
            scopes.append((season_key, *interpret_season_string(seasons, season_key)[1:]))
    return scopes


def _player_scope_stats(player: Player, scopes: list[tuple[str, int | float, int | float]]) -> dict[str, _SeasonStats]:
    grouped = {season_key: _SeasonStats() for season_key, _, _ in scopes}
    for match in player.matches or []:
        played_at = match.time
        record = match.players[player.user_id]
        for season_key, season_start, season_end in scopes:
            if not season_start < played_at < season_end:
                continue
            stats = grouped[season_key]
            stats.match_count += 1
            stats.wins += int(bool(record.won))
            stats.stars += record.stars
            stats.destruction += record.destruction
            for district, district_stats in record.districts.items():
                totals = stats.districts.get(district)
                if totals is None:
                    totals = [0, 0, 0]
                    stats.districts[district] = totals
                totals[0] += 1
                totals[1] += district_stats.stars
                totals[2] += district_stats.destruction
                quality = getattr(district_stats, "base_quality", None)
                if quality in _HITRATE_TIERS[1:]:
                    for quality_district in ("", district):
                        quality_totals = stats.qualities.setdefault((quality, quality_district), [0, 0, 0])
                        quality_totals[0] += 1
                        quality_totals[1] += district_stats.stars
                        quality_totals[2] += district_stats.destruction
    return grouped


def _winrate(stats: _SeasonStats) -> float:
    if not stats.match_count:
        return 0.0
    return float(stats.wins / stats.match_count) * 100.0


def _hitrate(stats: _SeasonStats) -> float:
    attacks = stats.match_count * 3
    if not attacks:
        return 0.0
    return (stats.stars / attacks) * 1000 + (stats.destruction / attacks)


def _district_count(stats: _SeasonStats, district: str) -> int:
    totals = stats.districts.get(district)
    return 0 if totals is None else totals[0]


def _district_hitrate(stats: _SeasonStats, district: str) -> float:
    totals = stats.districts.get(district)
    if totals is None or not totals[0]:
        return 0.0
    count, stars, destruction = totals
    return (stars / count) * 1000 + (destruction / count)


def _order_players(players: list[Player], lb_type: str, stats_of) -> list:
    if "|" in lb_type:
        board, tier = lb_type.split("|", 1)
        district = "" if board == "Hitrate" else board.removesuffix(" Hitrate")
        def totals(player):
            return stats_of(player).qualities.get((tier, district), [0, 0, 0])
        eligible = [player for player in players if totals(player)[0] >= 5]
        return sorted(eligible, key=lambda player: (totals(player)[1] * 1000 + totals(player)[2])
                      / totals(player)[0], reverse=True)
    if lb_type == "Addiction":
        return sorted(players, key=lambda player: stats_of(player).match_count, reverse=True)
    if lb_type == "Winrate":
        eligible = [player for player in players if stats_of(player).match_count >= 5]
        return sorted(eligible, key=lambda player: _winrate(stats_of(player)), reverse=True)
    if lb_type == "Hitrate":
        eligible = [player for player in players if stats_of(player).match_count >= 5]
        return sorted(eligible, key=lambda player: _hitrate(stats_of(player)), reverse=True)
    district = lb_type.replace(" Hitrate", "")
    eligible = [player for player in players if _district_count(stats_of(player), district) >= 5]
    return sorted(
        eligible,
        key=lambda player: _district_hitrate(stats_of(player), district),
        reverse=True,
    )


async def _live_elo_page(lb_type: str, starting_index: int, *,
                         total: int | None = None) -> tuple[list[Player], int]:
    if total is None:
        total = await Player.find_all().count()
    if total <= 0 or starting_index < 0 or starting_index >= total:
        return [], total
    page = await (
        Player.find_all()
        .sort(_LIVE_ELO_SORT[lb_type])
        .skip(starting_index)
        .limit(LB_ENTRIES_PER_PAGE)
        .to_list()
    )
    return page, total


class LeaderboardCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot
        self._player_cache: list[Player] = []
        self._seasons_cache: list[Season] | None = None
        self._rankings: dict[tuple[str, str, int | float], list] = {}
        self._ranking_generation = 0
        self._players_reload = False
        self._reload_done = asyncio.Event()
        self._reload_done.set()
        self._ranking_task: asyncio.Task | None = None
        self._board_stats: dict[str, dict[str, _SeasonStats]] = {}
        self._stats_dirty: set[str] | None = None

    def _discard_rankings(self) -> None:
        self._rankings.clear()
        self._ranking_generation += 1

    def _schedule_ranking_refresh(self) -> None:
        task = self._ranking_task
        if task is not None and not task.done():
            return
        self._ranking_task = asyncio.create_task(self._refresh_rankings())

    def request_player_reload(self) -> None:
        """Mongo changed match history outside the in-memory player objects."""
        self._players_reload = True
        self._stats_dirty = None
        self._board_stats = {}
        self._reload_done.clear()
        self._discard_rankings()
        self._schedule_ranking_refresh()

    async def refresh_cache(self):
        """Refreshes the entire player cache from the database."""
        self._board_stats = {}
        self._stats_dirty = None
        self._discard_rankings()
        self._seasons_cache = None
        self._player_cache = await Player.find_all(fetch_links=True).to_list()
        self._discard_rankings()
        self._schedule_ranking_refresh()

    async def update_player_cache(self, player: Player):
        """Updates a specific player in the cache."""
        # Remove the old player object if it exists
        self._player_cache = [p for p in self._player_cache if p.user_id != player.user_id]
        # Add the new player object
        self._player_cache.append(player)
        if self._stats_dirty is not None:
            self._stats_dirty.add(player.user_id)
        self._discard_rankings()
        self._schedule_ranking_refresh()

    async def get_players(self, force_refresh: bool = False) -> list[Player]:
        if force_refresh:
            await self.refresh_cache()
        await self._reload_done.wait()
        return self._player_cache

    async def _refresh_rankings(self) -> None:
        try:
            while True:
                generation = self._ranking_generation
                if self._players_reload:
                    self._players_reload = False
                    loaded = await Player.find_all(fetch_links=True).to_list()
                    if self._ranking_generation != generation or self._players_reload:
                        self._players_reload = True
                        continue
                    self._player_cache = loaded
                    self._board_stats = {}
                    self._stats_dirty = None
                    self._rankings.clear()
                    self._reload_done.set()
                if await self._warm_rankings() and self._ranking_generation == generation and not self._players_reload:
                    return
        except Exception:
            traceback.print_exc()
            self._reload_done.set()

    async def _seasons(self) -> list[Season]:
        if self._seasons_cache is None:
            self._seasons_cache = await Season.find_all().sort("season_number").to_list()
        return self._seasons_cache

    def _stats_current(self, generation: int, players: list[Player]) -> bool:
        return (
            self._ranking_generation == generation
            and self._player_cache is players
            and not self._players_reload
        )

    def _stats_for(self, player: Player, season_key: str) -> _SeasonStats:
        return self._board_stats.get(player.user_id, {}).get(season_key, _ZERO_STATS)

    async def _ranked_players(self, players: list[Player], lb_type: str, season_key: str,
                              season_start: int | float, season_end: int | float) -> list:
        if players is not self._player_cache:
            return await sort_players(players, lb_type, season_start, season_end)

        key = (lb_type, season_key, season_start)
        ranked = self._rankings.get(key)
        if ranked is not None:
            return ranked
        task = self._ranking_task
        if task is not None and not task.done():
            await task
            ranked = self._rankings.get(key)
            if ranked is not None and self._player_cache is players:
                return ranked
        if await self._warm_rankings():
            ranked = self._rankings.get(key)
            if ranked is not None:
                return ranked
        return await sort_players(players, lb_type, season_start, season_end)

    async def _build_board_stats(self, players: list[Player],
                                 scopes: list[tuple[str, int | float, int | float]],
                                 generation: int) -> dict[str, dict[str, _SeasonStats]] | None:
        built: dict[str, dict[str, _SeasonStats]] = {}
        for index, player in enumerate(players):
            if not self._stats_current(generation, players):
                return None
            built[player.user_id] = _player_scope_stats(player, scopes)
            if index % _RANK_SCAN_CHUNK == _RANK_SCAN_CHUNK - 1:
                await asyncio.sleep(0)
        return built

    async def _warm_rankings(self) -> bool:
        generation = self._ranking_generation
        seasons = await self._seasons()
        players = self._player_cache
        if not self._stats_current(generation, players):
            return False
        scopes = _season_scopes(seasons)
        if self._stats_dirty is None or not self._board_stats:
            built = await self._build_board_stats(players, scopes, generation)
            if built is None or not self._stats_current(generation, players):
                return False
            self._board_stats = built
        else:
            for user_id in list(self._stats_dirty):
                if not self._stats_current(generation, players):
                    return False
                player = next((item for item in players if item.user_id == user_id), None)
                if player is None:
                    self._board_stats.pop(user_id, None)
                else:
                    self._board_stats[user_id] = _player_scope_stats(player, scopes)
            if not self._stats_current(generation, players):
                return False
        ranked: dict[tuple[str, str, int | float], list] = {}
        for season_key, season_start, _season_end in scopes:
            if not self._stats_current(generation, players):
                return False
            for lb_type in _MATCH_LEADERBOARD_TYPES:
                ranked[(lb_type, season_key, season_start)] = _order_players(
                    players,
                    lb_type,
                    lambda player, key=season_key: self._stats_for(player, key),
                )
                if _is_hitrate(lb_type):
                    for tier in _HITRATE_TIERS[1:]:
                        tier_type = _ranking_type(lb_type, tier)
                        ranked[(tier_type, season_key, season_start)] = _order_players(
                            players, tier_type, lambda player, key=season_key: self._stats_for(player, key)
                        )
            await asyncio.sleep(0)
            if not self._stats_current(generation, players):
                return False
        self._rankings = ranked
        if not self._stats_current(generation, players):
            self._rankings.clear()
            return False
        self._stats_dirty = set()
        return True

    async def _board_rows(self, lb_type: str, season: str, season_start: int | float,
                          season_end: int | float, starting_index: int,
                          seasons: list[Season], tier: str = "Overall") -> tuple[list, int, bool]:
        if lb_type in {"Tokens", "Predictions"}:
            rows = await final_board_rows(lb_type, season)
            return rows, len(rows), False
        if _is_live_elo_board(lb_type, season):
            page, total = await _live_elo_page(lb_type, starting_index)
            return page, total, True

        if lb_type in ["Elo", "Elo Reversed"] and season.isdigit():
            season_doc = get_season_by_number(seasons, int(season))
            if season_doc is not None:
                players = [
                    Player(id=p.player_id, user_id=p.player_id, tag="", elo=p.elo, highest_elo=p.elo)
                    for p in season_doc.players
                ]
            else:
                players = []
        else:
            players = await self.get_players()

        ranked = await self._ranked_players(players, _ranking_type(lb_type, tier), season, season_start, season_end)
        return ranked, len(ranked), False

    async def _initial_board(self, lb_type: str, season: str | None, seasons: list[Season],
                             tier: str = "Overall") -> tuple:
        use_default = season is None
        season = _normalize_season(lb_type, season)
        if lb_type in _MATCH_LEADERBOARD_TYPES and not self._player_cache:
            await self.get_players(force_refresh=True)
        scope, season_start, season_end = _scope_window(lb_type, season, seasons)
        rows, total, paged = await self._board_rows(lb_type, season, season_start, season_end, 0, seasons, tier)
        has_entries = bool(total)
        if lb_type == "Addiction":
            has_entries = any(get_season_matches(player.matches, season_start, season_end) for player in rows)
        if use_default and not has_entries and lb_type in (*_MATCH_LEADERBOARD_TYPES, "Predictions"):
            season = "All Seasons"
            scope, season_start, season_end = _scope_window(lb_type, season, seasons)
            rows, total, paged = await self._board_rows(lb_type, season, season_start, season_end, 0, seasons, tier)
        if lb_type in {"Tokens", "Predictions"} and rows:
            scope = rows[0].scope
            if lb_type == "Predictions":
                season = scope.removeprefix("Season ").removesuffix(" Final")
        return season, scope, season_start, season_end, rows, total, paged

    @ipy.slash_command(name="leaderboard", description="Display leaderboard based on elo", scopes=GUILD_IDS)
    @ipy.slash_option(
        name="lb_type",
        description="The type of the leaderboard",
        choices=[
            ipy.SlashCommandChoice(name="Elo", value="Elo"),
            ipy.SlashCommandChoice(name="Elo Reversed", value="Elo Reversed"),
            ipy.SlashCommandChoice(name="Highest Elo", value="Highest Elo"),
            ipy.SlashCommandChoice(name="Addiction", value="Addiction"),
            ipy.SlashCommandChoice(name="Winrate", value="Winrate"),
            ipy.SlashCommandChoice(name="Hitrate", value="Hitrate"),
            ipy.SlashCommandChoice(name="Tokens", value="Tokens"),
            ipy.SlashCommandChoice(name="Predictions", value="Predictions"),
            ipy.SlashCommandChoice(name="Capital Peak Hitrate", value="Capital Peak Hitrate"),
            ipy.SlashCommandChoice(name="Wizard Valley Hitrate", value="Wizard Valley Hitrate"),
            ipy.SlashCommandChoice(name="Barbarian Camp Hitrate", value="Barbarian Camp Hitrate"),
            ipy.SlashCommandChoice(name="Balloon Lagoon Hitrate", value="Balloon Lagoon Hitrate"),
            ipy.SlashCommandChoice(name="Builder's Workshop Hitrate", value="Builder's Workshop Hitrate"),
            ipy.SlashCommandChoice(name="Dragon Cliffs Hitrate", value="Dragon Cliffs Hitrate"),
            ipy.SlashCommandChoice(name="Golem Quarry Hitrate", value="Golem Quarry Hitrate"),
            ipy.SlashCommandChoice(name="Skeleton Park Hitrate", value="Skeleton Park Hitrate"),
            ipy.SlashCommandChoice(name="Goblin Mines Hitrate", value="Goblin Mines Hitrate"),
        ],
        opt_type=ipy.OptionType.STRING
    )
    @ipy.slash_option(
        name="live",
        description="Make it a live embed or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    @ipy.slash_option(
        name="season",
        description="The season to check",
        opt_type=ipy.OptionType.STRING,
        autocomplete=True
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def leaderboard(self, ctx: ipy.SlashContext, lb_type: str = "Elo", live: bool = False,
                          season: str | None = None, hidden: bool = False):
        if not live:
            await ctx.defer(ephemeral=hidden)
        else:
            await ctx.defer()

        seasons = await self._seasons()
        season, scope, season_start, season_end, players_sorted, board_size, paged = await self._initial_board(
            lb_type, season, seasons
        )

        embed = ipy.Embed(
            title=f"**{lb_type} Leaderboard**",
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )
        embed.add_field(name="**Scope**", value=scope)

        if (board_size <= 0 or not players_sorted) and not _is_hitrate(lb_type):
            await ctx.send(f"{ERROR_EMOJI} There are no players in this leaderboard.", ephemeral=True)

            return

        leaderboard = (
            await construct_leaderboard_string(ctx.guild, players_sorted, lb_type, season_start, season_end, paged=paged)
            if players_sorted else "No eligible players in this leaderboard."
        )

        if live:
            components = ipy.Button(
                style=ipy.ButtonStyle.GRAY,
                label="Refresh",
                custom_id="lb_refresh",
                emoji=ipy.PartialEmoji(name="🔄")
            )
            if _is_hitrate(lb_type):
                components = [ipy.ActionRow(components), _tier_menu("Overall")]

            hidden = False
            timestamp = ipy.Timestamp.fromdatetime(datetime.now()).format(style=ipy.TimestampStyles.RelativeTime)
            embed.description = f"Last refreshed: {timestamp}\n\n{leaderboard}"
        else:
            lb_type_select = ipy.StringSelectMenu(
                *_selected_options(LB_TYPE_OPTIONS, lb_type),
                placeholder="📊 Select the leaderboard type",
                custom_id="lb_type_select"
            )

            components = [ipy.ActionRow(lb_type_select)]
            if lb_type not in {"Highest Elo", "Tokens"}:
                select_options = _season_options(lb_type, seasons, season)

                lb_season_select = ipy.StringSelectMenu(
                    *_selected_options(select_options, season),
                    placeholder="🔍 Select a season",
                    custom_id="lb_season_select"
                )

                components.append(ipy.ActionRow(lb_season_select))

            if _is_hitrate(lb_type):
                components.append(_tier_menu("Overall"))

            left_button = ipy.Button(
                style=ipy.ButtonStyle.PRIMARY,
                label="◀️️",
                custom_id=f"lb_left|{0 - LB_ENTRIES_PER_PAGE}",
                disabled=True,
            )

            total_pages = max(1, math.ceil(board_size / LB_ENTRIES_PER_PAGE))
            current_page = int(0 / LB_ENTRIES_PER_PAGE) + 1
            page_number_button = build_page_button(current_page, total_pages, "lb_page")

            right_button = ipy.Button(
                style=ipy.ButtonStyle.PRIMARY,
                label="▶️",
                custom_id=f"lb_right|{0 + LB_ENTRIES_PER_PAGE}",
                disabled=(0 + LB_ENTRIES_PER_PAGE) >= board_size
            )

            components.append(ipy.ActionRow(left_button, page_number_button, right_button))

            embed.description = leaderboard

        await ctx.send(embed=embed, components=components, ephemeral=hidden)

    @ipy.component_callback(re.compile(r"lb_(right|left)\|.*"))
    async def lb_pagination(self, ctx: ipy.ComponentContext):
        if str(ctx.message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)

            return

        lb_type = ctx.message.embeds[0].title.replace("**", "").replace(" Leaderboard", "")
        tier = _view_tier(ctx.message)
        season_value = ctx.message.embeds[0].fields[0].value
        if season_value in ["All Seasons", "Current Season", "Lifetime"]:
            season = season_value
        else:
            season_num = int(season_value.replace("Season ", "").replace(" Final", ""))
            season = str(season_num)
        _, starting_index = ctx.custom_id.split("|")
        starting_index = int(starting_index)

        season = _normalize_season(lb_type, season)
        seasons = await self._seasons()
        scope, season_start, season_end = _scope_window(lb_type, season, seasons)

        players_sorted, board_size, paged = await self._board_rows(
            lb_type, season, season_start, season_end, starting_index, seasons, tier
        )
        if board_size <= 0:
            await ctx.send(f"{ERROR_EMOJI} There are no players in this leaderboard.", ephemeral=True)
            return
        if not players_sorted:
            await ctx.send(f"{ERROR_EMOJI} This page no longer exists.", ephemeral=True)
            return

        if lb_type in {"Tokens", "Predictions"}:
            scope = players_sorted[0].scope
        leaderboard = await _board_text(
            ctx.guild, players_sorted, lb_type, season_start, season_end, starting_index, paged=paged, tier=tier
        )

        ctx.message.embeds[0].description = leaderboard
        ctx.message.embeds[0].fields[0].value = scope
        if lb_type in {"Elo", "Elo Reversed"}:
            menu = ctx.message.components[1].components[0]
            menu.options = _selected_options(menu.options, season)

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"lb_left|{starting_index - LB_ENTRIES_PER_PAGE}",
            disabled=starting_index == 0,
        )

        total_pages = math.ceil(board_size / LB_ENTRIES_PER_PAGE)
        current_page = int(starting_index / LB_ENTRIES_PER_PAGE) + 1
        page_number_button = build_page_button(current_page, total_pages, "lb_page")

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"lb_right|{starting_index + LB_ENTRIES_PER_PAGE}",
            disabled=(starting_index + LB_ENTRIES_PER_PAGE) >= board_size
        )

        ctx.message.components[-1] = ipy.ActionRow(left_button, page_number_button, right_button)

        await ctx.edit_origin(embeds=ctx.message.embeds, components=ctx.message.components)

    @ipy.component_callback("lb_page")
    async def lb_page_button(self, ctx: ipy.ComponentContext):
        if str(ctx.message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)
            return

        await ctx.send_modal(build_page_jump_modal("lb_page_modal"))

    @ipy.modal_callback("lb_page_modal")
    async def lb_page_modal(self, ctx: ipy.ModalContext, **response):
        message = ctx.message
        if message is None:
            await ctx.send(f"{ERROR_EMOJI} The paginated message is no longer available.", ephemeral=True)
            return

        if str(message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)
            return

        lb_type = message.embeds[0].title.replace("**", "").replace(" Leaderboard", "")
        tier = _view_tier(message)
        season_value = message.embeds[0].fields[0].value
        if season_value in ["All Seasons", "Current Season", "Lifetime"]:
            season = season_value
        else:
            season = str(int(season_value.replace("Season ", "").replace(" Final", "")))

        season = _normalize_season(lb_type, season)
        seasons = await self._seasons()
        scope, season_start, season_end = _scope_window(lb_type, season, seasons)
        if _is_live_elo_board(lb_type, season):
            total = await Player.find_all().count()
            if total <= 0:
                await ctx.send(f"{ERROR_EMOJI} There are no players in this leaderboard.", ephemeral=True)
                return
            total_pages = math.ceil(total / LB_ENTRIES_PER_PAGE)
            try:
                page = parse_page_number(response["page"], total_pages)
            except InvalidPageError as error:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
                return
            starting_index = (page - 1) * LB_ENTRIES_PER_PAGE
            players_sorted, board_size = await _live_elo_page(lb_type, starting_index, total=total)
            paged = True
            if not players_sorted:
                await ctx.send(f"{ERROR_EMOJI} This page no longer exists.", ephemeral=True)
                return
        else:
            players_sorted, board_size, paged = await self._board_rows(
                lb_type, season, season_start, season_end, 0, seasons, tier
            )
            if not players_sorted:
                await ctx.send(f"{ERROR_EMOJI} There are no players in this leaderboard.", ephemeral=True)
                return
            total_pages = math.ceil(board_size / LB_ENTRIES_PER_PAGE)
            try:
                page = parse_page_number(response["page"], total_pages)
            except InvalidPageError as error:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
                return
            starting_index = (page - 1) * LB_ENTRIES_PER_PAGE

        if lb_type in {"Tokens", "Predictions"}:
            scope = players_sorted[0].scope
        leaderboard = await _board_text(
            ctx.guild, players_sorted, lb_type, season_start, season_end, starting_index, paged=paged, tier=tier
        )
        message.embeds[0].description = leaderboard
        message.embeds[0].fields[0].value = scope
        if lb_type in {"Elo", "Elo Reversed"}:
            menu = message.components[1].components[0]
            menu.options = _selected_options(menu.options, season)

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"lb_left|{starting_index - LB_ENTRIES_PER_PAGE}",
            disabled=starting_index == 0,
        )
        page_number_button = build_page_button(page, total_pages, "lb_page")
        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"lb_right|{starting_index + LB_ENTRIES_PER_PAGE}",
            disabled=(starting_index + LB_ENTRIES_PER_PAGE) >= board_size,
        )

        message.components[-1] = ipy.ActionRow(left_button, page_number_button, right_button)
        await ctx.edit(message, embeds=message.embeds, components=message.components)

    @ipy.component_callback(re.compile(r"lb_type_select(?:\|.*)?"))
    async def lb_type_select(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        if str(ctx.message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)

            return

        lb_type = ctx.values[0]
        tier = _view_tier(ctx.message)
        seasons = await self._seasons()
        season, scope, season_start, season_end, players_sorted, board_size, paged = await self._initial_board(
            lb_type, None, seasons, tier
        )
        if (board_size <= 0 or not players_sorted) and not _is_hitrate(lb_type):
            await ctx.send(f"{ERROR_EMOJI} There are no players in this leaderboard.", ephemeral=True)

            return

        leaderboard = await _board_text(
            ctx.guild, players_sorted, lb_type, season_start, season_end, paged=paged, tier=tier
        )

        ctx.message.embeds[0].title = f"**{lb_type} Leaderboard**"
        ctx.message.embeds[0].description = leaderboard
        ctx.message.embeds[0].fields[0].value = scope

        # update the components accordingly
        lb_type_select = ipy.StringSelectMenu(
            *_selected_options(LB_TYPE_OPTIONS, lb_type),
            placeholder="📊 Select the leaderboard type",
            custom_id=f"lb_type_select|{tier}" if tier != "Overall" else "lb_type_select"
        )

        components = [ipy.ActionRow(lb_type_select)]
        if lb_type not in {"Highest Elo", "Tokens"}:
            select_options = _season_options(lb_type, seasons, season)

            lb_season_select = ipy.StringSelectMenu(
                *_selected_options(select_options, season),
                placeholder="🔍 Select a season",
                custom_id="lb_season_select"
            )

            components.append(ipy.ActionRow(lb_season_select))

        if _is_hitrate(lb_type):
            components.append(_tier_menu(tier))

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"lb_left|{0 - LB_ENTRIES_PER_PAGE}",
            disabled=True,
        )

        total_pages = max(1, math.ceil(board_size / LB_ENTRIES_PER_PAGE))
        current_page = int(0 / LB_ENTRIES_PER_PAGE) + 1
        page_number_button = build_page_button(current_page, total_pages, "lb_page")

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"lb_right|{0 + LB_ENTRIES_PER_PAGE}",
            disabled=(0 + LB_ENTRIES_PER_PAGE) >= board_size
        )

        components.append(ipy.ActionRow(left_button, page_number_button, right_button))

        await ctx.edit(ctx.message, embeds=ctx.message.embeds, components=components)

        await ctx.send(f"{SUCCESS_EMOJI} The leaderboard type is changed to `{lb_type}`.", ephemeral=True)

    @ipy.component_callback("lb_season_select")
    async def lb_season_select(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        if str(ctx.message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.",
                           ephemeral=True)

            return

        lb_type = ctx.message.embeds[0].title.replace("**", "").replace(" Leaderboard", "")
        tier = _view_tier(ctx.message)
        season = _normalize_season(lb_type, ctx.values[0])
        seasons = await self._seasons()
        scope, season_start, season_end = _scope_window(lb_type, season, seasons)

        players_sorted, board_size, paged = await self._board_rows(
            lb_type, season, season_start, season_end, 0, seasons, tier
        )
        if lb_type in {"Tokens", "Predictions"} and players_sorted:
            scope = players_sorted[0].scope
            if lb_type == "Predictions":
                season = scope.removeprefix("Season ").removesuffix(" Final")
        if (board_size <= 0 or not players_sorted) and not _is_hitrate(lb_type):
            await ctx.send(f"{ERROR_EMOJI} There are no players in this leaderboard.", ephemeral=True)

            return

        leaderboard = await _board_text(
            ctx.guild, players_sorted, lb_type, season_start, season_end, paged=paged, tier=tier
        )

        ctx.message.embeds[0].description = leaderboard
        ctx.message.embeds[0].fields[0].value = scope

        if lb_type not in {"Highest Elo", "Tokens"}:
            menu = ctx.message.components[1].components[0]
            menu.options = _selected_options(menu.options, season)

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"lb_left|{0 - LB_ENTRIES_PER_PAGE}",
            disabled=True,
        )

        total_pages = max(1, math.ceil(board_size / LB_ENTRIES_PER_PAGE))
        current_page = int(0 / LB_ENTRIES_PER_PAGE) + 1
        page_number_button = build_page_button(current_page, total_pages, "lb_page")

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"lb_right|{0 + LB_ENTRIES_PER_PAGE}",
            disabled=(0 + LB_ENTRIES_PER_PAGE) >= board_size
        )

        ctx.message.components[-1] = ipy.ActionRow(left_button, page_number_button, right_button)

        await ctx.edit(ctx.message, embeds=ctx.message.embeds, components=ctx.message.components)

        await ctx.send(f"{SUCCESS_EMOJI} The scope is changed to `{scope}`.", ephemeral=True)

    @ipy.component_callback("lb_tier_select")
    async def lb_tier_select(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)
        if str(ctx.message.interaction.user.id) != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.", ephemeral=True)
            return

        tier = ctx.values[0]
        lb_type = ctx.message.embeds[0].title.replace("**", "").replace(" Leaderboard", "")
        scope = ctx.message.embeds[0].fields[0].value
        season = scope.removeprefix("Season ").removesuffix(" Final")
        seasons = await self._seasons()
        scope, season_start, season_end = _scope_window(lb_type, season, seasons)
        players, size, paged = await self._board_rows(lb_type, season, season_start, season_end, 0, seasons, tier)
        ctx.message.embeds[0].description = await _board_text(
            ctx.guild, players, lb_type, season_start, season_end, paged=paged, tier=tier
        )
        is_live = ctx.message.components[0].components[0].custom_id.startswith("lb_refresh")
        ctx.message.components[0].components[0].custom_id = (
            f"{'lb_refresh' if is_live else 'lb_type_select'}|{tier}" if tier != "Overall"
            else ("lb_refresh" if is_live else "lb_type_select")
        )
        if is_live:
            ctx.message.components[-1] = _tier_menu(tier)
            await ctx.edit(ctx.message, embeds=ctx.message.embeds, components=ctx.message.components)
            return
        ctx.message.components[-2] = _tier_menu(tier)
        ctx.message.components[-1] = ipy.ActionRow(
            ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="◀️️", custom_id=f"lb_left|{-LB_ENTRIES_PER_PAGE}",
                       disabled=True),
            build_page_button(1, max(1, math.ceil(size / LB_ENTRIES_PER_PAGE)), "lb_page"),
            ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="▶️", custom_id=f"lb_right|{LB_ENTRIES_PER_PAGE}",
                       disabled=size <= LB_ENTRIES_PER_PAGE),
        )
        await ctx.edit(ctx.message, embeds=ctx.message.embeds, components=ctx.message.components)

    @ipy.component_callback(re.compile(r"lb_refresh(?:\|.*)?"))
    async def lb_refresh(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        selected_scope = ctx.message.embeds[0].fields[0].value
        lb_type = ctx.message.embeds[0].title.replace("**", "").replace(" Leaderboard", "")
        tier = _view_tier(ctx.message)
        season = _normalize_season(lb_type, selected_scope.removeprefix("Season ").removesuffix(" Final"))
        if lb_type in ["Elo", "Elo Reversed"] and season.isdigit():
            self._seasons_cache = None
        seasons = await self._seasons()
        scope, season_start, season_end = _scope_window(lb_type, season, seasons)

        starting_index = 0
        if lb_type in {"Tokens", "Predictions"} and isinstance(ctx.message.components, list) and len(ctx.message.components) > 1:
            if str(ctx.message.interaction.user.id) != str(ctx.author.id):
                await ctx.send(f"{ERROR_EMOJI} You cannot interfere with other's components.", ephemeral=True)
                return
            starting_index = int(ctx.message.components[-1].components[-1].custom_id.split("|")[1]) - LB_ENTRIES_PER_PAGE
        players_sorted, board_size, paged = await self._board_rows(
            lb_type, season, season_start, season_end, 0, seasons, tier
        )
        if lb_type in {"Tokens", "Predictions"} and players_sorted:
            scope = players_sorted[0].scope
        if not players_sorted and not _is_hitrate(lb_type):
            await ctx.send(f"{ERROR_EMOJI} There are no players in this leaderboard.", ephemeral=True)

            return

        leaderboard = await _board_text(
            ctx.guild, players_sorted, lb_type, season_start, season_end,
            max(0, min(starting_index, (board_size - 1) // LB_ENTRIES_PER_PAGE * LB_ENTRIES_PER_PAGE)),
            paged=paged, tier=tier
        )

        timestamp = ipy.Timestamp.fromdatetime(datetime.now()).format(style=ipy.TimestampStyles.RelativeTime)
        ctx.message.embeds[0].description = f"Last refreshed: {timestamp}\n\n{leaderboard}"
        ctx.message.embeds[0].fields[0].value = scope
        if (lb_type in {"Elo", "Elo Reversed"} and isinstance(ctx.message.components, list)
                and len(ctx.message.components) > 1):
            menu = ctx.message.components[1].components[0]
            menu.options = _selected_options(menu.options, season)

        if lb_type in {"Tokens", "Predictions"} and isinstance(ctx.message.components, list) and len(ctx.message.components) > 1:
            starting_index = min(starting_index, (board_size - 1) // LB_ENTRIES_PER_PAGE * LB_ENTRIES_PER_PAGE)
            ctx.message.components[-1] = ipy.ActionRow(
                ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="◀️️",
                           custom_id=f"lb_left|{starting_index - LB_ENTRIES_PER_PAGE}", disabled=starting_index == 0),
                build_page_button(starting_index // LB_ENTRIES_PER_PAGE + 1,
                                  math.ceil(board_size / LB_ENTRIES_PER_PAGE), "lb_page"),
                ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="▶️",
                           custom_id=f"lb_right|{starting_index + LB_ENTRIES_PER_PAGE}",
                           disabled=starting_index + LB_ENTRIES_PER_PAGE >= board_size),
            )

        if lb_type in {"Tokens", "Predictions"}:
            await ctx.message.edit(embeds=ctx.message.embeds, components=ctx.message.components)
        else:
            await ctx.message.edit(embeds=ctx.message.embeds)
        await ctx.send(f"{SUCCESS_EMOJI} The leaderboard is now up to date.", ephemeral=True)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        await self.refresh_cache()
        if self._ranking_task is not None:
            await self._ranking_task
        print("➤ Leaderboard commands loaded")
