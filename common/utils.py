import asyncio
import os
import math
import re

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import coc
import interactions
from PIL import UnidentifiedImageError
from PIL import Image
from interactions import Client, User, Member
import validators
import json

import aiohttp
import aiofiles

from common.constants import BASE_STORE_SLOTS, K, C, WIN_FACTOR, LOSS_FACTOR, PLAYER_LINK_PREFIX, BASE_TIERS, MAIN_GUILD
from common.base_store.messages import StoredBaseMessageRetirement, retire_stored_base_message
from common.base_store.legacy import resolve_stored_base_message_ids
from database_models import Clan, Match, Participant, Player, Season


class ComponentsV2ModalContext(interactions.ModalContext):
    @classmethod
    def from_dict(cls, client, payload):
        instance = super(interactions.ModalContext, cls).from_dict(client, payload)
        responses = {}
        for container in payload["data"]["components"]:
            component = container.get("component") or container["components"][0]
            value = component.get("values", component.get("value"))
            responses[component["custom_id"]] = value

        instance.responses = responses
        instance.kwargs = responses
        instance.custom_id = payload["data"]["custom_id"]
        instance.edit_origin = False
        return instance


def is_raid_weekend():
    now = datetime.utcnow()
    return coc.utils.get_raid_weekend_start(now) <= now <= coc.utils.get_raid_weekend_end(now)


def latest_completed_raid_weekend_end_timestamp() -> int:
    now = datetime.utcnow()
    raid_end = coc.utils.get_raid_weekend_end(now)
    if raid_end > now:
        raid_end -= timedelta(days=7)
    return int(raid_end.replace(tzinfo=timezone.utc).timestamp())


def is_unmapped_base_store_reservation(match: Match, participant_index: int) -> bool:
    participant = match.participants[participant_index]
    if not getattr(participant, "opponent_bases_stored", False):
        return False
    if getattr(participant, "base_store", None):
        return False

    times = getattr(match, "times", None)
    return not isinstance(times, (list, tuple)) or participant_index >= len(times) or not times[participant_index]


def get_participant_base_store(participant: Participant) -> tuple[str | None, str | None]:
    base_store = getattr(participant, "base_store", None)
    if isinstance(base_store, dict):
        clan_tag = base_store.get("clan_tag")
        slot = base_store.get("slot")
    else:
        clan_tag = getattr(base_store, "clan_tag", None)
        slot = getattr(base_store, "slot", None)

    if not isinstance(clan_tag, str) or not isinstance(slot, str):
        return None, None

    return clan_tag, slot


def collect_base_store_slot_owners(matches: list[Match]) -> dict[str, dict[str, tuple[str, str]]]:
    slot_owners: dict[str, dict[str, tuple[str, str]]] = {}
    for match in matches:
        thread_id = getattr(match, "thread", None)
        if not thread_id:
            continue

        for participant in getattr(match, "participants", []) or []:
            clan_tag, slot = get_participant_base_store(participant)
            user_id = getattr(participant, "user_id", None)
            if clan_tag and slot in BASE_STORE_SLOTS and user_id:
                slot_owners.setdefault(clan_tag, {}).setdefault(slot, (str(thread_id), str(user_id)))

    return slot_owners


def format_base_store_slot_status(
        clan_tag: str,
        slot_owners: dict[str, dict[str, tuple[str, str]]],
) -> str:
    clan_slot_owners = slot_owners.get(clan_tag, {})
    lines = []
    for slot in BASE_STORE_SLOTS:
        owner = clan_slot_owners.get(slot)
        if not owner:
            continue

        thread_id, user_id = owner
        lines.append(f"{slot}: <#{thread_id}> <@{user_id}>")
    return "\n".join(lines)


def format_clan_availability_status(
        clan: Clan,
        slot_owners: dict[str, dict[str, tuple[str, str]]],
) -> str:
    slot_status = format_base_store_slot_status(clan.tag, slot_owners)
    return "\n".join(filter(None, (clan.availability_str, slot_status)))


