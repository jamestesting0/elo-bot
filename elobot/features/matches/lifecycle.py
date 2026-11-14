import asyncio
import copy
import random
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime

import interactions as ipy

from elobot.config.emojis import CROSS_EMOJI, DISTRICT_EMOJI, IMPORTANT_EMOJI, TICK_EMOJI
from elobot.config.game import BASE_SUBMISSION_THRESHOLD
from elobot.config.ids import COMMAND_CHANNEL, MATCH_CHANNEL, STAFF_ROLE
from elobot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT, ORDINAL_REPRESENTATIONS
from elobot.db import BaseLink, Match, Participant, Player
from elobot.db.player_models import QueueDistrictSelection
from elobot.discord.mentions import mention_user
from elobot.features.finals.types import is_final_match
from elobot.features.matches.base_selection import get_base_quality
from elobot.features.matches.cleanup import delete_unreserved_match
from elobot.features.matches.thread_messages import (
    ThreadMessageBindingError,
    attach_thread_message,
    find_match_thread_announcement,
)
from elobot.features.queue.policy import QUEUE_OPERATION_LOCK
from elobot.features.queue.state import remove_matched_queue_attempts


def get_district_options(
        wizard_valley_available: bool,
        capital_peak_available: bool,
) -> list[list[str]]:
    district_options = [
        ["Barbarian Camp", "Skeleton Park"],
        ["Balloon Lagoon", "Builder's Workshop", "Golem Quarry"],
        ["Dragon Cliffs", "Goblin Mines"],
    ]
    if wizard_valley_available:
        district_options[0].append("Wizard Valley")
    if capital_peak_available:
        district_options[2].append("Capital Peak")
    return district_options


async def send_warnings(bot: ipy.Client, token):
    match = await Match.find_one(Match.token == token)
    thread = await bot.fetch_channel(match.thread)
    await thread.send(f"<@&{STAFF_ROLE}>\n{IMPORTANT_EMOJI} The upcoming match is missing a host! 1v1 helpers "
                      f"please check if you got time for it.")


def build_district_confirmation_modal(
        token: str,
        wizard_valley_available: bool = False,
        capital_peak_available: bool = False,
) -> dict:
    district_options = get_district_options(wizard_valley_available, capital_peak_available)
    labels = [
        {
            "type": 18,
            "label": f"Select the {index}{'st' if index == 1 else 'nd' if index == 2 else 'rd'} district",
            "component": {
                "type": 3,
                "custom_id": f"inline_district|{token}|{index - 1}",
                "options": [{"label": district, "value": district} for district in options],
                "required": True,
            },
        }
        for index, options in enumerate(district_options, start=1)
    ]

    return {
        "type": int(ipy.CallbackType.MODAL),
        "data": {
            "title": "Confirm Match",
            "custom_id": f"confirm_occupied_match_modal|{token}",
            "components": labels,
        },
    }


