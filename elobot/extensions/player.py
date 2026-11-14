import asyncio
import re

import coc

import interactions as ipy
from elobot.config.ids import BOT_ID, LVL1_ROLES, MAIN_GUILD, PLAYER_ROLE
from elobot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elobot.config.emojis import (
    DISTRICT_EMOJI, ERROR_EMOJI, LEAGUE_EMOJIS, LOSS_EMOJI, STAR_EMOJI, SUCCESS_EMOJI, TOKEN_EMOJI, WIN_EMOJI,
)
from elobot.config.game import DISTRICT_ORDER, LEAGUES
from elobot.discord.decorators import lock_files, create_async_task
from elobot.db import Match, Player, RecordedMatch, Queue, Season
from elobot.features.seasons.selection import interpret_season_string
from elobot.discord.members import fetch_member_otherwise_user, member_guild
from elobot.discord.mentions import mention_user
from elobot.features.leaderboards.stats import (
    get_season_matches, get_winrate, get_avg_stars, get_avg_destruction, get_opponent_avg_stars,
    get_opponent_avg_destruction, get_wins,
)
from elobot.features.players.match_choices import _option_user_id
from elobot.features.players.match_choices import fallback_global_match_choices
from elobot.features.players.match_choices import hydrate_match_id_choice_names
from elobot.features.players.match_choices import publish_match_id_choices
from elobot.features.players.match_choices import select_match_id_choices


async def construct_match_embed(bot: ipy.Client, guild: ipy.Guild, match_id: str, player_id: str) -> ipy.Embed:
    match = await RecordedMatch.find_one(RecordedMatch.id == match_id)
    if not match:
        embed = ipy.Embed(
            title="**Match not Found**",
            description=f"ID: `{match_id}`",
            color=COLOR
        )
        embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
        return embed

    player_result = match.players[player_id]
    opponent_id = player_result.opponent
    opponent_result = match.players[opponent_id]
    player_districts = list(player_result.districts.keys())

    match_info = ""
    for i in range(3):
        district = player_districts[i]
        match_info += (f"{DISTRICT_EMOJI} **{district}**\n"
                       f"{player_result.districts[district].stars}{STAR_EMOJI} "
                       f"{player_result.districts[district].destruction}% - "
                       f"{opponent_result.districts[district].stars}{STAR_EMOJI} "
                       f"{opponent_result.districts[district].destruction}%\n\n")

    match_info += (f"**Final Score**\n"
                   f"{player_result.stars}{STAR_EMOJI} {player_result.destruction}% - "
                   f"{opponent_result.stars}{STAR_EMOJI} {opponent_result.destruction}%")

    elo_change = f"Elo change: {LOSS_EMOJI} {player_result.elo_change}"
    if player_result.elo_change > 0:
        elo_change = f"Elo change: {WIN_EMOJI} +{player_result.elo_change}"

    player = await fetch_member_otherwise_user(guild, bot, player_id)
    opponent = await fetch_member_otherwise_user(guild, bot, opponent_id)

    embed = ipy.Embed(
        author=ipy.EmbedAuthor(
            name=f"{player.display_name}",
            icon_url=player.display_avatar.url
        ),
        title=f"**vs. {opponent.display_name} ({match_id})**",
        description=f"{ipy.Timestamp.fromtimestamp(match.time).format(ipy.TimestampStyles.LongDateTime)}\n"
                    f"{ipy.Timestamp.fromtimestamp(match.time).format(ipy.TimestampStyles.RelativeTime)}\n"
                    f"{elo_change}\n\n"
                    f"{match_info}",
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR
    )

    return embed


def get_matches_against(player_matches: list[RecordedMatch],
                        player_id: str, opponent_id: str) -> list[RecordedMatch]:
    matches = []
    for m in player_matches:
        if m.players[player_id].opponent == opponent_id:
            matches.append(m)
            
    return matches


class PlayerCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot

    @property
    def coc_client(self) -> coc.Client:
        return self.bot.coc_client
    
    base = ipy.SlashCommand(name="player", description="", dm_permission=True)

    @base.subcommand(
        sub_cmd_name="capital-peak",
        sub_cmd_description="Enable or disable Capital Peak in your matches",
    )
    @ipy.slash_option(
        name="enabled",
        description="Whether Capital Peak can be included in your matches",
        opt_type=ipy.OptionType.BOOLEAN,
        required=True,
    )
    async def player_capital_peak(self, ctx: ipy.SlashContext, enabled: bool):
        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(
                f"{ERROR_EMOJI} You are not registered in the elo system, "
                f"please register using the command `/player register`.",
                ephemeral=True,
            )
            return

        player.capital_peak_enabled = enabled
        await player.save_changes()
        await ctx.send(
            f"{SUCCESS_EMOJI} Capital Peak is now {'enabled' if enabled else 'disabled'} for your matches.",
            ephemeral=True,
        )

    @base.subcommand(sub_cmd_name="h2h", sub_cmd_description="See head to head result of 2 players")
    @ipy.slash_option(
        name="player1",
        description="The 1st player",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="player2",
        description="The 2nd player",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def player_h2h(self, ctx: ipy.SlashContext, player1: ipy.User, player2: ipy.User,
                         hidden: bool = False):
        await ctx.defer(ephemeral=hidden)

        p1_doc = await Player.find_one(Player.user_id == str(player1.id), fetch_links=True)
        if not p1_doc:
            await ctx.send(f"{ERROR_EMOJI} {player1.mention} is not registered in Elo.",
                           ephemeral=hidden)

            return

        p2_doc = await Player.find_one(Player.user_id == str(player2.id))
        if not p2_doc:
            await ctx.send(f"{ERROR_EMOJI} {player2.mention} is not registered in Elo.",
                           ephemeral=hidden)

            return

        matches_against_player2 = []
        player1_id = str(player1.id)
        player2_id = str(player2.id)
        
        for match in p1_doc.matches:
            if player1_id in match.players and match.players[player1_id].opponent == player2_id:
                matches_against_player2.append(match)

        if not matches_against_player2:
            await ctx.send(f"{ERROR_EMOJI} {player1.mention} have no matches against {player2.mention}.",
                           ephemeral=hidden)

            return

        # get statistics
        wins = 0
        losses = 0
        total_stars = 0
        total_destruction = 0
        opp_total_stars = 0
        opp_total_destruction = 0
        districts = {}
        
        for match in matches_against_player2:
            p1_stats = match.players[player1_id]
            p2_stats = match.players[player2_id]
            
            if p1_stats.won:
                wins += 1
            else:
                losses += 1

            total_stars += p1_stats.stars
            total_destruction += p1_stats.destruction
            opp_total_stars += p2_stats.stars
            opp_total_destruction += p2_stats.destruction
            
            for district in p1_stats.districts.keys():
                districts.setdefault(district, 0)
                districts[district] += 1

        sorted_districts = sorted(districts.items(), key=lambda x: x[1], reverse=True)
        match_count = wins + losses
        avg_stars = total_stars / match_count
        avg_destruction = total_destruction / match_count
        opp_avg_stars = opp_total_stars / match_count
        opp_avg_destruction = opp_total_destruction / match_count

        # get recent matches
        recent_matches = ""
        count = 0
        sorted_matches = sorted(matches_against_player2, key=lambda m: int(m.id), reverse=True)
        for match in sorted_matches:
            if count >= 6:
                break
            
            p1_stats = match.players[player1_id]
            p2_stats = match.players[player2_id]

            elo_change_str = f"{LOSS_EMOJI}{p1_stats.elo_change}"
            if p1_stats.elo_change > 0:
                elo_change_str = f"{WIN_EMOJI}+{p1_stats.elo_change}"

            match_ts = ipy.Timestamp.fromtimestamp(match.time).format(style=ipy.TimestampStyles.RelativeTime)
            recent_matches += f"{match_ts} ({elo_change_str})\n" \
                              f"{p1_stats.stars}{STAR_EMOJI} {p1_stats.destruction}% - " \
                              f"{p2_stats.stars}{STAR_EMOJI} {p2_stats.destruction}%\n"
            count += 1

        recent_matches = "No recent matches." if not recent_matches else recent_matches

        embed = ipy.Embed(
            title=f"**{player1}'s Record Against {player2}**",
            description=f"**Results:** {wins} - {losses}\n"
                        f"**Stars:** {avg_stars:.2f}{STAR_EMOJI} - {opp_avg_stars:.2f}{STAR_EMOJI}\n"
                        f"**Destruction:** {avg_destruction:.2f}% - {opp_avg_destruction:.2f}%\n"
                        f"**Popular Districts:**\n"
                        f"1. {DISTRICT_EMOJI} {sorted_districts[0][0]} `{sorted_districts[0][1]}`\n"
                        f"2. {DISTRICT_EMOJI} {sorted_districts[1][0]} `{sorted_districts[1][1]}`\n"
                        f"3. {DISTRICT_EMOJI} {sorted_districts[2][0]} `{sorted_districts[2][1]}`\n",
            fields=[
                ipy.EmbedField(
                    name="__**Recent Matches**__",
                    value=recent_matches
                )
            ],
            thumbnail=ipy.EmbedAttachment(url=player1.avatar.url),
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        await ctx.send(embed=embed, ephemeral=hidden)

    @base.subcommand(sub_cmd_name="deregister", sub_cmd_description="Deregister a player from the elo system")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player you want to deregister",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    async def player_deregister(self, ctx: ipy.SlashContext, player: ipy.User):
        from elobot.features.finals.administration import guard_deregistration
        from elobot.features.finals.types import FinalError
        try:
            await guard_deregistration(str(player.id))
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        p_doc = await Player.find_one(Player.user_id == str(player.id))
        if not p_doc:
            await ctx.send(f"{ERROR_EMOJI} {player.mention} is not in the system.", ephemeral=True)
            
            return

        from elobot.features.finals.administration import deregister_player
        try:
            await deregister_player(str(player.id))
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return

        await ctx.send(f"{SUCCESS_EMOJI} {player.mention} is deregistered from the system.", ephemeral=True)

    @base.subcommand(sub_cmd_name="register", sub_cmd_description="Register for the elo system")
    @ipy.slash_option(
        name="player_tag",
        description="Your in-game account tag",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    async def player_register(self, ctx: ipy.SlashContext | ipy.ModalContext, player_tag: str):
        p_doc = await Player.find_one(Player.user_id == str(ctx.user.id))
        if p_doc:
            await ctx.send(f"{ERROR_EMOJI} You are already registered in the system.", ephemeral=True)

            return

        try:
            player: coc.Player = await self.coc_client.get_player(player_tag)
        except coc.errors.NotFound:
            await ctx.send(f"{ERROR_EMOJI} The player tag you have provided is invalid.", ephemeral=True)
            return
        except coc.errors.Maintenance:
            await ctx.send(f"{ERROR_EMOJI} Please try again after the maintenance break has ended.",
                           ephemeral=True)
            return

        # check if the tag is already linked to another player
        existing_tag_doc = await Player.find_one(Player.tag == player.tag)
        if existing_tag_doc:
            await ctx.send(f"{ERROR_EMOJI} The player tag is already linked to another player.",
                           ephemeral=True)

            return

        new_player = Player(
            id=str(ctx.user.id),
            user_id=str(ctx.user.id),
            tag=player.tag,
            elo=950,
            highest_elo=950,
            wins=0,
            avg_stars=0,
            avg_destruction=0,
            opponent_avg_stars=0,
            opponent_avg_destruction=0,
            statsUpToDate=True,
            last_cancel=None,
            matches=[]
        )
        await new_player.insert()
        from elobot.features.tokens.wallets import ensure_wallet
        from elobot.features.finals.types import FinalError
        try:
            await ensure_wallet(str(ctx.user.id))
        except FinalError as error:
            await ctx.send(f"Registration saved. Tokens are unavailable: {error}", ephemeral=True)

        if isinstance(ctx.author, ipy.Member):
            await ctx.author.add_role(PLAYER_ROLE)

        elo_select = ipy.StringSelectMenu(
            ipy.StringSelectOption(
                label="Beginner (Default)", value="950",
                description="Rarely/never hit maxed districts in raids."
            ),
            ipy.StringSelectOption(
                label="Intermediate", value="1050",
                description="Can two shot maxed districts raids, but never played tournaments."
            ),
            ipy.StringSelectOption(
                label="Expert", value="1150",
                description="Have played tournaments and can two shot difficult layouts."
            ),
            placeholder="🖱️ Select your starting Elo",
            custom_id="elo_select"
        )

        await ctx.send(f"{SUCCESS_EMOJI} You are now registered in the elo system. Run "
                       f"{self.bot.mention_command('match queue')} to start your first match. \n\n"
                       f"*You may select a starting Elo based on your skill. Higher Elo "
                       f"means harder bases!*",
                       components=elo_select, ephemeral=True)

    @ipy.component_callback("elo_select")
    async def elo_select(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        p_doc = await Player.find_one(Player.user_id == str(ctx.author.id))

        queues = await Queue.find_all(fetch_links=True).to_list()
        for q in queues:
            if p_doc in q.players:
                await ctx.send(f"{ERROR_EMOJI} You cannot change your starting Elo after queuing.",
                               ephemeral=True)

                return

        in_match = await Match.find(
            {"participants.user_id": str(ctx.author.id)}
        ).exists()

        if in_match:
            await ctx.send(
                f"{ERROR_EMOJI} You cannot change your starting Elo after being in a match.",
                ephemeral=True
            )
            return
       
        if p_doc.matches:
            await ctx.send(f"{ERROR_EMOJI} You cannot change your starting Elo after playing a match.",
                           ephemeral=True)

            return

        p_doc.elo = int(ctx.values[0])
        p_doc.highest_elo = int(ctx.values[0])
        await p_doc.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} Your starting Elo is now set to `{ctx.values[0]}`.",
                       ephemeral=True)

    @base.subcommand(sub_cmd_name="profile", sub_cmd_description="To check the profile of a player")
    @ipy.slash_option(
        name="user",
        description="A user registered in the elo system",
        opt_type=ipy.OptionType.USER
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def player_profile(self, ctx: ipy.SlashContext, user: ipy.User = None, hidden: bool = False):
        await ctx.defer(ephemeral=hidden)

        user = ctx.author if not user else user
        
        player_doc = await Player.find_one(Player.user_id == str(user.id), fetch_links=True)
        
        if not player_doc:
            if user == ctx.author:
                await ctx.send(f"{ERROR_EMOJI} You are not registered in the elo system, "
                               f"please register using the command `/player register`.", ephemeral=True)
            else:
                await ctx.send(f"{ERROR_EMOJI} {user.mention} is not registered in the elo system.",
                               ephemeral=True)
            return

        player_id = str(user.id)
        elo = player_doc.elo
        total_matches = len(player_doc.matches)
        league = list(LEAGUES.keys())[-1]
        for key, value in LEAGUES.items():
            if value[0] <= elo <= value[1]:
                league = key
                break

        higher_elo_count = await Player.find(Player.elo > elo).count()
        rank = higher_elo_count + 1
        
        wins = player_doc.wins
        avg_stars = player_doc.avg_stars
        avg_destruction = player_doc.avg_destruction
        opp_avg_stars = player_doc.opponent_avg_stars
        opp_avg_destruction = player_doc.opponent_avg_destruction

        recent = []
        sorted_matches = sorted(player_doc.matches, key=lambda m: int(m.id), reverse=True)
        for match in sorted_matches:
            if len(recent) >= 4:
                break
            
            if player_id not in match.players:
                continue
                
            p_stats = match.players[player_id]
            opponent_id = p_stats.opponent
            
            if opponent_id not in match.players:
                continue
            o_stats = match.players[opponent_id]

            elo_change_str = f"{LOSS_EMOJI}{p_stats.elo_change}"
            if p_stats.elo_change > 0:
                elo_change_str = f"{WIN_EMOJI}+{p_stats.elo_change}"

            match_ts = ipy.Timestamp.fromtimestamp(match.time).format(style=ipy.TimestampStyles.RelativeTime)
            recent.append((opponent_id, f"({elo_change_str}) {match_ts}\n"
                                        f"{p_stats.stars}{STAR_EMOJI} {p_stats.destruction}% - "
                                        f"{o_stats.stars}{STAR_EMOJI} {o_stats.destruction}%\n"))

        guild = await member_guild(ctx)
        opponents = await asyncio.gather(*(guild.fetch_member(opponent_id) for opponent_id, _ in recent))
        recent_matches = "".join(
            f"vs. {mention_user(opponent, opponent_id, mention_type='Player')} {details}"
            for (opponent_id, details), opponent in zip(recent, opponents)
        )
        recent_matches = "No recent matches." if not recent_matches else recent_matches

        rank_str = f"#{rank} in the server"
        winrate = wins / total_matches if total_matches else 0

        try:
            player_account: coc.Player = await self.coc_client.get_player(player_doc.tag)
        except coc.errors.NotFound:
            await ctx.send(f"{ERROR_EMOJI} The player's linked in-game account seems to be invalid, "
                           f"please ask a staff to renew it!", ephemeral=True)

            return
        except coc.errors.Maintenance:
            await ctx.send(f"{ERROR_EMOJI} Please try again after the maintenance break has ended.",
                           ephemeral=True)
            return

        seasons = await Season.find_all().sort("season_number").to_list()
        select_options = [
            ipy.StringSelectOption(label="All Seasons", value="All Seasons"),
            ipy.StringSelectOption(label="Current Season", value="Current Season"),
        ]
        for season_doc in seasons:
            select_options.append(
                ipy.StringSelectOption(
                    label=f"Season {season_doc.season_number}", value=str(season_doc.season_number)
                )
            )

        stats_select = ipy.StringSelectMenu(
            *select_options,
            placeholder="🔍 Select a season",
            custom_id=f"stats_select|{user.id}"
        )

        from elobot.features.tokens.wallets import ensure_wallet, wallet_summary
        from elobot.features.finals.types import FinalError
        try:
            available, _, _ = await wallet_summary(await ensure_wallet(player_id))
            tokens_str = f"{available:,} {TOKEN_EMOJI}"
        except FinalError as error:
            tokens_str = f"Unavailable: {error}"

        embed = ipy.Embed(
            title=f"**{user}'s Profile**",
            description=f"[{player_account.name} ({player_account.tag})]({player_account.share_link})\n"
                        f"**Elo:** {elo}\n"
                        f"**Highest Elo:** {player_doc.highest_elo}\n"
                        f"**League:** {LEAGUE_EMOJIS[league]} {league}\n"
                        f"**Rank:** {rank_str}\n"
                        f"**Tokens:** {tokens_str}",
            fields=[
                ipy.EmbedField(
                    name="__**Statistics (All Seasons)**__",
                    value=f"**Wins:** {wins}\n"
                          f"**Total Matches:** {total_matches}\n"
                          f"**Winrate:** {winrate * 100:.2f}%\n"
                          f"**Stars:** {avg_stars:.2f}{STAR_EMOJI} - {opp_avg_stars:.2f}{STAR_EMOJI}\n"
                          f"**Destruction:** {avg_destruction:.2f}% - {opp_avg_destruction:.2f}%"
                ),
                ipy.EmbedField(
                    name="__**Recent Matches**__",
                    value=recent_matches
                )
            ],
            thumbnail=ipy.EmbedAttachment(url=user.avatar.url),
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        await ctx.send(embed=embed, components=stats_select, ephemeral=hidden)

    @ipy.component_callback(re.compile(r"^stats_select\|\d+$"))
    async def stats_select(self, ctx: ipy.ComponentContext):
        if ctx.author and int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)

            return

        _, player_id = ctx.custom_id.split("|")
        p_doc = await Player.find_one(Player.user_id == player_id, fetch_links=True)

        seasons = await Season.find_all().sort("season_number").to_list()
        scope, season_start, season_end = interpret_season_string(seasons, ctx.values[0])
        
        player_matches = get_season_matches(p_doc.matches, season_start, season_end)

        wins = get_wins(player_matches, player_id)
        winrate = get_winrate(player_matches, player_id)
        avg_stars = get_avg_stars(player_matches, player_id)
        avg_destruction = get_avg_destruction(player_matches, player_id)
        opp_avg_stars = get_opponent_avg_stars(player_matches, player_id)
        opp_avg_destruction = get_opponent_avg_destruction(player_matches, player_id)
        total_matches = len(player_matches)

        ctx.message.embeds[0].fields[0] = ipy.EmbedField(
            name=f"__**Statistics ({scope})**__",
            value=f"**Wins:** {wins}\n"
                  f"**Total Matches:** {total_matches}\n"
                  f"**Winrate:** {winrate:.2f}%\n"
                  f"**Stars:** {avg_stars:.2f}{STAR_EMOJI} - {opp_avg_stars:.2f}{STAR_EMOJI}\n"
                  f"**Destruction:** {avg_destruction:.2f}% - {opp_avg_destruction:.2f}%"
        )

        await ctx.edit_origin(embeds=ctx.message.embeds)

    matches_group = base.group(name="matches", description="")

    @matches_group.subcommand(sub_cmd_name="view", sub_cmd_description="To view all matches of a player as a list")
    @ipy.slash_option(
        name="user",
        description="A user registered in the elo system",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def player_matches_view(self, ctx: ipy.SlashContext, user: ipy.User, hidden: bool = False):
        p_doc = await Player.find_one(Player.user_id == str(user.id), fetch_links=True)
        if not p_doc:
            await ctx.send(f"{ERROR_EMOJI} {user.mention} is not registered in the elo system.",
                           ephemeral=hidden)

            return

        if not p_doc.matches:
            await ctx.send(f"{ERROR_EMOJI} {user.mention} has not done any matches.",
                           ephemeral=hidden)

            return

        index = 0

        sorted_matches = sorted(p_doc.matches, key=lambda m: int(m.id), reverse=True)

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"match_left|{user.id}|{index}",
            disabled=index == 0,
        )
        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"match_right|{user.id}|{index}",
            disabled=(index == len(p_doc.matches) - 1)
        )
        action_row = ipy.ActionRow(left_button, right_button)

        match = sorted_matches[index]
        embed = await construct_match_embed(self.bot, ctx.guild, match.id, str(user.id))

        await ctx.send(embed=embed, ephemeral=hidden, components=action_row)

    @ipy.component_callback(re.compile(r"^match_right\|\w+\|\d+$"))
    async def match_right_button(self, ctx: ipy.ComponentContext):
        if int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)

            return

        _, user_id, index = ctx.custom_id.split("|")
        index = int(index) + 1
        
        p_doc = await Player.find_one(Player.user_id == user_id, fetch_links=True)

        sorted_matches = sorted(p_doc.matches, key=lambda m: int(m.id), reverse=True)

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"match_left|{user_id}|{index}",
            disabled=index == 0,
        )
        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"match_right|{user_id}|{index}",
            disabled=(index == len(p_doc.matches) - 1)
        )
        action_row = ipy.ActionRow(left_button, right_button)

        match = sorted_matches[index]
        embed = await construct_match_embed(self.bot, ctx.guild, match.id, user_id)

        await ctx.edit_origin(embed=embed, components=action_row)

    @ipy.component_callback(re.compile(r"^match_left\|\w+\|\d+$"))
    async def match_left_button(self, ctx: ipy.ComponentContext):
        if int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)

            return

        _, user_id, index = ctx.custom_id.split("|")
        index = int(index) - 1
        
        p_doc = await Player.find_one(Player.user_id == user_id, fetch_links=True)

        sorted_matches = sorted(p_doc.matches, key=lambda m: int(m.id), reverse=True)

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"match_left|{user_id}|{index}",
            disabled=index == 0,
        )
        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"match_right|{user_id}|{index}",
            disabled=(index == len(p_doc.matches) - 1)
        )
        action_row = ipy.ActionRow(left_button, right_button)

        match = sorted_matches[index]
        embed = await construct_match_embed(self.bot, ctx.guild, match.id, user_id)

        await ctx.edit_origin(embed=embed, components=action_row)

    @matches_group.subcommand(sub_cmd_name="search", sub_cmd_description="To search a match of a player")
    @ipy.slash_option(
        name="user",
        description="A user registered in the elo system",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="match_id",
        description="The ID of match to check",
        opt_type=ipy.OptionType.STRING,
        autocomplete=True,
        required=True
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def player_matches_search(self, ctx: ipy.SlashContext, user: ipy.User, match_id: str, hidden: bool = False):
        try:
            embed = await construct_match_embed(self.bot, ctx.guild, match_id, str(user.id))
        except KeyError:
            await ctx.send(
                f"{ERROR_EMOJI} The match data is inconsistent and cannot be displayed. "
                f"Make sure the match actually exists for this player.",
                ephemeral=True
            )

            return

        await ctx.send(embed=embed, ephemeral=hidden)

    @create_async_task
    @lock_files("match_id_choices.dict")
    async def fetch_all_match_id_choices(self, guild: ipy.Guild):
        return await publish_match_id_choices(self.bot, guild)

    @ipy.global_autocomplete(option_name="match_id")
    async def match_id_autocomplete(self, ctx: ipy.AutocompleteContext):
        choices = None
        if str(ctx.command.name) == "player":
            if "user" in ctx.kwargs:
                choices = self.bot.match_id_choices.get(_option_user_id(ctx.kwargs["user"]))
                if choices:
                    registered = await Player.get_pymongo_collection().find_one(
                        {"user_id": str(ctx.author.id)}, {"_id": 0, "user_id": 1}
                    )
                    if registered is None:
                        choices = None
        else:
            stored = self.bot.match_id_choices.get("all")
            if stored:
                await ctx.send(select_match_id_choices(stored, ctx.input_text))
            else:
                await ctx.send(await fallback_global_match_choices(self.bot, ctx.guild, ctx.input_text))
            return

        await ctx.send(select_match_id_choices(choices or [], ctx.input_text))

    @base.subcommand(sub_cmd_name="hitrates", sub_cmd_description="To check hit-rates of a player")
    @ipy.slash_option(
        name="user",
        description="A user registered in the elo system",
        opt_type=ipy.OptionType.USER,
        required=True
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
    async def player_hitrates(self, ctx: ipy.SlashContext, user: ipy.User, season: str = "All Seasons",
                              hidden: bool = False):
        await ctx.defer(ephemeral=hidden)

        p_doc = await Player.find_one(Player.user_id == str(user.id), fetch_links=True)
        if not p_doc:
            await ctx.send(f"{ERROR_EMOJI} The player is not registered in the elo system.",
                           ephemeral=True)

            return

        if not p_doc.matches:
            await ctx.send(f"{ERROR_EMOJI} The player has not played any matches yet.",
                           ephemeral=True)

            return

        seasons = await Season.find_all().sort("season_number").to_list()
        scope, season_start, season_end = interpret_season_string(seasons, season)
        
        # p_doc.matches contains RecordedMatch objects
        player_matches = get_season_matches(p_doc.matches, season_start, season_end)

        district_stars = {}
        district_destruction = {}
        district_count = {}
        
        player_id = str(user.id)
        
        for match in player_matches:
            if player_id not in match.players:
                continue
                
            p_stats = match.players[player_id]
            
            for district, district_data in p_stats.districts.items():
                district_stars.setdefault(district, []).append(district_data.stars)
                district_destruction.setdefault(district, []).append(district_data.destruction)

        for district, stars in district_stars.items():
            district_count[district] = len(stars)

        sorted_keys = sorted(district_stars.keys(), key=lambda k: DISTRICT_ORDER[k])
        hitrates = {}
        for key in sorted_keys:
            hitrates[key] = (f"`{district_count[key]}` → "
                             f"{(sum(district_stars[key]) / len(district_stars[key])):.2f}{STAR_EMOJI} "
                             f"{(sum(district_destruction[key]) / len(district_destruction[key])):.2f}%")

        hitrates_str = ""
        for key, value in hitrates.items():
            hitrates_str += f"{DISTRICT_EMOJI} **{key}**\n{value}\n\n"

        embed = ipy.Embed(
            title=f"**{user}'s Hitrates**",
            description=hitrates_str,
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )
        embed.add_field(name="**Scope**", value=scope)

        seasons = await Season.find_all().sort("season_number").to_list()
        select_options = [
            ipy.StringSelectOption(label="All Seasons", value="All Seasons"),
            ipy.StringSelectOption(label="Current Season", value="Current Season"),
        ]
        for season_doc in seasons:
            select_options.append(
                ipy.StringSelectOption(
                    label=f"Season {season_doc.season_number}", value=str(season_doc.season_number)
                )
            )

        hitrates_select = ipy.StringSelectMenu(
            *select_options,
            placeholder="🔍 Select a season",
            custom_id=f"hitrates_select|{user.id}"
        )

        await ctx.send(embed=embed, components=hitrates_select, ephemeral=hidden)

    @ipy.component_callback(re.compile(r"^hitrates_select\|\d+$"))
    async def hitrates_select(self, ctx: ipy.ComponentContext):
        if ctx.author and int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)

            return

        _, player_id = ctx.custom_id.split("|")
        p_doc = await Player.find_one(Player.user_id == player_id, fetch_links=True)

        seasons = await Season.find_all().sort("season_number").to_list()
        scope, season_start, season_end = interpret_season_string(seasons, ctx.values[0])
        
        player_matches = get_season_matches(p_doc.matches, season_start, season_end)

        district_stars = {}
        district_destruction = {}
        district_count = {}
        
        for match in player_matches:
            if player_id not in match.players:
                continue
                
            p_stats = match.players[player_id]
            
            for district, district_data in p_stats.districts.items():
                district_stars.setdefault(district, []).append(district_data.stars)
                district_destruction.setdefault(district, []).append(district_data.destruction)

        for district, stars in district_stars.items():
            district_count[district] = len(stars)

        sorted_keys = sorted(district_stars.keys(), key=lambda k: DISTRICT_ORDER[k])
        hitrates = {}
        for key in sorted_keys:
            hitrates[key] = (f"{(sum(district_stars[key]) / len(district_stars[key])):.2f}{STAR_EMOJI} "
                             f"{(sum(district_destruction[key]) / len(district_destruction[key])):.2f}%\n"
                             f"Total hits: `{district_count[key]}`")

        hitrates_str = ""
        for key, value in hitrates.items():
            hitrates_str += f"{DISTRICT_EMOJI} **{key}**\n{value}\n\n"

        ctx.message.embeds[0].description = hitrates_str
        ctx.message.embeds[0].fields[0].value = scope

        await ctx.edit_origin(embeds=ctx.message.embeds)

    @ipy.modal_callback("elo_registration_modal")
    async def elo_registration_modal(self, ctx: ipy.ModalContext):
        await self.player_register(ctx)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Player commands loaded")

        if int(self.bot.user.id) == BOT_ID:
            guild = await self.bot.fetch_guild(MAIN_GUILD)
            snapshot = await self.fetch_all_match_id_choices(guild)
            await hydrate_match_id_choice_names(self.bot, snapshot)