async def clear_all_base_store_slots(*, bot: Client | None = None) -> None:
    cutoff = latest_completed_raid_weekend_end_timestamp()
    matches = await Match.find().to_list()
    for match in matches:
        for index, participant in enumerate(getattr(match, "participants", []) or []):
            store = participant.base_store
            if store is None and not is_unmapped_base_store_reservation(match, index):
                continue
            if store is not None and store.stored_at is not None and store.stored_at > cutoff:
                continue
            message_ids = await resolve_stored_base_message_ids(bot, match, index)
            if message_ids is None:
                continue
            for message_id in message_ids:
                await retire_stored_base_message(bot, StoredBaseMessageRetirement(match.thread, message_id, "releasing"))
            if await match.clear_base_store(index, reset_stored_flag=True):
                for message_id in message_ids:
                    await retire_stored_base_message(
                        bot, StoredBaseMessageRetirement(
                            match.thread, message_id, "raid_weekend_ended",
                            store.reservation_id if store and store.is_live else None,
                        ),
                    )

    remaining = await Match.find().to_list()
    occupied_slots = collect_base_store_slot_owners(remaining)
    blocks_all_slots = any(
        is_unmapped_base_store_reservation(match, index)
        for match in remaining if match.thread
        for index in range(len(match.participants))
    )
    clans = await Clan.find_all().to_list()
    for clan in clans:
        available_slots = [] if blocks_all_slots else [
            slot for slot in BASE_STORE_SLOTS if slot not in occupied_slots.get(clan.tag, {})
        ]
        if list(clan.available_slots or []) != available_slots:
            clan.available_slots = available_slots
            await clan.save_changes()


def parse_commands(bot: Client, input_text, scope: int = 0):
    pattern = r'`/([a-zA-Z]+(?:\s[a-zA-Z]+)*)\??`'
    return re.sub(pattern, lambda m: bot.mention_command(f"{m.group(1)}", scope), input_text)


def get_extensions(ext_folder: str) -> list[str]:
    extensions = [
        f"{ext_folder}.{f[:-3]}"
        for f in os.listdir(ext_folder)
        if f.endswith("_commands.py") and not f.startswith("_")
    ]

    return extensions


def get_stars(string: str) -> int:
    return int(string.split(".")[0])


def get_destruction(string: str) -> int:
    return int(string.split(".")[1])


def extract_integer(input_string: str, index: int = 0) -> int | None:
    if not input_string:
        return 0

    if isinstance(input_string, int):
        return input_string

    match = re.findall(r'\d+', input_string)
    if match:
        return int(match[index])

    return 0


def calculate_elo(elo: int, opponent_elo: int, stars: list[int], destruction: list[int], opponent_stars: list[int],
                  opponent_destruction: list[int], is_challenge: bool) -> int:
    q_p1 = 10 ** (elo / C)
    q_p2 = 10 ** (opponent_elo / C)
    expected_outcome = q_p1 / (q_p1 + q_p2)
    p_p1 = calculate_points(stars, destruction)
    p_p2 = calculate_points(opponent_stars, opponent_destruction)
    sign = calculate_sign(stars, destruction, opponent_stars, opponent_destruction)
    try:
        actual_outcome = p_p1 / (p_p1 + p_p2)
    except ZeroDivisionError:
        actual_outcome = 0.5

    min_loss = 5
    max_loss = 40
    min_gain = 10
    max_gain = 45
    if is_challenge:
        min_loss = 0
        max_loss = 30
        min_gain = 0
        max_gain = 35

    final_elo = round(sign * 25 + elo + K * (actual_outcome - expected_outcome))
    if sign > 0:
        return min(elo + max_gain, max(elo + min_gain, final_elo))
    elif sign < 0:
        return max(elo - max_loss, min(elo - min_loss, final_elo))
    else:
        return min(elo + 10, max(elo - 10, final_elo))


def calculate_points(stars: list[int], destruction: list[int]) -> int:
    points = 0
    for i in range(0, 3):
        if stars[i] == 0:
            points += math.floor(destruction[0] / 10)
        elif stars[i] == 1:
            points += 5 + math.floor(destruction[0] / 9)
        elif stars[i] == 2:
            points += 16 + math.floor((destruction[0] - 50) / 3)
        else:
            points += 40

    return points


def calculate_sign(stars: list[int], destruction: list[int], opponent_stars: list[int],
                   opponent_destruction: list[int]) -> int:
    p1_score = sum(stars) * 1000 + sum(destruction)
    p2_score = sum(opponent_stars) * 1000 + sum(opponent_destruction)

    if p1_score == p2_score:
        return 0
    elif p1_score > p2_score:
        return 1 * WIN_FACTOR
    else:
        return -1 * LOSS_FACTOR


def validate_dict(data: dict):
    if not isinstance(data, dict):
        return False

    player_result_keys = {
        "stars": int,
        "destruction": int,
        "opponent": str,
        "eloChange": int,
        "won": bool,
        "districts": list,
    }

    district_keys = {
        "name": str,
        "stars": int,
        "destruction": int
    }

    for i in range(2):
        player_id = list(data.keys())[i]
        player_result = data[player_id]
        for key, expected_type in player_result_keys.items():
            if key not in player_result or not isinstance(player_result[key], expected_type):
                return False

        for district in player_result["districts"]:
            if not isinstance(district, dict):
                return False

            for key, expected_type in district_keys.items():
                if key not in district or not isinstance(district[key], expected_type):
                    return False

    return True


