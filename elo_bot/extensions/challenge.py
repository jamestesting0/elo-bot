import secrets

from interactions import utils

import interactions as ipy
from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT, ORDINAL_REPRESENTATIONS
from elo_bot.config.ids import COMMAND_CHANNEL, ELO_BLACKLIST_ROLE, GUILD_IDS, INFO_CHANNEL, LVL3_ROLES
from elo_bot.config.emojis import CROSS_EMOJI, ERROR_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI, TICK_EMOJI
from elo_bot.discord.decorators import reset_cooldown_on_error
from elo_bot.features.matches.challenges import weekly_challenge_balance
from elo_bot.features.matches.cleanup import delete_unreserved_match
from elo_bot.db import Participant, Match, Player, RecordedMatch, MatchPlayerStats, OverrideCode, ServerState
import asyncio
import re
from datetime import datetime
from elo_bot.discord.mentions import mention_user
from elo_bot.features.queue.policy import QUEUE_OPERATION_LOCK
from elo_bot.features.matches.lifecycle import confirm_match, get_district_options
from elo_bot.features.matches.occupancy import is_player_occupied


CHALLENGE_ACCEPT_LOCK = asyncio.Lock()


def get_last_challenge(player_matches: list[RecordedMatch], player_id: str) -> MatchPlayerStats | None:
    # sort matches by ID to ensure the correct order
    sorted_matches = sorted(player_matches, key=lambda m: int(m.id))

    count = 0
    for match in reversed(sorted_matches):
        if count >= 5:
            return None

        if match.is_challenge:
            return match.players[player_id]

        count += 1

    return None


async def available_challenges(player: Player, now: float) -> int:
    """Deduct accepted challenges whose results are not yet in the player's history."""
    balance = weekly_challenge_balance(player.matches, now)
    recorded_ids = {match.id for match in player.matches}
    accepted_matches = await Match.find({
        "is_challenge": True,
        "participants.user_id": player.user_id,
        "participants": {"$not": {"$elemMatch": {"confirmed": {"$ne": True}}}},
    }).to_list()
    for match in accepted_matches:
        record_id = match.recorded_match_id
        if match.result_operation and match.result_operation.record:
            record_id = match.result_operation.record.id
        if record_id not in recorded_ids:
            balance = max(0, balance - 1)
    return balance


class ChallengeCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot

    base = ipy.SlashCommand(name="challenge", description="", scopes=GUILD_IDS)

    @base.subcommand(sub_cmd_name="send", sub_cmd_description="Send a challenge")
    @reset_cooldown_on_error
    @ipy.check(ipy.has_any_role(*LVL3_ROLES))
    @ipy.cooldown(ipy.Buckets.USER, 1, 10800)
    @ipy.slash_option(
        name="opponent",
        description="The player you want to challenge",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="district1",
        description="First district assigned",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Barbarian Camp", value="Barbarian Camp"),
            ipy.SlashCommandChoice(name="Skeleton Park", value="Skeleton Park"),
            ipy.SlashCommandChoice(name="Wizard Valley", value="Wizard Valley"),
        ],
        required=True
    )
    @ipy.slash_option(
        name="district2",
        description="Second district assigned",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Balloon Lagoon", value="Balloon Lagoon"),
            ipy.SlashCommandChoice(name="Builder's Workshop", value="Builder's Workshop"),
            ipy.SlashCommandChoice(name="Golem Quarry", value="Golem Quarry")
        ],
        required=True
    )
    @ipy.slash_option(
        name="district3",
        description="Third district assigned",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Dragon Cliffs", value="Dragon Cliffs"),
            ipy.SlashCommandChoice(name="Goblin Mines", value="Goblin Mines"),
            ipy.SlashCommandChoice(name="Capital Peak", value="Capital Peak"),
        ],
        required=True
    )
    @ipy.slash_option(
        name="message",
        description="A custom message to provoke your opponent?",
        opt_type=ipy.OptionType.STRING,
        required=False
    )
    @ipy.slash_option(
        name="override_code",
        description="A code to override Elo difference restriction",
        opt_type=ipy.OptionType.STRING,
        min_length=16,
        max_length=16,
        required=False
    )
    @ipy.slash_option(
        name="base_submission",
        description="Whether base submission is required",
        opt_type=ipy.OptionType.BOOLEAN,
        required=False
    )
    async def challenge_send(self, ctx: ipy.SlashContext, opponent: ipy.Member, district1: str, district2: str,
                             district3: str, message: str = None, override_code: str = None,
                             base_submission: bool = True):
        await ctx.defer(ephemeral=True)

        if ctx.author.has_role(ELO_BLACKLIST_ROLE):
            await ctx.send(f"{ERROR_EMOJI} You are currently banned from playing Elo. If you are unsure of why, "
                           f"please open a support ticket in <#{INFO_CHANNEL}>.",
                           ephemeral=True)

            return

        if opponent.has_role(ELO_BLACKLIST_ROLE):
            await ctx.send(f"{ERROR_EMOJI} {opponent.mention} is currently banned from playing Elo.",
                           ephemeral=True)

            return

        if (await ServerState.current()).queue_disabled:
            await ctx.send(f"{ERROR_EMOJI} Match-making is currently disabled due to season reset.",
                           ephemeral=True)

            return

        player = await Player.find_one(Player.user_id == str(ctx.author.id), fetch_links=True)
        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered in the elo system, "
                           f"please register using the command `/player register`.", ephemeral=True)
            await self.challenge_send.cooldown.reset(ctx)

            return

        opponent_player = await Player.find_one(Player.user_id == str(opponent.id), fetch_links=True)
        if not opponent_player:
            await ctx.send(f"{ERROR_EMOJI} You cannot challenge a user who is not registered in Elo.",
                           ephemeral=True)
            await self.challenge_send.cooldown.reset(ctx)

            return

        if str(opponent.id) == str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot challenge yourself in Elo.",
                           ephemeral=True)
            await self.challenge_send.cooldown.reset(ctx)

            return

        if not player.challenge:
            await ctx.send(f"{ERROR_EMOJI} You have challenge mode disabled. "
                           f"You can enable it with `/challenge enable`.",
                           ephemeral=True)
            await self.challenge_send.cooldown.reset(ctx)

            return

        if not opponent_player.challenge:
            await ctx.send(f"{ERROR_EMOJI} {opponent.mention} have challenge mode disabled.",
                           ephemeral=True)
            await self.challenge_send.cooldown.reset(ctx)

            return

        wizard_valley_available = player.elo < 1200 and opponent_player.elo < 1200
        capital_peak_available = player.capital_peak_enabled and opponent_player.capital_peak_enabled
        district_options = get_district_options(wizard_valley_available, capital_peak_available)
        districts = [district1, district2, district3]
        if any(district not in options for district, options in zip(districts, district_options)):
            await ctx.send(f"{ERROR_EMOJI} One or more districts are not available for this challenge.", ephemeral=True)
            await self.challenge_send.cooldown.reset(ctx)
            return

        has_override = await OverrideCode.is_valid("challenge", override_code)

        player_last_challenge = get_last_challenge(player.matches, str(ctx.author.id))
        if (player_last_challenge and player_last_challenge.opponent == str(opponent.id) and
            not has_override):
            await ctx.send(f"{ERROR_EMOJI} You have already challenged {opponent.mention} in your "
                           f"recent challenges.", ephemeral=True)
            await self.challenge_send.cooldown.reset(ctx)

            return

        opponent_last_challenge = get_last_challenge(opponent_player.matches, str(opponent.id))
        if (opponent_last_challenge and opponent_last_challenge.opponent == str(ctx.author.id) and
                not has_override):
            await ctx.send(f"{ERROR_EMOJI} The player you are challenging was {opponent.mention} challenged "
                           f"by you recently.", ephemeral=True)
            await self.challenge_send.cooldown.reset(ctx)

            return

        now = datetime.now().timestamp()
        if not has_override and not await available_challenges(player, now):
            await ctx.send(f"{ERROR_EMOJI} You need an unused normal ranked match from the past "
                           f"7 days to send a challenge. Available challenges: `0`.", ephemeral=True)
            await self.challenge_send.cooldown.reset(ctx)

            return

        if not has_override and not await available_challenges(opponent_player, now):
            await ctx.send(f"{ERROR_EMOJI} {opponent.mention} needs an unused normal ranked match from the past "
                           f"7 days to receive a challenge. Available challenges: `0`.", ephemeral=True)
            await self.challenge_send.cooldown.reset(ctx)

            return

        current_week = datetime.now().strftime("%U")
        current_year = datetime.now().year

        # ensure last_challenged has 2 elements
        if len(player.last_challenged) < 2:
            player.last_challenged = ["", ""]

        if all(record == f"{current_year}.{current_week}" for record in player.last_challenged):
            await ctx.send(f"{ERROR_EMOJI} You may only challenge twice per week.",
                           ephemeral=True)
            await self.challenge_send.cooldown.reset(ctx)

            return

        player_occupied = await is_player_occupied(str(ctx.author.id))
        if player_occupied:
            await ctx.send(
                f"{ERROR_EMOJI} {player_occupied.message}",
                components=player_occupied.component,
                ephemeral=True,
            )
            await self.challenge_send.cooldown.reset(ctx)

            return

        if str(ctx.author.id) in opponent_player.blocked_users:
            await ctx.send(f"{ERROR_EMOJI} You are blocked by {opponent.mention}. You can ask them to unblock you "
                           f"via by running `/challenge unblock`.",
                           ephemeral=True)
            await self.challenge_send.cooldown.reset(ctx)

            return

        # find any match where both players are present
        candidates = await Match.find({
            "thread": {"$ne": None, "$exists": True},
            "participants": {
                "$all": [
                    {"$elemMatch": {"user_id": str(ctx.author.id)}},
                    {"$elemMatch": {"user_id": str(opponent.id)}}
                ]
            }
        }).to_list()

        for match in candidates:
            thread = await self.bot.fetch_channel(match.thread)
            if not thread or thread.name.startswith("🔴"):
                continue

            await ctx.send(
                f"{ERROR_EMOJI} You already have an on-going match against {opponent.mention} "
                f"in <#{match.thread}>!.",
                ephemeral=True
            )
            await self.challenge_send.cooldown.reset(ctx)
            return

        embed = ipy.Embed(
            title="**YOU HAVE BEEN CHALLENGED!**",
            description="This match affects your Elo. The challenge expires in 24 hours.\n"
                        "Disable challenges with `/challenge disable` or block a player with `/challenge block`.",
            fields=[
                ipy.EmbedField(
                    name="**Challenger**",
                    value=mention_user(ctx.author)
                ),
                ipy.EmbedField(
                    name="**Base Submission**",
                    value="Enabled" if base_submission else "Disabled"
                )
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        if (player.elo - opponent_player.elo) >= 200:
            embed.add_field(
                name="**Warning**",
                value="The challenger's Elo is significantly lower, you might not gain any Elo even if you win!"
            )

        if message:
            embed.add_field(
                name="Message from the Challenger",
                value=message
            )

        if has_override and not await OverrideCode.consume("challenge", override_code):
            await ctx.send(
                f"{ERROR_EMOJI} The override code has expired or was already used.",
                ephemeral=True,
            )
            await self.challenge_send.cooldown.reset(ctx)
            return

        # write the match data
        token = secrets.token_hex(8)
        challenger = Participant(
            user_id=str(ctx.author.id),
            districts=[district1, district2, district3],
            confirmed=True,
            confirmation_method="ready",
        )
        opponent_participant = Participant(
            user_id=str(opponent.id),
            confirmed=False
        )

        async with QUEUE_OPERATION_LOCK:
            occupied = await Match.find_one({
                "format": "elo_final_seven", "participants.user_id": {"$in": [challenger.user_id, opponent_participant.user_id]},
                "final_result_id": None, "final_superseded": {"$ne": True},
            })
            if occupied and occupied.format == "elo_final_seven":
                await ctx.send(f"{ERROR_EMOJI} An entrant has an unfinished Final match. Finish it before challenging.",
                               ephemeral=True)
                return
            matched_time = datetime.now().timestamp()
            challenger.confirmed_at = matched_time
            match = Match(
                token=token,
                matched_time=matched_time,
                confirmation_tracking=True,
                is_war=True,
                is_challenge=True,
                challenge_activity_override=has_override,
                base_submission=base_submission,
                wizard_valley_available=wizard_valley_available,
                capital_peak_available=capital_peak_available,
                participants=[challenger, opponent_participant],
            )
            await match.insert()

        district_selects = [
            ipy.StringSelectMenu(
                *(ipy.StringSelectOption(label=district, value=district) for district in options),
                placeholder=f"{index + 1}️⃣ Select the {ORDINAL_REPRESENTATIONS[index]} district",
                custom_id=f"district_select|{token}|{index}",
            )
            for index, options in enumerate(district_options)
        ]

        reject_button = ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Reject",
            custom_id=f"challenge_reject|{token}",
            emoji=CROSS_EMOJI
        )

        accept_button = ipy.Button(
            style=ipy.ButtonStyle.SUCCESS,
            label="Accept",
            custom_id=f"challenge_accept|{token}",
            emoji=TICK_EMOJI
        )

        components = [
            *(ipy.ActionRow(district_select) for district_select in district_selects),
            ipy.ActionRow(reject_button, accept_button)
        ]

        try:
            await opponent.send(embed=embed, components=components)
        except ipy.errors.Forbidden:
            command_channel = await self.bot.fetch_channel(COMMAND_CHANNEL)

            await command_channel.send(
                f"{player.mention}\n{message}\n\n"
                f"*This message is sent here, because you have disabled direct messages.*",
                embed=embed,
                components=components
            )

        cancel_button = ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Cancel",
            custom_id=f"challenge_cancel|{token}",
            emoji=CROSS_EMOJI
        )

        embed = ipy.Embed(
            title="**PENDING CHALLENGE!**",
            description=f"A match thread opens when your opponent accepts. "
                        f"You cannot use {self.bot.mention_command('match queue')} while this challenge is pending.\n"
                        f"Cancel at any time, or it expires in 24 hours.",
            fields=[ipy.EmbedField(name="**Opponent**", value=mention_user(opponent))],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        submit_bases_button = ipy.Button(
            style=ipy.ButtonStyle.GRAY,
            label="Submit Bases",
            emoji="📥",
            custom_id=f"submit_bases|{token}",
        )

        await ctx.author.send(embed=embed, components=ipy.ActionRow(cancel_button, submit_bases_button))

        await ctx.send(f"{SUCCESS_EMOJI} Your challenge is successfully sent!", ephemeral=True)

    @ipy.component_callback(re.compile(r"^challenge_accept\|\w+$"))
    async def challenge_accept_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        if ctx.channel and str(ctx.channel.id) == COMMAND_CHANNEL and not ctx.message.contains_mention(ctx.author):
            await ctx.send(
                f"{ERROR_EMOJI} You cannot interact with another user's components.",
                ephemeral=True
            )
            return

        _, token = ctx.custom_id.split("|")

        match = await Match.find_one(Match.token == token)
        if match is None:
            await ctx.send(
                f"{ERROR_EMOJI} It seems that this match has been cancelled.",
                ephemeral=True
            )
            return

        participant = next((p for p in match.participants if p.user_id == str(ctx.author.id)), None)

        if participant is None:
            await ctx.send(f"{ERROR_EMOJI} This challenge is not awaiting your acceptance.", ephemeral=True)
            return

        # require all 3 districts to be chosen
        for i, dist in enumerate(participant.districts):
            if not dist:
                await ctx.send(
                    f"{ERROR_EMOJI} Please select a district for the "
                    f"{i + 1}{'st' if i == 0 else 'nd' if i == 1 else 'rd'} select menu.",
                    ephemeral=True
                )
                return

        async with CHALLENGE_ACCEPT_LOCK:
            match = await Match.find_one(Match.token == token)
            if match is None:
                await ctx.send(f"{ERROR_EMOJI} It seems that this match has been cancelled.", ephemeral=True)
                return
            participant = next((p for p in match.participants if p.user_id == str(ctx.author.id)), None)
            if participant is None or participant.confirmed:
                await ctx.send(f"{ERROR_EMOJI} This challenge is not awaiting your acceptance.", ephemeral=True)
                return
            now = datetime.now()
            for entrant in match.participants:
                player = await Player.find_one(Player.user_id == entrant.user_id, fetch_links=True)
                if not player or (not match.challenge_activity_override and
                                  not await available_challenges(player, now.timestamp())):
                    await ctx.send(
                        f"{ERROR_EMOJI} <@{entrant.user_id}> needs an unused normal ranked match from the past "
                        f"7 days to accept this challenge. Available challenges: `0`.",
                        ephemeral=True,
                    )
                    return
                if entrant.user_id != str(ctx.author.id):
                    week = f"{now.year}.{now.strftime('%U')}"
                    if len(player.last_challenged) >= 2 and all(
                        record == week for record in player.last_challenged
                    ):
                        await ctx.send(f"{ERROR_EMOJI} The challenger has already used both challenges this week.",
                                       ephemeral=True)
                        return

            participant.confirmed = True
            if match.confirmation_tracking:
                participant.confirmed_at = datetime.now().timestamp()
                participant.confirmation_method = "manual"
            await match.save_changes()

            # update challenger last_challenged
            challenger_participant = next((p for p in match.participants if p.user_id != str(ctx.author.id)), None)
            if challenger_participant:
                challenger_player = await Player.find_one(Player.user_id == challenger_participant.user_id)
                if challenger_player:
                    if len(challenger_player.last_challenged) < 2:
                        challenger_player.last_challenged = ["", ""]

                    for i in range(2):
                        if challenger_player.last_challenged[i] != week:
                            challenger_player.last_challenged[i] = week
                            break
                    await challenger_player.save_changes()

        # replace the components with a Submit Bases button
        submit_bases_button = ipy.Button(
            style=ipy.ButtonStyle.GRAY,
            label="Submit Bases",
            emoji="📥",
            custom_id=f"submit_bases|{token}",
        )
        await ctx.message.edit(components=submit_bases_button)

        # confirm the match and get thread ID
        thread_id = await confirm_match(self.bot, token)

        await ctx.send(
            f"{SUCCESS_EMOJI} The match is now initiated, please determine the match "
            f"timing with your opponent in <#{thread_id}>",
            ephemeral=True
        )

    @ipy.component_callback(re.compile(r"^challenge_reject\|\w+$"))
    async def challenge_reject_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        if ctx.channel and str(ctx.channel.id) == COMMAND_CHANNEL and not ctx.message.contains_mention(ctx.author):
            await ctx.send(
                f"{ERROR_EMOJI} You cannot interact with another user's components.",
                ephemeral=True,
            )
            return

        _, token = ctx.custom_id.split("|")

        match = await Match.find_one(Match.token == token)
        if match is None:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} It seems that this match is invalidated.", ephemeral=True)
            return

        # ask for confirmation
        cancel_button = ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Cancel",
            custom_id="cancel",
            emoji=CROSS_EMOJI,
        )
        confirm_button = ipy.Button(
            style=ipy.ButtonStyle.SUCCESS,
            label="Confirm",
            custom_id="confirm",
            emoji=TICK_EMOJI,
        )
        button_actionrow = ipy.ActionRow(cancel_button, confirm_button)

        msg = await ctx.send(
            "Are you sure that you would like to **reject** the challenge?\n\n"
            "*Note: you cannot undo this action.*",
            components=button_actionrow,
            ephemeral=True,
        )

        try:
            res: ipy.ComponentContext = (
                await self.bot.wait_for_component(
                    components=button_actionrow, messages=int(msg.id), timeout=180
                )
            ).ctx
        except asyncio.TimeoutError:
            await ctx.delete(ctx.message)
            return

        # handle confirm / cancel
        if res.custom_id == "confirm":
            await res.defer(ephemeral=True)
            async with CHALLENGE_ACCEPT_LOCK:
                deleted = await delete_unreserved_match(match, pre_thread=True)
            if not deleted:
                await res.send(
                    f"{ERROR_EMOJI} The challenge changed while rejection was being confirmed. It was not rejected.",
                    ephemeral=True,
                )
                return

            # notify both players
            for participant in match.participants:
                try:
                    user = await self.bot.fetch_user(int(participant.user_id))
                    if str(user.id) != str(ctx.author.id):
                        await user.send(
                            f"{IMPORTANT_EMOJI} Unfortunately, {ctx.author.mention} has decided to "
                            f"**reject** your challenge."
                        )
                    else:
                        await user.send(f"{IMPORTANT_EMOJI} You have **rejected** the challenge.")
                except ipy.errors.Forbidden:
                    pass

            # disable the components on the original message
            for actionrow in ctx.message.components:
                for component in actionrow.components:
                    component.disabled = True
            await ctx.message.edit(components=ctx.message.components)
            await ctx.edit_origin(components=ipy.utils.misc_utils.disable_components(button_actionrow))

            await res.send(f"{SUCCESS_EMOJI} The action is recorded.", ephemeral=True)

        if res.custom_id == "cancel":
            await ctx.edit_origin(components=ipy.utils.misc_utils.disable_components(button_actionrow))
            await res.send(f"{SUCCESS_EMOJI} The action has been cancelled.", ephemeral=True)

    @ipy.component_callback(re.compile(r"^challenge_cancel\|\w+$"))
    async def challenge_cancel_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        _, token = ctx.custom_id.split("|")

        match = await Match.find_one(Match.token == token)
        if match is None:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} It seems that this match is invalidated.", ephemeral=True)
            return

        if all(p.confirmed for p in match.participants):
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(
                f"{ERROR_EMOJI} The challenge is already accepted, is too late to cancel now.",
                ephemeral=True
            )
            return

        cancel_button = ipy.Button(
            style=ipy.ButtonStyle.DANGER, label="Cancel", custom_id="cancel", emoji=CROSS_EMOJI
        )
        confirm_button = ipy.Button(
            style=ipy.ButtonStyle.SUCCESS, label="Confirm", custom_id="confirm", emoji=TICK_EMOJI
        )
        button_actionrow = ipy.ActionRow(cancel_button, confirm_button)

        msg = await ctx.send(
            "Are you sure that you would like to **reject** the challenge?\n\n"
            "*Note: you cannot undo this action.*",
            components=button_actionrow,
            ephemeral=True,
        )

        try:
            res: ipy.ComponentContext = (
                await self.bot.wait_for_component(components=button_actionrow, messages=int(msg.id), timeout=180)
            ).ctx
        except asyncio.TimeoutError:
            await ctx.delete(ctx.message)
            return

        if res.custom_id == "confirm":
            await res.defer(ephemeral=True)
            async with CHALLENGE_ACCEPT_LOCK:
                deleted = await delete_unreserved_match(match, pre_thread=True)
            if not deleted:
                await res.send(
                    f"{ERROR_EMOJI} The challenge changed while cancellation was being confirmed. It was not cancelled.",
                    ephemeral=True,
                )
                return

            p1_id = int(match.participants[0].user_id)
            p2_id = int(match.participants[1].user_id)

            player1 = await self.bot.fetch_user(p1_id)
            player2 = await self.bot.fetch_user(p2_id)

            for user in (player1, player2):
                try:
                    if str(user.id) != str(ctx.author.id):
                        await user.send(
                            f"{IMPORTANT_EMOJI} {ctx.author.mention} "
                            f"has decided to **cancel** the challenge against you."
                        )
                    else:
                        await user.send(f"{IMPORTANT_EMOJI} You have **cancelled** the challenge.")
                except ipy.errors.Forbidden:
                    pass

            # disable components in both messages
            for actionrow in ctx.message.components:
                for component in actionrow.components:
                    component.disabled = True
            await ctx.message.edit(components=ctx.message.components)
            await ctx.edit_origin(components=ipy.utils.misc_utils.disable_components(button_actionrow))

            await res.send(f"{SUCCESS_EMOJI} The action is recorded.", ephemeral=True)

        if res.custom_id == "cancel":
            await ctx.edit_origin(components=ipy.utils.misc_utils.disable_components(button_actionrow))
            await res.send(f"{SUCCESS_EMOJI} The action has been cancelled.", ephemeral=True)

    @base.subcommand(sub_cmd_name="disable", sub_cmd_description="To disable the challenge module")
    @ipy.check(ipy.has_any_role(*LVL3_ROLES))
    async def challenge_disable(self, ctx: ipy.SlashContext):
        await ctx.defer(ephemeral=True)

        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered in the elo system, "
                           f"please register using the command `/player register`.", ephemeral=True)

            return

        player.challenge = False
        await player.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} You have disabled the challenge module, you can no "
                       f"longer send/receive challenges.", ephemeral=True)

    @base.subcommand(sub_cmd_name="enable", sub_cmd_description="To enable the challenge module")
    @ipy.check(ipy.has_any_role(*LVL3_ROLES))
    async def challenge_enable(self, ctx: ipy.SlashContext):
        await ctx.defer(ephemeral=True)

        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered in the elo system, "
                           f"please register using the command `/player register`.", ephemeral=True)

            return

        player.challenge = True
        await player.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} You have enabled the challenge module, you can now "
                       f"send/receive challenges.", ephemeral=True)

    @base.subcommand(sub_cmd_name="block", sub_cmd_description="To block a user from challenging you.")
    @ipy.check(ipy.has_any_role(*LVL3_ROLES))
    @ipy.slash_option(
        name="user",
        description="The user you want to block",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    async def challenge_block(self, ctx: ipy.SlashContext, user: ipy.User):
        await ctx.defer(ephemeral=True)

        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered in the elo system, "
                           f"please register using the command `/player register`.", ephemeral=True)

            return

        await player.update({"$addToSet": {"blocked_users": str(user.id)}})

        await ctx.send(f"{SUCCESS_EMOJI} You have blocked {user.mention}.", ephemeral=True)

    @base.subcommand(sub_cmd_name="unblock", sub_cmd_description="To unblock a user from challenging you.")
    @ipy.check(ipy.has_any_role(*LVL3_ROLES))
    @ipy.slash_option(
        name="user",
        description="The user you want to unblock",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    async def challenge_unblock(self, ctx: ipy.SlashContext, user: ipy.User):
        await ctx.defer(ephemeral=True)

        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered in the elo system, "
                           f"please register using the command `/player register`.", ephemeral=True)

            return

        await player.update({"$pull": {"blocked_users": str(user.id)}})

        await ctx.send(f"{SUCCESS_EMOJI} You have unblocked {user.mention}.", ephemeral=True)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=10))
    async def auto_reject_challenges(self):
        challenge_matches = await Match.find({Match.is_challenge: True}).to_list()

        for match in challenge_matches:
            challenger = match.participants[0]
            opponent = match.participants[1]

            # skip if opponent already confirmed
            if opponent.confirmed:
                continue

            now = datetime.now().timestamp()
            if int(now - match.matched_time) < 86400:  # 24h
                continue
            async with CHALLENGE_ACCEPT_LOCK:
                deleted = await delete_unreserved_match(match, pre_thread=True)
            if not deleted:
                continue

            # Notify challenger
            try:
                challenger_user = await self.bot.fetch_user(int(challenger.user_id))
                await challenger_user.send(
                    f"{IMPORTANT_EMOJI} Your opponent did **not** accept your challenge "
                    f"for 24 hours, therefore it is cancelled."
                )
            except ipy.errors.HTTPException:
                pass

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Challenge commands loaded")

        self.auto_reject_challenges.start()