async def initiate_match(
        bot: ipy.Client, player1: Player, player2: Player,
        *, on_inserted: Callable[[], Awaitable[None]] | None = None,
        queue_selections: tuple[QueueDistrictSelection | None, QueueDistrictSelection | None] = (None, None),
):
    user1 = await bot.fetch_user(player1.user_id)
    user2 = await bot.fetch_user(player2.user_id)

    wizard_valley_available = player1.elo < 1200 and player2.elo < 1200
    capital_peak_available = player1.capital_peak_enabled and player2.capital_peak_enabled
    district_options = get_district_options(wizard_valley_available, capital_peak_available)
    p1_districts = [
        district if district in district_options[index] else None
        for index, district in enumerate(player1.district_presets)
    ]
    p2_districts = [
        district if district in district_options[index] else None
        for index, district in enumerate(player2.district_presets)
    ]
    for player, districts, selection in zip((player1, player2), (p1_districts, p2_districts), queue_selections):
        if selection is not None and selection.confirmed:
            continue
        if player.district_presets[0] == "Wizard Valley" and not wizard_valley_available:
            secondary = player.secondary_district_presets[0]
            districts[0] = secondary if secondary in district_options[0] else random.choice(district_options[0])
        if player.district_presets[2] == "Capital Peak" and not capital_peak_available:
            secondary = player.secondary_district_presets[2]
            districts[2] = secondary if secondary in district_options[2] else random.choice(district_options[2])

    token = secrets.token_hex(8)

    p1 = Participant(user_id=str(user1.id), districts=p1_districts, confirmed=all(p1_districts))
    p2 = Participant(user_id=str(user2.id), districts=p2_districts, confirmed=all(p2_districts))

    for participant, selection in zip((p1, p2), queue_selections):
        if selection is None:
            continue
        participant.queue_attempt_id = selection.attempt_id
        if selection.confirmed or not participant.confirmed:
            participant.districts = [
                district if district in options else random.choice(options) if district else None
                for district, options in zip(selection.districts, district_options)
            ]
            participant.confirmed = selection.confirmed and all(participant.districts)
        participant.queue_districts_locked = selection.confirmed

    primary_base_quality, secondary_base_quality = get_base_quality(player1, player2)

    matched_time = datetime.now().timestamp()
    for participant in (p1, p2):
        if participant.confirmed:
            participant.confirmed_at = matched_time
            participant.confirmation_method = "ready"

    # create and insert a match document
    match = Match(
        token=token,
        matched_time=matched_time,
        confirmation_tracking=True,
        base_submission=player1.elo >= BASE_SUBMISSION_THRESHOLD and player2.elo >= BASE_SUBMISSION_THRESHOLD,
        primary_base_quality=primary_base_quality,
        secondary_base_quality=secondary_base_quality,
        wizard_valley_available=wizard_valley_available,
        capital_peak_available=capital_peak_available,
        participants=[p1, p2],
    )
    await match.insert()
    if on_inserted is not None:
        await on_inserted()

    embed = ipy.Embed(
        title="**Match Found**",
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR,
    )

    district_selects = [
        ipy.StringSelectMenu(
            *(ipy.StringSelectOption(label=district, value=district) for district in options),
            placeholder=f"{index + 1}️⃣ Select the {ORDINAL_REPRESENTATIONS[index]} district",
            custom_id=f"district_select|{token}|{index}",
        )
        for index, options in enumerate(district_options)
    ]

    cancel_button = ipy.Button(
        style=ipy.ButtonStyle.DANGER,
        label="Cancel",
        custom_id=f"match_cancel|{token}",
        emoji=CROSS_EMOJI,
    )

    confirm_button = ipy.Button(
        style=ipy.ButtonStyle.SUCCESS,
        label="Confirm",
        custom_id=f"match_confirm|{token}",
        emoji=TICK_EMOJI,
    )

    view_opponent_button = ipy.Button(
        style=ipy.ButtonStyle.SECONDARY,
        label="Reveal Opponent",
        custom_id=f"reveal_opponent|{token}",
        emoji="🔍",
    )

    submit_bases_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Submit Bases",
        custom_id=f"submit_bases|{token}",
        emoji="📥",
    )

    # send DM to each player
    for participant, user in zip([p1, p2], [user1, user2]):
        message = "A match has been found, please read the information on the embed!"

        # build the last action row based on base_submission
        action_buttons = [cancel_button, view_opponent_button]
        if match.base_submission:
            action_buttons.append(submit_bases_button)

        # copy select menus so they don't share state
        components = [
            ipy.ActionRow(copy.deepcopy(district_selects[0])),
            ipy.ActionRow(copy.deepcopy(district_selects[1])),
            ipy.ActionRow(copy.deepcopy(district_selects[2])),
            ipy.ActionRow(*action_buttons),
        ]

        # Mark preset districts if any
        for i, d in enumerate(participant.districts):
            if d:
                components[i].components[0].placeholder = f"✅ {d} (Preset)"
            if participant.queue_districts_locked:
                components[i].components[0].disabled = True

        if participant.confirmed:
            embed.description = (
                "The bot will automatically confirm for your opponent if they do not confirm or cancel within 12 hours."
            )
            embed.fields = [
                ipy.EmbedField(
                    name="__**Instruction**__",
                    value=(
                        "1. Select your preferred districts for the match.\n"
                        "2. Confirm the match by pressing \"Confirm\".\n"
                        "3. Once both players have confirmed the match, "
                        f"a thread will be made in <#{MATCH_CHANNEL}>."
                    ),
                )
            ]
        else:
            components[3] = ipy.ActionRow(cancel_button, confirm_button)
            embed.description = (
                "The bot will automatically confirm for you if you do not confirm or cancel within 12 hours."
            )
            embed.fields = [
                ipy.EmbedField(
                    name="__**Instruction**__",
                    value=(
                        "1. Select your preferred districts for the match.\n"
                        "2. Confirm the match by pressing \"Confirm\".\n"
                        "3. Once both players have confirmed the match, "
                        f"a thread will be made in <#{MATCH_CHANNEL}>."
                    ),
                )
            ]

        try:
            await user.send(message, embed=embed, components=components)
        except ipy.errors.Forbidden:
            command_channel = await bot.fetch_channel(COMMAND_CHANNEL)
            await command_channel.send(
                f"{user.mention}\n{message}\n\n"
                "*This message is sent here, because you have disabled direct messages.*",
                embed=embed,
                components=components,
            )

    # Auto-confirm if both participants already confirmed
    if p1.confirmed and p2.confirmed:
        await confirm_match(bot, match.token)