def generate_player_link(tag: str):
    return PLAYER_LINK_PREFIX + tag.replace("#", "")


def mention_user(user: User | Member | None, user_id: int | str = 0,
                 mention_type: Literal["Player", "Builder", "Unknown Builder", "Helper"] = "Player"):
    if user:
        return f"**[{user.display_name}](<https://discordapp.com/users/{user.id}>)**"

    if mention_type == "Player":
        name_filler = "❌Invalid Player"
    elif mention_type == "Builder":
        name_filler = "❓Unknown Builder"
    else:
        name_filler = "❌Invalid Helper"

    return f"**[{name_filler}](<https://discordapp.com/users/{user_id}>)**"


async def member_guild(ctx: interactions.BaseContext) -> interactions.Guild:
    """Server to look up members in; DMs and other server-less interactions use the main server."""
    return ctx.guild or await ctx.bot.fetch_guild(MAIN_GUILD)


class InvalidSeasonSelectionError(ValueError):
    pass


def get_season_by_number(seasons: list[Season], season_number: int) -> Season | None:
    return next((season for season in seasons if season.season_number == season_number), None)


def interpret_season_string(seasons: list[Season], season: str) -> tuple[str, int, int]:
    if season == "All Seasons":
        scope = season
        season_start = -1
        season_end = datetime.now().timestamp()
    elif season == "Current Season":
        scope = season
        season_start = seasons[-1].end if seasons else -1
        season_end = datetime.now().timestamp()
    else:
        if not season.isdigit():
            raise InvalidSeasonSelectionError(f"\"{season}\" is not a valid season string.")

        season_doc = get_season_by_number(seasons, int(season))
        if season_doc is None:
            raise InvalidSeasonSelectionError(f"Season {season} does not exist.")

        scope = f"Season {season_doc.season_number}"
        season_start = int(season_doc.start)
        season_end = int(season_doc.end)

    return scope, season_start, season_end


def get_base_quality(player_doc: Player, opponent_doc: Player, match_doc: Optional[Match] = None):
    if match_doc and match_doc.primary_base_quality and match_doc.secondary_base_quality:
        return match_doc.primary_base_quality, match_doc.secondary_base_quality

    player_quality = "Excellent"
    opponent_quality = "Excellent"
    for quality, (lower, upper) in BASE_TIERS.items():
        if lower <= player_doc.elo <= upper:
            player_quality = quality

        if lower <= opponent_doc.elo <= upper:
            opponent_quality = quality

    if player_doc.elo >= opponent_doc.elo:
        primary_quality = player_quality
        secondary_quality = opponent_quality
    else:
        primary_quality = opponent_quality
        secondary_quality = player_quality

    return primary_quality, secondary_quality


async def download_file(url: str, filename: str) -> bool:
    max_size = 10 * 1024 * 1024  # 8 MB

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status != 200:
                return False

            # temporary save of original file
            with open(filename, 'wb') as f:
                async for chunk in response.content.iter_chunked(1024):
                    f.write(chunk)

    # check size
    if os.path.getsize(filename) > max_size:
        try:
            with Image.open(filename) as img:
                quality = 90
                # save as webp
                img.save(filename, "WEBP", quality=quality, optimize=True)

                # if still over 10MB, keep reducing quality
                while os.path.getsize(filename) > max_size and quality > 20:
                    quality -= 10
                    img.save(filename, "WEBP", quality=quality, optimize=True)

            return True
        except (OSError, IOError, UnidentifiedImageError):
            return False

    return True


async def delete_file(file_path: str):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, os.remove, file_path)


async def is_url_image(image_url: str) -> True:
    if not validators.url(image_url):
        return False

    image_formats = ("image/png", "image/jpeg", "image/jpg")
    async with aiohttp.ClientSession() as session:
        async with session.head(image_url) as r:
            content_type = r.headers.get("content-type")
            if content_type in image_formats:
                return True
            return False


async def read_json(file_name: str, dir_path: str = "data/"):
    for i in range(3):
        try:
            async with aiofiles.open(f"{dir_path}{file_name}", 'r') as file:
                data = await file.read()
                return json.loads(data)
        except json.decoder.JSONDecodeError:
            await asyncio.sleep(0.1)


async def write_json(file_name: str, data, dir_path: str = "data/"):
    async with aiofiles.open(f"{dir_path}{file_name}", 'w') as file:
        await file.write(json.dumps(data, indent=4))


async def fetch_member_otherwise_user(guild: interactions.Guild, bot: interactions.Client, user_id: str):
    member = None
    if guild:
        try:
            member = await guild.fetch_member(user_id)
        except interactions.errors.DiscordError as error:
            if error.status != 503:
                raise
    if not member:
        member = await bot.fetch_user(user_id)

    return member