async def cancel_match(bot: ipy.Client, token):
    async with QUEUE_OPERATION_LOCK:
        match = await Match.find_one(Match.token == token)
        if not match or any(participant.confirmed for participant in match.participants):
            return
        await remove_matched_queue_attempts(match)
        if not await delete_unreserved_match(match, pre_thread=True):
            return
    player1 = await bot.fetch_user(match.participants[0].user_id)
    player2 = await bot.fetch_user(match.participants[1].user_id)

    try:
        await player1.send(f"{IMPORTANT_EMOJI} Your match against {player2.mention} has been canceled. This is "
                           f"either due to both of you not confirming for more than 12 hours or the match failed to "
                           f"initiate due to some unknown bug.")
    except ipy.errors.Forbidden:
        pass

    try:
        await player2.send(f"{IMPORTANT_EMOJI} Your match against {player1.mention} has been canceled. This is "
                           f"either due to both of you not confirming for more than 12 hours or the match failed to "
                           f"initiate due to some unknown bug.")
    except ipy.errors.Forbidden:
        pass


# per-token locks to prevent concurrent confirm_match calls on the same match
_confirm_match_locks: dict[str, asyncio.Lock] = {}


class MatchChannelUnavailable(RuntimeError):
    """A channel lookup returned no channel without confirming its deletion."""


async def confirm_match(bot: ipy.Client, token: str) -> str:
    async with _confirm_match_locks.setdefault(token, asyncio.Lock()):
        match = await Match.find_one(Match.token == token)
        if not match:
            raise ValueError(f"No match found with token={token}")

        if match.thread:
            if (any(match.times) or any(match.old_msgs) or match.is_autowin
                    or any(any(participant.result) for participant in match.participants)):
                return str(match.thread)
            thread = await bot.fetch_channel(match.thread)
            if thread is None:
                raise MatchChannelUnavailable(f"Match {token} thread {match.thread} is unavailable")
            msg = await find_match_thread_announcement(thread, match, bot.user.id)
            if msg is not None:
                if match.thread_message_id != int(msg.id) and not await attach_thread_message(match, int(msg.id)):
                    raise ThreadMessageBindingError(token)
                if (is_final_match(match) and match.base_submission and not match.final_superseded
                        and match.final_result_id is None and not any(
                            component.custom_id == f"submit_bases|{token}"
                            for row in msg.components for component in row.components)):
                    await msg.edit(components=[ipy.ActionRow(ipy.Button(
                        style=ipy.ButtonStyle.GRAY, label="Submit Bases", custom_id=f"submit_bases|{token}",
                        emoji=ipy.PartialEmoji(name="📥"))), *msg.components])
                if f"<@&{STAFF_ROLE}>" not in msg.content:
                    await msg.edit(content=f"{msg.content}\n<@&{STAFF_ROLE}>")
                if not msg.pinned:
                    await msg.pin()
                return str(match.thread)
        else:
            channel = await bot.fetch_channel(match.routing_channel_id if is_final_match(match)
                                             else MATCH_CHANNEL)
            if channel is None:
                raise MatchChannelUnavailable(f"Match {token} parent channel is unavailable")

        # fetch players from participants
        p1, p2 = match.participants
        player1 = await bot.fetch_user(int(p1.user_id))
        player2 = await bot.fetch_user(int(p2.user_id))

        if not match.thread:
            if is_final_match(match):
                claimed = await match.get_pymongo_collection().update_one(
                    {"_id": match.id, "thread": None, "final_thread_creation_claimed": False},
                    {"$set": {"final_thread_creation_claimed": True}},
                )
                if not claimed.matched_count:
                    raise ThreadMessageBindingError(token)
                match.final_thread_creation_claimed = True
            # create private thread
            thread_name = f"🟡 {player1} vs. {player2}".replace("@", "")
            thread = await channel.create_private_thread(
                name=thread_name,
                invitable=False,
                auto_archive_duration=ipy.AutoArchiveDuration.ONE_WEEK,
            )

            # randomly assign districts from participants
            seed = random.choice([
                [0, 0, 1], [0, 1, 1], [0, 1, 0],
                [1, 0, 0], [1, 0, 1], [1, 1, 0],
            ])
            matched_players = [p1, p2]

            district1 = matched_players[seed[0]].districts[0]
            district2 = matched_players[seed[1]].districts[1]
            district3 = matched_players[seed[2]].districts[2]

            if not is_final_match(match):
                match.final_districts = [district1, district2, district3]
            match.thread = str(thread.id)

            # only load preset bases when base submission is enabled
            if match.base_submission:
                for participant in match.participants:

                    player = await Player.find_one(Player.user_id == participant.user_id, fetch_links=True)

                    # for each assigned district, check if a valid preset base exists
                    new_bases = []
                    for district in match.final_districts:
                        base = player.base_presets.get(district)
                        if (
                                base
                                and base.expiration_date
                                and base.expiration_date >= datetime.now().timestamp()
                        ):
                            new_bases.append(
                                BaseLink(
                                    link=base.link,
                                    image=base.image_url,
                                    expiration_date=base.expiration_date,
                                )
                            )
                        else:
                            new_bases.append(None)

                    participant.bases = new_bases

            # save updated match
            await match.save_changes()

        embed = ipy.Embed(
            title=f"**{player1} vs. {player2}**",
            description=(
                f"- Ping <@&{STAFF_ROLE}> to start the match, and keep an eye on the chat for the next 10 minutes."
                + ("\n- Submit bases (</match base submit:1288780187042971700>) 10 minutes prior to the match."
                   if match.base_submission else "")
            ),
            fields=[
                ipy.EmbedField(
                    name="**Assigned Districts**",
                    value="\n".join(f"{index}. {DISTRICT_EMOJI} {district}"
                                    for index, district in enumerate(match.final_districts, 1)),
                    inline=False,
                ),
                ipy.EmbedField(
                    name="**Scheduling**",
                    value="Use [HammerTime](https://old.hammertime.cyou/en-GB) to "
                          "generate a Discord timestamp and send in chat.",
                    inline=False,
                ),
                ipy.EmbedField(
                    name="**Live Matches**",
                    value="By default the players attack independently. To attack together, "
                          "please inform each other and the staff!",
                    inline=False,
                ),
                ipy.EmbedField(
                    name="**Rules**",
                    value="1. 2 attacks per district.\n"
                          "2. 3 miners maximum per attack.\n"
                          + ("3. The host manages match timing." if is_final_match(match)
                             else "3. Finish the attacks in 30 minutes."),
                    inline=False,
                ),
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR,
        )

        if not match.base_submission:
            notes = [f"- Base submission is disabled for matches between players under {BASE_SUBMISSION_THRESHOLD} Elo."]
        else:
            names = {p.user_id: mention_user(player) for p, player in zip(match.participants, (player1, player2))}
            notes = [f"- {names[p.user_id]} has preset bases." for p in match.participants if any(p.bases)]
            notes.extend(f"- {names[p.user_id]} wish to submit bases." for p in match.participants if p.submit_bases)

        if notes:
            embed.add_field(name="**Notes**", value="\n".join(notes))

        submit_button = ipy.Button(
            style=ipy.ButtonStyle.GRAY,
            label="Submit Result",
            custom_id=f"submit_button|{token}",
            emoji=ipy.PartialEmoji(name="📥"),
        )

        controls = [submit_button]
        if is_final_match(match) and match.base_submission:
            controls.insert(0, ipy.Button(style=ipy.ButtonStyle.GRAY, label="Submit Bases",
                custom_id=f"submit_bases|{token}", emoji=ipy.PartialEmoji(name="📥")))

        # Send a match thread message
        msg = await thread.send(
            f"{player1.mention}\n{player2.mention}",
            embed=embed,
            components=[ipy.ActionRow(*controls)],
            nonce=f"match|{token}", enforce_nonce=True,
        )
        if not await attach_thread_message(match, int(msg.id)):
            raise ThreadMessageBindingError(token)
        await msg.edit(content=f"{msg.content}\n<@&{STAFF_ROLE}>")  # avoids ping
        await msg.pin()

        return str(thread.id)
