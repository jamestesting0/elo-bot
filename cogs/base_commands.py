import asyncio
import difflib
import logging
import math
import os
import re
import secrets
import tempfile
import time
from datetime import datetime
from typing import assert_never
from uuid import UUID

import anyio
import validators
from beanie import PydanticObjectId
from beanie.odm.queries.find import FindMany
from interactions import utils
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import DuplicateKeyError, PyMongoError

import elobot.features.hosting.war.helper_preserve as helper_preserve
from elobot.features.bases.screenshots import capture_base_screenshot
from elobot.features.base_submission.presentation import (
    ACCEPT_BUTTON_ID, ACCEPT_MODAL_ID, REJECT_BUTTON_ID, REJECT_MODAL_ID, build_resolution_embed,
    build_review_components, build_review_embed, mark_review_embed,
)
from elobot.features.base_submission.models import (
    AcceptDecision, RejectDecision, ResolutionDecision, ReviewStatus, SubmissionDraft,
)
from elobot.features.base_submission.parsing import parse_base_district
import interactions as ipy
from elobot.config.emojis import BASE_QUALITY_EMOJIS, ERROR_EMOJI, LOADING_EMOJI, RANK_EMOJIS, SUCCESS_EMOJI
from elobot.config.ids import (
    BASE_RENEWAL_LOGS_CHANNEL, BASE_RENEWAL_THREAD, BASE_SUBMISSION_CHANNEL, BOT_ID, COMMAND_CHANNEL, GUILD_IDS,
    LVL1_ROLES, LVL2_ROLES, LVL3_ROLES, MAIN_GUILD, WHITELISTED_STAFFS,
)
from elobot.config.game import BASE_TIERS, DISTRICT_CHOICES, DISTRICT_CODES_REVERSED
from elobot.config.presentation import (
    COLOR, FOOTER_IMAGE, FOOTER_TEXT, LB_ENTRIES_PER_PAGE, LINE_SEPERATOR_GIF, RED_COLOR, UNAVAILABLE_IMAGE,
)
from elobot.config.settings import HELPER_API_BASE_URL, HELPER_API_KEY
from elobot.db import BaseExtractionRun, BaseFingerprintTask, BaseLayout, PendingBaseSubmission, Player
from elobot.db.base_submission_models import SubmissionAcceptance, SubmissionRejection
from elobot.discord.pagination import InvalidPageError, build_page_button, build_page_jump_modal, parse_page_number
from elobot.integrations.files import delete_file
from elobot.discord.mentions import mention_user
from elobot.discord.members import member_guild
from elobot.discord.checks import has_any, has_any_id
from elobot.features.bases.fingerprints.images import attachment_identity, download_base_screenshot
from elobot.features.bases.fingerprints.ingestion import replace_source
from elobot.features.bases.fingerprints.removal import FingerprintRemovalProgress, remove_all_fingerprints, remove_fingerprint
from elobot.features.bases.fingerprints.search import (
    SearchSession, SearchSessions, can_search, show_result_image, find_matches, render_page, visibility_filter,
)
from elobot.features.bases.fingerprints.types import DISTRICTS, ExtractionState, FingerprintError, PIPELINE_ID
from elobot.features.base_submission.presentation import FIND_SIMILAR_BUTTON_ID

BASE_QUALITY_CHOICES = [
    ipy.SlashCommandChoice(name="Average", value="Average"),
    ipy.SlashCommandChoice(name="Good", value="Good"),
    ipy.SlashCommandChoice(name="Great", value="Great"),
    ipy.SlashCommandChoice(name="Excellent", value="Excellent")
]


class BaseAutocompleteRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    district: str
    builder_id: str | None = None


class BaseExtractionAutocompleteRow(BaseAutocompleteRow):
    id: PydanticObjectId = Field(alias="_id")
    link: str


def _fingerprint_status_emoji(status: str) -> ipy.PartialEmoji:
    if status in {"pending", "running"}:
        return LOADING_EMOJI
    if status in {"ready", "succeeded", "completed"}:
        return SUCCESS_EMOJI
    return ERROR_EMOJI


def _resolve_builder_id(bot: ipy.Client, builder_id: str) -> str | None:
    return None if builder_id == str(bot.user.id) else builder_id


def _builder_names(bot: ipy.Client) -> dict[str, str]:
    names = getattr(bot, "builder_names", None)
    if names is None:
        names = {}
        bot.builder_names = names
    return names


def _cached_user_name(user) -> str | None:
    if user is None:
        return None
    tag = getattr(user, "tag", None)
    if isinstance(tag, str) and tag:
        return tag
    username = getattr(user, "username", None)
    if isinstance(username, str) and username:
        return username if username.startswith("@") else f"@{username}"
    name = str(user)
    return name if name else None


def _builder_choice_label(bot: ipy.Client, guild: ipy.Guild | None, builder_id: str | None) -> str:
    if not builder_id:
        return "@anonymous"
    builder_id = str(builder_id)
    names = _builder_names(bot)
    cached_name = names.get(builder_id)
    if cached_name:
        return cached_name
    cached = guild.get_member(builder_id) if guild is not None else None
    if cached is None:
        cached = bot.get_user(builder_id)
    name = _cached_user_name(cached)
    if name:
        names[builder_id] = name
        return name
    return f"<@{builder_id}>"


def _parse_base_lb_title(title: str):
    """extract district and optional quality from embed title."""
    cleaned = title.replace("**", "")  # "Best Performing X Bases" or "Best Performing X Bases (Quality)"
    cleaned = cleaned.replace("Best Performing ", "")

    quality = None
    if cleaned.endswith(")"):
        # extract quality from parentheses
        base_part, quality = cleaned.rsplit("(", 1)
        quality = quality.rstrip(")")
        cleaned = base_part.strip()

    district = cleaned.replace(" Bases", "")
    return district, quality


async def construct_base_leaderboard_string(bot: ipy.Client, bases_sorted: list[BaseLayout], starting_index: int = 0):
    leaderboard = ""
    index = 0
    for base in bases_sorted[starting_index:]:
        if index >= 10:
            break

        if not base.builder_id:
            builder_mention = mention_user(None, base.builder_id, mention_type="Builder")
        else:
            builder = await bot.fetch_user(base.builder_id)
            builder_mention = mention_user(builder)

        rank_index = starting_index + index
        if rank_index > 2:
            emoji = f"**#{rank_index + 1}**"
        else:
            emoji = RANK_EMOJIS[rank_index]

        entries = len(base.records)
        if entries > 0:
            avg_stars = sum(record[1] for record in base.records) / entries
            avg_destruction = sum(record[2] for record in base.records) / entries
        else:
            avg_stars = 0
            avg_destruction = 0

        leaderboard += (f"{emoji} {builder_mention} `{entries}` → "
                        f"`{avg_stars:.2f}⭐ {avg_destruction:.2f}%`\n")

        index += 1

    return leaderboard


async def sort_bases(query: FindMany[BaseLayout]) -> list[BaseLayout]:
    query = query.find({"records.5": {"$exists": True}})

    bases = await query.to_list()

    bases_sorted = sorted(
        bases,
        key=lambda base: sum((r[1] * 1000 + r[2]) for r in base.records) / len(base.records),
        reverse=False
    )

    return bases_sorted


async def base_embed_names(guild: ipy.Guild, base: BaseLayout, embed_type: str) -> dict[str, str]:
    user_ids = {record[0] for record in base.records[-10:]} if embed_type == "Defense Log" else set()
    if base.builder_id:
        user_ids.add(base.builder_id)
    members = await asyncio.gather(*(guild.fetch_member(user_id) for user_id in user_ids))
    return {user_id: mention_user(member, user_id, mention_type="Builder" if user_id == base.builder_id else "Player")
            for user_id, member in zip(user_ids, members)}


def construct_base_embed(
        base: BaseLayout, embed_type: str = "Performance", *, names: dict[str, str],
) -> ipy.Embed:
    builder = base.builder_id
    quality = base.quality

    expiration_ts = ipy.Timestamp.fromtimestamp(base.expiration_date)
    expiration_ts = expiration_ts.format(style=ipy.TimestampStyles.RelativeTime)

    embed = ipy.Embed(
        title=f"**{base.district} Base**",
        description=f"{base.link}\n"
                    f"*Expires {expiration_ts}*",
        fields=[
            ipy.EmbedField(
                name="**Base Builder**",
                value=names[builder] if builder else "Unknown builder"
            ),
            ipy.EmbedField(
                name="**Base Quality**",
                value=f"{BASE_QUALITY_EMOJIS[quality]} {quality}"
            )
        ],
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR
    )

    embed.set_image(base.image_url)

    if embed_type == "Performance":
        total_destruction = 0
        total_1stars = 0
        total_2stars = 0
        total_3stars = 0
        total_entries = 0

        # iterate over the 'records' list from the model
        for record in base.records:
            total_entries += 1
            total_destruction += record[2]
            if record[1] == 1:
                total_1stars += 1
            if record[1] == 2:
                total_2stars += 1
            if record[1] == 3:
                total_3stars += 1

        embed.add_field(
            name="**Performance**",
            value=f"⭐ Rate: `{total_1stars * 100 / total_entries if total_entries else 0:.2f}%`\n"
                  f"⭐⭐ Rate: `{total_2stars * 100 / total_entries if total_entries else 0:.2f}%`\n"
                  f"⭐⭐⭐ Rate: `{total_3stars * 100 / total_entries if total_entries else 0:.2f}%`\n"
                  f"Average Destruction: `{total_destruction / total_entries if total_entries else 0:.2f}%`\n"
                  f"Total Entries: `{total_entries}`"
        )

    if embed_type == "Defense Log":
        log = ""
        count = 0
        for record in reversed(base.records):
            if count >= 10:
                break

            log += f"{names[record[0]]} → {record[1]}⭐ {record[2]}%"
            # check for optional timestamp in record tuple
            if len(record) == 4:
                ts = ipy.Timestamp.fromtimestamp(record[3]).format(ipy.TimestampStyles.RelativeTime)
                log += f" {ts}"
            log += "\n"
            count += 1

        embed.add_field(
            name="**Defense Log**",
            value=log if log else "No defenses so far"
        )

    return embed


async def _one_listed_base(
        query: FindMany[BaseLayout], index: int, *, total: int | None = None,
) -> tuple[BaseLayout | None, int]:
    if total is None:
        total = await query.count()
    if index < 0 or index >= total:
        return None, total

    matches = await query.sort("_id").skip(index).limit(1).to_list()
    return (matches[0] if matches else None), total


def _iter_message_components(components):
    rows = [components] if isinstance(components, ipy.ActionRow) else components or []
    for row in rows:
        yield from getattr(row, "components", [])


def _public_list_query(bot: ipy.Client, district: str, base_quality: str, builder_id: str):
    query = BaseLayout.find(BaseLayout.quality != "Private")
    if district != "Any":
        query = query.find(BaseLayout.district == district)
    if base_quality != "Any":
        query = query.find(BaseLayout.quality == base_quality)
    if builder_id != "Any":
        query = query.find(BaseLayout.builder_id == _resolve_builder_id(bot, builder_id))
    return query


def _parse_base_list_state(components):
    for component in _iter_message_components(components):
        custom_id = getattr(component, "custom_id", None) or ""
        if custom_id.startswith("base_left|") or custom_id.startswith("base_right|"):
            _, district, index, embed_type, base_quality, builder_id = custom_id.split("|")
            return district, int(index), embed_type, base_quality, builder_id
    raise ValueError("missing base list pagination state")


def _build_base_list_components(
        district: str, index: int, embed_type: str, base_quality: str, builder_id: str, total: int,
):
    district_select = ipy.StringSelectMenu(
        ipy.StringSelectOption(label="Any", value="Any"),
        *(ipy.StringSelectOption(label=str(choice.name), value=str(choice.value)) for choice in DISTRICT_CHOICES),
        placeholder="📊 Select the district type",
        custom_id="base_list_district_select",
    )
    embed_type_select = ipy.StringSelectMenu(
        ipy.StringSelectOption(label="Performance", value="Performance"),
        ipy.StringSelectOption(label="Defense Log", value="Defense Log"),
        placeholder="📄 Select the embed type",
        custom_id="base_list_embed_type_select",
    )
    left_button = ipy.Button(
        style=ipy.ButtonStyle.PRIMARY,
        label="◀️️",
        custom_id=f"base_left|{district}|{index}|{embed_type}|{base_quality}|{builder_id}",
        disabled=index == 0,
    )
    page_number_button = build_page_button(
        index + 1,
        total,
        f"base_page|{district}|{embed_type}|{base_quality}|{builder_id}",
    )
    right_button = ipy.Button(
        style=ipy.ButtonStyle.PRIMARY,
        label="▶️",
        custom_id=f"base_right|{district}|{index}|{embed_type}|{base_quality}|{builder_id}",
        disabled=(total - 1 <= index),
    )
    return [
        ipy.ActionRow(district_select),
        ipy.ActionRow(embed_type_select),
        ipy.ActionRow(left_button, page_number_button, right_button),
    ]


async def update_private_base_message(ctx: ipy.ComponentContext, district: str, index: int):
    query = BaseLayout.find(BaseLayout.quality == "Private", BaseLayout.builder_id == str(ctx.author.id))
    if district != "Any":
        query = query.find(BaseLayout.district == district)

    bases = await query.sort("_id").to_list()

    if not bases:
        await ctx.edit(ctx.message, content=f"{ERROR_EMOJI} No private bases found.", embeds=[], components=[])
        return

    index = min(max(index, 0), len(bases) - 1)
    base = bases[index]

    player = await Player.find_one(Player.user_id == str(ctx.author.id), fetch_links=True)
    is_preset = False
    if player and base.district in player.base_presets:
        preset_base = player.base_presets[base.district]
        is_preset = preset_base and preset_base.link == base.link

    left_button = ipy.Button(
        style=ipy.ButtonStyle.PRIMARY,
        label="◀️️",
        custom_id=f"p_base_left|{district}|{index - 1}",
        disabled=index == 0,
    )

    right_button = ipy.Button(
        style=ipy.ButtonStyle.PRIMARY,
        label="▶️",
        custom_id=f"p_base_right|{district}|{index + 1}",
        disabled=(len(bases) - 1 <= index)
    )

    preset_label = "Preset Remove" if is_preset else "Preset Add"
    preset_style = ipy.ButtonStyle.DANGER if is_preset else ipy.ButtonStyle.SUCCESS
    preset_action = "remove" if is_preset else "add"
    preset_button = ipy.Button(
        style=preset_style,
        label=preset_label,
        custom_id=f"p_base_preset|{district}|{index}|{base.id}|{preset_action}"
    )

    remove_button = ipy.Button(
        style=ipy.ButtonStyle.DANGER,
        label="Delete",
        custom_id=f"p_base_delete|{district}|{index}|{base.id}"
    )

    guild = await member_guild(ctx)
    embed = construct_base_embed(base, "Performance", names=await base_embed_names(guild, base, "Performance"))
    await ctx.edit(ctx.message, embed=embed, components=ipy.ActionRow(
        left_button, right_button, preset_button, remove_button
    ))


async def find_private_base_action(
        ctx: ipy.ComponentContext, district: str, base_id: str | None,
) -> BaseLayout | None:
    query: dict[str, str | PydanticObjectId] = {
        "quality": "Private", "builder_id": str(ctx.author.id),
    }
    if base_id:
        query["_id"] = PydanticObjectId(base_id)
    else:
        if not ctx.message.embeds or not ctx.message.embeds[0].description:
            return None
        query["link"] = ctx.message.embeds[0].description.splitlines()[0]
    if district != "Any":
        query["district"] = district
    return await BaseLayout.find_one(query)


async def clear_private_base_preset(player: Player, base: BaseLayout) -> None:
    field = f"base_presets.{base.district}"
    await player.get_pymongo_collection().update_one(
        {"_id": player.id, "$or": [{f"{field}._id": base.id}, {field: base.to_ref()}]},
        {"$set": {field: None}},
    )


class BaseCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot
        self.helper_api_base_url = HELPER_API_BASE_URL
        self.helper_api_key = HELPER_API_KEY
        self._review_locks: dict[int, anyio.Lock] = {}
        self._similarity = SearchSessions()
        self._review_refresh_cursor = None
        self._review_refresh_retry_after: dict[str, float] = {}

    async def send_base_renewal_log(self, base: BaseLayout) -> None:
        embed = ipy.Embed(
            title=f"{base.district} Base Renewed",
            description=base.link,
            color=COLOR,
        )
        embed.set_image(base.image_url)
        channel = await self.bot.fetch_channel(BASE_RENEWAL_LOGS_CHANNEL)
        await channel.send(embed=embed)

    async def send_base_renewal_failure_log(self, base: BaseLayout, notification: str) -> None:
        embed = ipy.Embed(
            title=f"{base.district} Base Renewal Failed",
            description=f"{base.link}\n\n{notification}",
            color=RED_COLOR,
        )
        embed.set_image(base.image_url)
        channel = await self.bot.fetch_channel(BASE_RENEWAL_LOGS_CHANNEL)
        await channel.send(embed=embed)

    base = ipy.SlashCommand(name="base", description="", scopes=GUILD_IDS)
    set_group = base.group(name="set", description="")

    def _extractable_bases(self, ctx):
        if str(ctx.author.id) == str(self.bot.owner.id):
            return BaseLayout.find_all()
        return BaseLayout.find({"$or": [{"quality": {"$ne": "Private"}}, {"builder_id": str(ctx.author.id)}]})

    @base.subcommand(sub_cmd_name="extract", sub_cmd_description="Regenerate one base fingerprint on CPU")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(name="base_link", description="Select a base or paste its link", opt_type=ipy.OptionType.STRING,
                      autocomplete=True, required=True)
    async def base_extract(self, ctx: ipy.SlashContext, base_link: str):
        await ctx.defer(ephemeral=True)
        try:
            identity = ({"_id": PydanticObjectId(base_link)} if PydanticObjectId.is_valid(base_link)
                        else {"link": base_link})
            base = await self._extractable_bases(ctx).find(identity).first_or_none()
            if base is None:
                await ctx.send(f"{ERROR_EMOJI} This base is unavailable or you do not have access to it.",
                               ephemeral=True)
                return
            state, created = await self.bot.base_fingerprints.request_extraction(base)
            status = "queued on CPU" if created else f"already {state.status}"
            await ctx.send(f"{LOADING_EMOJI} Fingerprint extraction {status} for `{base.district}`.\n"
                           f"Check `/base extract_status job_id:{state.request_id}`.",
                           ephemeral=True)
        except FingerprintError as error:
            await ctx.send(f"{ERROR_EMOJI} Fingerprint extraction unavailable: {error}.", ephemeral=True)
        except PyMongoError:
            await ctx.send(f"{ERROR_EMOJI} Could not confirm the saved extraction request. "
                           "Retry to retrieve its status.",
                           ephemeral=True)

    @base.subcommand(sub_cmd_name="extract_status", sub_cmd_description="Check an extraction job or bulk run")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(name="job_id", description="Job ID from /base extract or run ID from /base extract_all",
                      opt_type=ipy.OptionType.STRING, required=True)
    async def base_extract_status(self, ctx: ipy.SlashContext, job_id: str):
        await ctx.defer(ephemeral=True)
        try:
            job_id = str(UUID(job_id.strip()))
        except ValueError:
            await ctx.send(f"{ERROR_EMOJI} Invalid job or run ID. "
                           "Paste the ID returned by `/base extract` or `/base extract_all`.",
                           ephemeral=True)
            return
        try:
            run = await BaseExtractionRun.get(job_id)
            if run is not None:
                details = await self.bot.base_fingerprints.status(run)
                if run.notification_error:
                    details += f"\n{ERROR_EMOJI} Channel updates unavailable: `{run.notification_error}`."
                await ctx.send(f"{_fingerprint_status_emoji(run.status)} {details}", ephemeral=True)
                return
            task = await BaseFingerprintTask.get(job_id)
            identity = {"_id": task.base_id} if task else {"fingerprint_extraction.request_id": job_id}
            base = await self._extractable_bases(ctx).find(identity).first_or_none()
            if base is None:
                await ctx.send(f"{ERROR_EMOJI} Job or run not found, or its base is unavailable to you.",
                               ephemeral=True)
                return
            state = base.fingerprint_extraction
            status, error = (task.state, task.error_code) if task else (state.status, state.error_code)
            if state and state.request_id == job_id and state.completion_receipt:
                status, error = ("succeeded" if state.status == "ready" else state.status), state.error_code
            details = f"Job `{job_id}`: **{status}** ({base.district})."
            if error:
                details += f"\n{ERROR_EMOJI} Error: `{error}`."
            await ctx.send(f"{_fingerprint_status_emoji(status)} {details}", ephemeral=True)
        except PyMongoError:
            await ctx.send(f"{ERROR_EMOJI} Job status is temporarily unavailable. Please retry.", ephemeral=True)

    @base.subcommand(sub_cmd_name="unextract", sub_cmd_description="Remove one base fingerprint")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(name="base_link", description="Select a base or paste its link", opt_type=ipy.OptionType.STRING,
                      autocomplete=True, required=True)
    async def base_unextract(self, ctx: ipy.SlashContext, base_link: str) -> None:
        await ctx.defer(ephemeral=True)
        try:
            identity = ({"_id": PydanticObjectId(base_link)} if PydanticObjectId.is_valid(base_link)
                        else {"link": base_link})
            base = await self._extractable_bases(ctx).find(identity).first_or_none()
            if base is None:
                await ctx.send(f"{ERROR_EMOJI} This base is unavailable or you do not have access to it.", ephemeral=True)
                return
            result = await remove_fingerprint(base, self.bot.base_fingerprints.run_lock)
            status = "Fingerprint removed" if result.removed else "No fingerprint was stored"
            await ctx.send(f"{SUCCESS_EMOJI} {status} for `{base.district}`. Stopped {result.cancelled} pending extractions.",
                           ephemeral=True)
        except FingerprintError as error:
            await ctx.send(f"{ERROR_EMOJI} Fingerprint removal incomplete: {error}.", ephemeral=True)
        except PyMongoError:
            await ctx.send(f"{ERROR_EMOJI} Could not confirm fingerprint removal. Please retry.", ephemeral=True)

    @base.subcommand(sub_cmd_name="unextract_all", sub_cmd_description="Remove all saved base fingerprints")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    async def base_unextract_all(self, ctx: ipy.SlashContext) -> None:
        await ctx.defer(ephemeral=True)
        message = await ctx.send(f"{LOADING_EMOJI} **Removing base fingerprints...**", ephemeral=True)
        last_update = -math.inf
        progress_available = True

        async def report_progress(progress: FingerprintRemovalProgress) -> None:
            nonlocal last_update, progress_available
            if not progress_available:
                return
            now = time.monotonic()
            if now - last_update < 1:
                return
            last_update = now
            try:
                await ctx.edit(message, content=f"{LOADING_EMOJI} **Removing base fingerprints...**\n"
                               f"**{progress.processed:,} / {progress.total:,}** bases processed · "
                               f"**{progress.result.removed:,}** fingerprints removed")
            except ipy.errors.HTTPException as error:
                progress_available = False
                logging.warning("Base fingerprint removal progress unavailable: %s", type(error).__name__)

        try:
            result = await remove_all_fingerprints(self.bot.base_fingerprints.run_lock,
                                                   self.bot.base_fingerprints.gpu.close, report_progress)
            changed = f" {result.changed} bases changed during removal; retry to clear them." if result.changed else ""
            emoji = ERROR_EMOJI if result.changed else SUCCESS_EMOJI
            status = (f"{emoji} Removed {result.removed} fingerprints. Stopped {result.cancelled} pending extractions."
                      + changed)
        except FingerprintError as error:
            status = f"{ERROR_EMOJI} Fingerprint removal incomplete: {error}."
        await ctx.edit(message, content=status)

    @base_unextract.autocomplete(option_name="base_link")
    @base_extract.autocomplete(option_name="base_link")
    async def base_extract_autocomplete(self, ctx: ipy.AutocompleteContext):
        if not can_search(ctx, review=True):
            await ctx.send([])
            return
        bases = await self._extractable_bases(ctx).sort("_id").project(BaseExtractionAutocompleteRow).to_list()
        choices = {}
        for base in bases:
            builder = _builder_choice_label(self.bot, ctx.guild, base.builder_id)
            choices[f"{base.district} {builder} {base.link} {base.id}".lower()] = {
                "name": f"{base.district} by {builder} ({str(base.id)[-6:]})"[:100], "value": str(base.id),
            }
        if not ctx.input_text:
            await ctx.send(list(choices.values())[:25])
            return
        user_input = ctx.input_text.lower()
        matches = [key for key in choices if user_input in key][:25]
        if not matches:
            matches = difflib.get_close_matches(user_input, choices, n=25, cutoff=0.15)
        await ctx.send([choices[key] for key in matches])

    @base.subcommand(sub_cmd_name="extract_all",
                     sub_cmd_description="Extract non-private bases without saved fingerprints")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(name="batch_size", description="1 uses CPU; 2-32 require CUDA", opt_type=ipy.OptionType.INTEGER,
                      min_value=1, max_value=32)
    async def base_extract_all(self, ctx: ipy.SlashContext, batch_size: int = 1):
        await ctx.defer(ephemeral=True)
        try:
            previous = await BaseExtractionRun.find({"active": False}).sort("-created_at").first_or_none()
            run, created = await self.bot.base_fingerprints.begin_run(str(ctx.author.id), batch_size)
            status = await self.bot.base_fingerprints.status(run)
            status = f"{_fingerprint_status_emoji(run.status)} {status}"
            if created and previous:
                previous_status = await self.bot.base_fingerprints.status(previous)
                status = f"{_fingerprint_status_emoji(previous.status)} {previous_status}\n\n{status}"
            await ctx.send(status, ephemeral=True)
            if created:
                await self.bot.base_fingerprints.publish(run)
        except FingerprintError as error:
            await ctx.send(f"{ERROR_EMOJI} Fingerprint extraction unavailable: {error}.", ephemeral=True)
        except PyMongoError:
            await ctx.send(f"{ERROR_EMOJI} Could not confirm durable extraction work. "
                           "Retry to retrieve its status.", ephemeral=True)

    @base.subcommand(sub_cmd_name="find", sub_cmd_description="Find similar bases using a screenshot")
    @ipy.check(has_any(ipy.has_any_role(*LVL3_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(name="district", description="Screenshot district", opt_type=ipy.OptionType.STRING,
                      choices=[ipy.SlashCommandChoice(name=name, value=name) for name in DISTRICTS], required=True)
    @ipy.slash_option(name="screenshot", description="Base screenshot", opt_type=ipy.OptionType.ATTACHMENT,
                      required=True)
    @ipy.slash_option(name="top", description="Maximum results", opt_type=ipy.OptionType.INTEGER,
                      min_value=1, max_value=10)
    @ipy.slash_option(name="min_similarity", description="Minimum geometric similarity (%)",
                      opt_type=ipy.OptionType.NUMBER, min_value=0, max_value=100)
    @ipy.slash_option(name="fuzzy", description="Treat cannons and spears as the same type",
                      opt_type=ipy.OptionType.BOOLEAN)
    async def base_find(self, ctx: ipy.SlashContext, district: str, screenshot: ipy.Attachment,
                        top: int = 5, min_similarity: float = 80, fuzzy: bool = False):
        await ctx.defer(ephemeral=True)
        if district not in DISTRICTS or not 1 <= top <= 10 or not 0 <= min_similarity <= 100:
            await ctx.send(f"{ERROR_EMOJI} Invalid district, result count or similarity threshold.", ephemeral=True)
            return
        await self._find_similar(ctx, district, screenshot.url, top=top, minimum=min_similarity, fuzzy=fuzzy)

    @ipy.component_callback(FIND_SIMILAR_BUTTON_ID)
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    async def find_similar_submission(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)
        submission = await PendingBaseSubmission.get(str(ctx.message.id))
        if (submission is None or int(ctx.channel_id) != BASE_SUBMISSION_CHANNEL or
                submission.review_channel_id not in (None, str(ctx.channel_id))):
            await ctx.send(f"{ERROR_EMOJI} This saved review is unavailable or belongs to another channel.",
                           ephemeral=True)
            return
        if submission.resolution is not None:
            await ctx.send(f"{ERROR_EMOJI} This submission is resolved or acceptance is in progress. "
                           "New searches are closed.",
                           ephemeral=True)
            return
        await self._find_similar(ctx, submission.district, submission.image_url, top=10, minimum=80, fuzzy=True,
                                 submission=submission)

    async def _find_similar(self, ctx, district: str, image_url: str, *, top: int, minimum: float, fuzzy: bool,
                            submission: PendingBaseSubmission | None = None):
        review_id = submission.id if submission else None
        key = (str(ctx.author.id), review_id or "slash")
        if not can_search(ctx, review=bool(review_id)):
            await ctx.send(f"{ERROR_EMOJI} You no longer have access to this search.", ephemeral=True)
            return
        if key in self._similarity.pending or len(self._similarity.pending) >= 16:
            await ctx.send(f"{LOADING_EMOJI} A search is already pending. Please wait and try again.", ephemeral=True)
            return
        self._similarity.pending.add(key)
        try:
            service = self.bot.base_fingerprints
            query = await service.query(image_url, district, submission.image_attachment if submission else None)
            if not can_search(ctx, review=bool(review_id)):
                await ctx.send(f"{ERROR_EMOJI} You no longer have access to this search.", ephemeral=True)
                return
            matches, indexed, total = await find_matches(
                service, query, visibility_filter(ctx, district), top=top, minimum=minimum, fuzzy=fuzzy,
            )
            if not can_search(ctx, review=bool(review_id)):
                await ctx.send(f"{ERROR_EMOJI} You no longer have access to this search.", ephemeral=True)
                return
            if not indexed:
                warning_text = "\n" + "\n".join(query.warnings)[:1024] if query.warnings else ""
                await ctx.send(f"{ERROR_EMOJI} No current indexed candidates are available to you in this district."
                               + warning_text,
                               ephemeral=True)
                return
            if not matches:
                warning_text = "\n" + "\n".join(query.warnings)[:1024] if query.warnings else ""
                await ctx.send(f"{SUCCESS_EMOJI} Extraction succeeded. "
                               f"No matches with reliable geometry at or above {minimum:g}%. "
                               f"Indexed coverage: {indexed}/{total}." + warning_text, ephemeral=True)
                return
            warnings = [*query.warnings]
            if indexed < total:
                warnings.append(f"Incomplete compatible-fingerprint coverage: {indexed}/{total} accessible bases indexed.")
            if fuzzy:
                warnings.append("Fuzzy matching groups cannons and spears.")
            session = SearchSession(owner_id=str(ctx.author.id), district=district, matches=matches,
                                    warnings=warnings, review_id=review_id, query=query, fuzzy=fuzzy,
                                    query_attachment=submission.image_attachment if submission else None)
            payload = await render_page(session, ctx)
            message = await ctx.send(**payload, ephemeral=True)
            session.message_id = str(message.id)
            self._similarity.add(session)
        except FingerprintError as error:
            await ctx.send(f"{ERROR_EMOJI} Screenshot search failed: {error}. Review decisions remain available.",
                           ephemeral=True)
        except PyMongoError:
            await ctx.send(f"{ERROR_EMOJI} Search data is temporarily unavailable. Please retry.", ephemeral=True)
        finally:
            self._similarity.pending.discard(key)

    @ipy.component_callback(re.compile(r"^base_compare\|[\w-]+\|[0-9a-f]{24}$"))
    async def similar_compare(self, ctx: ipy.ComponentContext):
        await show_result_image(self._similarity, self.bot.base_fingerprints, ctx)

    @ipy.component_callback(re.compile(r"^base_labels\|[\w-]+\|[0-9a-f]{24}$"))
    async def similar_labels(self, ctx: ipy.ComponentContext):
        await show_result_image(self._similarity, self.bot.base_fingerprints, ctx, labels=True)

    @ipy.component_callback(re.compile(r"^base_similar\|[\w-]+\|(previous|next|page)$"))
    async def similar_page(self, ctx: ipy.ComponentContext) -> None:
        await self._navigate_similar_page(ctx)

    @ipy.modal_callback(re.compile(r"^base_similar_modal\|[\w-]+\|page$"))
    async def similar_page_modal(self, ctx: ipy.ModalContext, **response) -> None:
        await self._navigate_similar_page(ctx, page_number=response.get("page", ""))

    async def _navigate_similar_page(
        self, ctx: ipy.ComponentContext | ipy.ModalContext, *, page_number: str | None = None,
    ) -> None:
        if ctx.message is None:
            await ctx.send(f"{ERROR_EMOJI} The paginated message is no longer available.", ephemeral=True)
            return
        _, token, direction = ctx.custom_id.split("|")
        session = self._similarity.sessions.get(token)
        if session and (session.owner_id != str(ctx.author.id) or session.message_id != str(ctx.message.id)):
            await ctx.send(f"{ERROR_EMOJI} These search controls belong to another response or user.", ephemeral=True)
            return
        if session is None or session.expires_at <= time.monotonic():
            self._similarity.sessions.pop(token, None)
            await ctx.send(f"{ERROR_EMOJI} This search expired. Run a new search.", ephemeral=True)
            try:
                await ctx.message.edit(components=[])
            except ipy.errors.HTTPException:
                pass
            return
        if not can_search(ctx, review=bool(session.review_id)):
            await ctx.send(f"{ERROR_EMOJI} You no longer have access to this search.", ephemeral=True)
            return
        if direction == "page" and page_number is None:
            await ctx.send_modal(build_page_jump_modal(f"base_similar_modal|{token}|page"))
            return
        if page_number is None:
            await ctx.defer(edit_origin=True)
        async with session.lock:
            if self._similarity.sessions.get(token) is not session or session.expires_at <= time.monotonic():
                payload = {"content": f"{ERROR_EMOJI} This search expired. Run a new search.",
                           "embeds": [], "components": []}
            elif not can_search(ctx, review=bool(session.review_id)):
                payload = {"content": f"{ERROR_EMOJI} You no longer have access to this search.",
                           "embeds": [], "components": []}
            else:
                session.page += 1 if direction == "next" else -1 if direction == "previous" else 0
                try:
                    payload = await render_page(session, ctx, page_number=page_number)
                except InvalidPageError as error:
                    await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
                    return
            if page_number is None:
                await ctx.edit_origin(**payload)
            else:
                await ctx.edit("@original", **payload)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=1))
    async def refresh_similarity_buttons(self):
        now = time.monotonic()
        self._review_refresh_retry_after = {
            review_id: deadline for review_id, deadline in self._review_refresh_retry_after.items()
            if deadline > now
        }
        query = {"resolution": None}
        if self._review_refresh_cursor:
            query["_id"] = {"$gt": self._review_refresh_cursor}
        submissions = await PendingBaseSubmission.find(query).sort("_id").limit(100).to_list()
        for saved in submissions:
            self._review_refresh_cursor = saved.id
            if saved.id in self._review_refresh_retry_after:
                continue
            lock_key = int(saved.id)
            lock = self._review_locks.setdefault(lock_key, anyio.Lock())
            try:
                async with lock:
                    current = await PendingBaseSubmission.get(saved.id)
                    if current is None or current.resolution is not None:
                        continue
                    if current.review_channel_id not in (None, str(BASE_SUBMISSION_CHANNEL)):
                        continue
                    channel = await self.bot.fetch_channel(BASE_SUBMISSION_CHANNEL)
                    message = await channel.fetch_message(saved.id, force=True)
                    if message is None or message.author.id != self.bot.user.id:
                        continue
                    buttons = list(_iter_message_components(message.components))
                    button_ids = {button.custom_id for button in buttons}
                    legacy = button_ids == {ACCEPT_BUTTON_ID, REJECT_BUTTON_ID} and not any(
                        button.disabled for button in buttons)
                    pending_publication = (
                        button_ids == {ACCEPT_BUTTON_ID, REJECT_BUTTON_ID, FIND_SIMILAR_BUTTON_ID}
                        and all(button.disabled for button in buttons)
                        and any(embed.title == "Base Submission Review" and embed.description == current.link
                                and any(field.name == "Review Status" and field.value == ReviewStatus.PENDING.value
                                        for field in embed.fields) for embed in message.embeds)
                    )
                    if not legacy and not pending_publication:
                        continue
                    await message.edit(components=build_review_components())
            except ipy.errors.Forbidden as error:
                self._review_refresh_retry_after[saved.id] = time.monotonic() + 3600
                logging.warning(
                    "Could not refresh saved review %s: Forbidden (Discord code %s); retrying in 1 hour",
                    saved.id, error.code,
                )
            except ipy.errors.HTTPException as error:
                logging.warning("Could not refresh saved review %s: %s", saved.id, type(error).__name__)
            finally:
                if not lock.statistics().locked and lock.statistics().tasks_waiting == 0:
                    self._review_locks.pop(lock_key, None)
        if len(submissions) < 100:
            self._review_refresh_cursor = None

    @base.subcommand(sub_cmd_name="submit", sub_cmd_description="Submit a base for staff review")
    @ipy.check(has_any(ipy.has_any_role(*LVL3_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(name="base_link", description="The base link", opt_type=ipy.OptionType.STRING, required=True)
    @ipy.slash_option(
        name="base_quality",
        description="Proposed base quality",
        opt_type=ipy.OptionType.STRING,
        choices=[ipy.SlashCommandChoice(name=quality, value=quality) for quality in BASE_TIERS],
        required=True,
    )
    @ipy.slash_option(name="base_ss", description="Screenshot of the base", opt_type=ipy.OptionType.ATTACHMENT,
                      required=False)
    @ipy.slash_option(name="builder", description="Builder of the base", opt_type=ipy.OptionType.USER)
    @ipy.slash_option(
        name="reason",
        description="Why this base should be added",
        opt_type=ipy.OptionType.STRING,
        max_length=1000,
    )
    async def base_submit(
        self,
        ctx: ipy.SlashContext,
        base_link: str,
        base_ss: ipy.Attachment | None = None,
        base_quality: str = "Average",
        builder: ipy.User | None = None,
        reason: str | None = None,
    ) -> None:
        await ctx.defer(ephemeral=True)
        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)
            return
        district = parse_base_district(base_link)
        if district is None:
            await ctx.send(
                f"{ERROR_EMOJI} The link you have provided does not look like a Clash of Clans base link.",
                ephemeral=True,
            )
            return

        temp_path = f"temp/{secrets.token_hex(8)}.webp"
        try:
            if await BaseLayout.find_one(BaseLayout.link == base_link):
                await ctx.send(
                    f"{ERROR_EMOJI} This base link already exists in the base bank.", ephemeral=True,
                )
                return
            if base_ss is not None:
                if not await download_base_screenshot(self.bot, ctx, base_ss.url, temp_path):
                    return
            elif not await capture_base_screenshot(base_link, str(ctx.author.id), temp_path, ctx=ctx):
                await ctx.send(f"{ERROR_EMOJI} Screenshot unavailable. No review was created.",
                               ephemeral=True)
                return
            try:
                image_message = await self.bot.owner.send(
                    file=ipy.File(temp_path, file_name="base-submission.webp"),
                    silent=True,
                )
            except ipy.errors.HTTPException:
                if base_ss is not None:
                    raise
                await ctx.send(f"{ERROR_EMOJI} The screenshot could not be uploaded. No review was created.",
                               ephemeral=True)
                return
            if not image_message.attachments:
                await ctx.send(f"{ERROR_EMOJI} Something went wrong when uploading the image.", ephemeral=True)
                return
            image_url = image_message.attachments[0].url
            draft = SubmissionDraft(
                link=base_link,
                image_url=image_url,
                submitter_id=str(ctx.author.id),
                submitter_display=mention_user(ctx.author),
                builder_id=str(builder.id) if builder else None,
                builder_display=mention_user(
                    builder, builder.id if builder else 0, mention_type="Builder",
                ),
                district=district,
                proposed_quality=base_quality,
                reason=reason,
            )
            review_channel = await self.bot.fetch_channel(BASE_SUBMISSION_CHANNEL)
            review_message = await review_channel.send(
                embed=build_review_embed(draft),
                components=build_review_components(disabled=True),
            )
            submission = PendingBaseSubmission(
                id=str(review_message.id),
                review_channel_id=str(BASE_SUBMISSION_CHANNEL),
                image_attachment=attachment_identity(image_message),
                link=draft.link,
                image_url=draft.image_url,
                submitter_id=draft.submitter_id,
                submitter_display=draft.submitter_display,
                builder_id=draft.builder_id,
                builder_display=draft.builder_display,
                district=draft.district,
                proposed_quality=draft.proposed_quality,
                reason=draft.reason,
            )
            lock_key = int(review_message.id)
            lock = self._review_locks.setdefault(lock_key, anyio.Lock())
            try:
                async with lock:
                    await submission.insert()
                    controls_enabled = False
                    try:
                        published = await review_message.edit(components=build_review_components())
                        buttons = list(_iter_message_components(published.components)) if published else []
                        controls_enabled = (
                            {button.custom_id for button in buttons} ==
                            {ACCEPT_BUTTON_ID, REJECT_BUTTON_ID, FIND_SIMILAR_BUTTON_ID}
                            and not any(button.disabled for button in buttons)
                        )
                    except ipy.errors.HTTPException as error:
                        logging.warning("Saved review %s controls unavailable: %s; background refresh will retry",
                                        review_message.id, type(error).__name__)
                    if not controls_enabled:
                        await ctx.send(
                            "Your submission was saved, but its review buttons could not be enabled. "
                            "I will retry automatically; you do not need to submit it again.", ephemeral=True,
                        )
                        return
            finally:
                if not lock.statistics().locked and lock.statistics().tasks_waiting == 0:
                    self._review_locks.pop(lock_key, None)
            await ctx.send(f"{SUCCESS_EMOJI} Your base for `{district}` was submitted for review.", ephemeral=True)
        except PyMongoError:
            await ctx.send(
                f"{ERROR_EMOJI} I could not confirm that your submission was saved for review.",
                ephemeral=True,
            )
        finally:
            if os.path.exists(temp_path):
                await delete_file(temp_path)

    @ipy.component_callback(ACCEPT_BUTTON_ID)
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    async def accept_submission_button(self, ctx: ipy.ComponentContext) -> None:
        submission = await PendingBaseSubmission.get(str(ctx.message.id))
        if submission is None:
            await ctx.send(f"{ERROR_EMOJI} This submission is no longer pending.", ephemeral=True)
            return
        if submission.resolution is not None or await BaseLayout.find_one(BaseLayout.link == submission.link):
            await self._resolve_submission(ctx, AcceptDecision(submission.proposed_quality, ""))
            return
        options = [
            {"label": quality, "value": quality, "default": quality == submission.proposed_quality}
            for quality in BASE_TIERS
        ]
        await ctx.send_modal({
            "type": int(ipy.CallbackType.MODAL),
            "data": {
                "title": "Accept Base Submission",
                "custom_id": ACCEPT_MODAL_ID,
                "components": [
                    {
                        "type": 18,
                        "label": "Select the accepted quality",
                        "component": {
                            "type": 3,
                            "custom_id": "quality",
                            "options": options,
                            "required": True,
                        },
                    },
                    {
                        "type": 18,
                        "label": "Builder override",
                        "description": "Select Elo Bot to set the builder to none.",
                        "component": {
                            "type": int(ipy.ComponentType.USER_SELECT),
                            "custom_id": "builder",
                            "placeholder": "Select a builder",
                            "default_values": [{"id": submission.submitter_id, "type": "user"}],
                            "min_values": 1,
                            "max_values": 1,
                            "required": True,
                        },
                    },
                    {
                        "type": 18,
                        "label": "Moderator note",
                        "component": {
                            "type": 4,
                            "custom_id": "note",
                            "style": 2,
                            "required": False,
                            "max_length": 1000,
                        },
                    },
                ],
            },
        })

    @ipy.component_callback(REJECT_BUTTON_ID)
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    async def reject_submission_button(self, ctx: ipy.ComponentContext) -> None:
        submission = await PendingBaseSubmission.get(str(ctx.message.id))
        if submission is None:
            await ctx.send(f"{ERROR_EMOJI} This submission is no longer pending.", ephemeral=True)
            return
        if submission.resolution is not None:
            await self._resolve_submission(ctx, RejectDecision(""))
            return
        await ctx.send_modal(ipy.Modal(
            ipy.ParagraphText(
                label="Rejection reason",
                custom_id="reason",
                required=True,
                max_length=1000,
            ),
            title="Reject Base Submission",
            custom_id=REJECT_MODAL_ID,
        ))

    @ipy.modal_callback(ACCEPT_MODAL_ID)
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    async def accept_submission_modal(
        self,
        ctx: ipy.ModalContext,
        quality: list[str],
        note: str = "",
        builder: list[str] | None = None,
    ) -> None:
        selected_quality = quality[0] if quality else ""
        if selected_quality not in BASE_TIERS:
            await ctx.send(f"{ERROR_EMOJI} That base quality is invalid.", ephemeral=True)
            return
        selected_builder_id = builder[0] if builder else None
        await self._resolve_submission(
            ctx,
            AcceptDecision(selected_quality, note.strip(), selected_builder_id),
        )

    @ipy.modal_callback(REJECT_MODAL_ID)
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    async def reject_submission_modal(self, ctx: ipy.ModalContext, reason: str) -> None:
        if not reason.strip():
            await ctx.send(f"{ERROR_EMOJI} A rejection reason is required.", ephemeral=True)
            return
        await self._resolve_submission(ctx, RejectDecision(reason.strip()))

    async def _resolve_submission(
        self, ctx: ipy.ModalContext | ipy.ComponentContext, decision: ResolutionDecision,
    ) -> None:
        await ctx.defer(ephemeral=True)
        message_id = str(ctx.message.id)
        lock_key = int(message_id)
        lock = self._review_locks.setdefault(lock_key, anyio.Lock())
        try:
            async with lock:
                submission = await PendingBaseSubmission.get(message_id)
                if submission is None:
                    await ctx.send(f"{ERROR_EMOJI} This submission is no longer pending.", ephemeral=True)
                    return

                newly_resolved = False
                if submission.resolution is None:
                    match decision:
                        case AcceptDecision(quality=quality, note=note, builder_id=builder_id):
                            existing = await BaseLayout.find_one(BaseLayout.link == submission.link)
                            if existing:
                                resolution = SubmissionRejection(
                                    reviewer_id=str(ctx.author.id),
                                    reason="This base link already exists in the base bank.",
                                )
                            else:
                                accepted_builder_id = (
                                    _resolve_builder_id(self.bot, builder_id)
                                    if builder_id is not None else submission.builder_id
                                )
                                resolution = SubmissionAcceptance(
                                    reviewer_id=str(ctx.author.id), base_id=PydanticObjectId(), quality=quality,
                                    note=note, builder_id=builder_id, accepted_builder_id=accepted_builder_id,
                                    expiration_date=datetime.now().timestamp() + 15 * 86400,
                                )
                        case RejectDecision(reason=reason):
                            resolution = SubmissionRejection(reviewer_id=str(ctx.author.id), reason=reason)
                        case unreachable:
                            assert_never(unreachable)
                    newly_resolved = await submission.claim_resolution(resolution)
                    if not newly_resolved:
                        submission = await PendingBaseSubmission.get(message_id)
                        if submission is None:
                            await ctx.send(f"{ERROR_EMOJI} This submission is no longer pending.", ephemeral=True)
                            return

                match submission.resolution:
                    case SubmissionAcceptance(phase="accepting") as resolution:
                        match decision:
                            case RejectDecision():
                                await ctx.send(
                                    f"{ERROR_EMOJI} Acceptance is already in progress. Retry Accept to finish it.",
                                    ephemeral=True,
                                )
                                return
                            case AcceptDecision():
                                pass
                            case unreachable:
                                assert_never(unreachable)
                        stored_base = await BaseLayout.get(resolution.base_id)
                        if stored_base is None:
                            try:
                                await BaseLayout(
                                    id=resolution.base_id, district=submission.district, link=submission.link,
                                    image_url=submission.image_url, records=[], quality=resolution.quality,
                                    expiration_date=resolution.expiration_date,
                                    builder_id=resolution.accepted_builder_id,
                                    image_attachment=submission.image_attachment,
                                    fingerprint_extraction=ExtractionState(source_revision=0, pipeline_id=PIPELINE_ID),
                                ).insert()
                            except DuplicateKeyError:
                                if await BaseLayout.get(resolution.base_id) is None:
                                    raise
                        newly_resolved = await submission.complete_acceptance(resolution)
                        if not newly_resolved:
                            submission = await PendingBaseSubmission.get(message_id)
                            if submission is None:
                                await ctx.send(f"{ERROR_EMOJI} This submission is no longer pending.", ephemeral=True)
                                return
                    case SubmissionAcceptance(phase="accepted") | SubmissionRejection() | None:
                        pass
                    case unreachable:
                        assert_never(unreachable)

                await self._show_submission_resolution(ctx, submission, newly_resolved=newly_resolved)
        except PyMongoError:
            await ctx.send(
                f"{ERROR_EMOJI} I could not confirm the saved review decision. Retry the same decision to finish it.",
                ephemeral=True,
            )
        finally:
            lock_state = lock.statistics()
            if not lock_state.locked and lock_state.tasks_waiting == 0:
                self._review_locks.pop(lock_key, None)

    async def _show_submission_resolution(
        self, ctx: ipy.ModalContext | ipy.ComponentContext, submission: PendingBaseSubmission,
        *, newly_resolved: bool,
    ) -> None:
        match submission.resolution:
            case SubmissionAcceptance(phase="accepted") as resolution:
                decision = AcceptDecision(resolution.quality, resolution.note, resolution.builder_id)
                status = "accepted"
            case SubmissionRejection() as resolution:
                decision = RejectDecision(resolution.reason)
                status = "rejected"
            case SubmissionAcceptance(phase="accepting") | None:
                await ctx.send(f"{ERROR_EMOJI} This review is incomplete. Retry the same decision.", ephemeral=True)
                return
            case unreachable:
                assert_never(unreachable)
        user_ids = {submission.submitter_id, resolution.reviewer_id}
        builder_id = resolution.builder_id if isinstance(resolution, SubmissionAcceptance) else submission.builder_id
        if builder_id:
            user_ids.add(builder_id)
        guild = await member_guild(ctx)
        members = await asyncio.gather(*(guild.fetch_member(user_id) for user_id in user_ids))
        names = {user_id: mention_user(member, user_id, mention_type="Builder" if user_id == builder_id else "Player")
                 for user_id, member in zip(user_ids, members)}
        await ctx.message.edit(
            embed=mark_review_embed(submission, decision, resolution.reviewer_id, names),
            components=build_review_components(disabled=True),
        )
        fingerprint_status = ""
        if status == "accepted":
            try:
                base = await BaseLayout.get(resolution.base_id)
                if base and isinstance(base.fingerprint_extraction, ExtractionState):
                    fingerprint_status = f" Fingerprint extraction: {base.fingerprint_extraction.status}."
            except PyMongoError:
                fingerprint_status = " Fingerprint status is temporarily unavailable."
                logging.warning("Fingerprint status unavailable for accepted review %s", submission.id)
        if newly_resolved:
            await self._notify_submitter(submission.submitter_id, build_resolution_embed(submission, decision))
            await ctx.send(f"{SUCCESS_EMOJI} This base submission was {status}.{fingerprint_status}", ephemeral=True)
        else:
            await ctx.send(f"{ERROR_EMOJI} This base submission was already {status}.", ephemeral=True)

    async def _notify_submitter(self, submitter_id: str, embed: ipy.Embed) -> None:
        submitter = await self.bot.fetch_user(int(submitter_id))
        try:
            await submitter.send(embed=embed)
        except ipy.errors.Forbidden:
            command_channel = await self.bot.fetch_channel(COMMAND_CHANNEL)
            await command_channel.send(
                f"{submitter.mention}\n*This message is sent here because you have disabled direct messages.*",
                embed=embed,
            )

    @base.subcommand(sub_cmd_name="info", sub_cmd_description="Show detailed information of a base")
    @ipy.check(ipy.has_any_role(*LVL3_ROLES))
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        autocomplete=True,
        required=True
    )
    @ipy.slash_option(
        name="embed_type",
        description="The type of embed to display",
        choices=[
            ipy.SlashCommandChoice(name="Performance", value="Performance"),
            ipy.SlashCommandChoice(name="Defense Log", value="Defense Log")
        ],
        opt_type=ipy.OptionType.STRING,
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def base_info(self, ctx: ipy.SlashContext, base_link: str, embed_type: str = "Performance",
                        hidden: bool = True):
        await ctx.defer(ephemeral=hidden)

        if base_link.isdigit():
            try:
                index = int(base_link)
                bases = await BaseLayout.find_all().sort("_id").to_list()
                base = bases[index]
            except (ValueError, IndexError):
                await ctx.send(f"{ERROR_EMOJI} The provided base link is invalid.", ephemeral=hidden)
                return
        else:
            base = await BaseLayout.find_one(BaseLayout.link == base_link)
            if not base:
                await ctx.send(f"{ERROR_EMOJI} The base is not part of the helper's bases.",
                               ephemeral=hidden)

                return

        if base.builder_id != str(ctx.author.id) and str(ctx.author.id) != str(self.bot.owner.id):
            await ctx.send(f"{ERROR_EMOJI} You may not access bases built by others.",
                           ephemeral=hidden)

            return

        guild = await member_guild(ctx)
        embed = construct_base_embed(base, embed_type, names=await base_embed_names(guild, base, embed_type))

        await ctx.send(embed=embed, ephemeral=hidden)

    @base_info.autocomplete(option_name="base_link")
    async def base_autocomplete(self, ctx: ipy.AutocompleteContext):
        bases = await BaseLayout.find_all().sort("_id").project(BaseAutocompleteRow).to_list()
        author_id = str(ctx.author.id)
        is_owner = author_id == str(self.bot.owner.id)

        choices = {}
        for index, base in enumerate(bases, start=1):
            if not is_owner and base.builder_id != author_id:
                continue
            builder = _builder_choice_label(self.bot, ctx.guild, base.builder_id)
            choices[f"{base.district} {builder} {index}".lower()] = {
                "name": f"{base.district} by {builder} ({index})"[:100],
                "value": index - 1,
            }

        if not choices:
            return await ctx.send([])

        user_input = ctx.input_text or ""
        if not user_input:
            return await ctx.send(list(choices.values())[:25])

        returns = difflib.get_close_matches(
            user_input.lower(),
            choices.keys(),
            n=25,
            cutoff=0.15,
        )
        await ctx.send([choices[item] for item in returns])

    @base.subcommand(sub_cmd_name="add", sub_cmd_description="Add a base")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="base_quality",
        description="The quality of the base",
        opt_type=ipy.OptionType.STRING,
        choices=BASE_QUALITY_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="base_ss",
        description="Screenshot of your first base",
        opt_type=ipy.OptionType.ATTACHMENT,
        required=False
    )
    @ipy.slash_option(
        name="builder",
        description="Builder of the base",
        opt_type=ipy.OptionType.USER
    )
    async def base_add(self, ctx: ipy.SlashContext, base_link: str, base_ss: ipy.Attachment | None = None,
                       base_quality: str = "Average",
                       builder: ipy.User = None):
        await ctx.defer(ephemeral=True)

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)
            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or
                (len(base_link.split("%")) > 1 and base_link.split("%")[1] != "3ACC")):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)
            return

        # handle image upload
        token = secrets.token_hex(8)
        temp_path = f"temp/{token}.webp"

        downloaded = False
        try:
            if base_ss is not None:
                downloaded = await download_base_screenshot(self.bot, ctx, base_ss.url, temp_path)
            else:
                downloaded = await capture_base_screenshot(base_link, str(ctx.author.id), temp_path, ctx=ctx)
                if not downloaded:
                    await ctx.send(f"{ERROR_EMOJI} Screenshot unavailable. No base was changed.",
                                   ephemeral=True)
            if not downloaded:
                return
            file_msg = await self.bot.owner.send(file=ipy.File(temp_path), silent=True)
        except ipy.errors.HTTPException:
            if base_ss is not None:
                raise
            await ctx.send(f"{ERROR_EMOJI} The screenshot could not be uploaded. No base was changed.", ephemeral=True)
            return
        finally:
            if downloaded or os.path.exists(temp_path):
                await delete_file(temp_path)
        if not file_msg.attachments:
            await ctx.send(f"{ERROR_EMOJI} Something went wrong when uploading the image.", ephemeral=True)
            return

        # update database
        expiration_ts = datetime.now().timestamp() + 15 * 86400
        district = DISTRICT_CODES_REVERSED[base_link.split('%')[2]]
        existing_base = await BaseLayout.find_one(BaseLayout.link == base_link)
        if existing_base:
            await replace_source(existing_base, file_msg.attachments[0].url, district,
                                 attachment=attachment_identity(file_msg), metadata={
                                     "expiration_date": expiration_ts, "builder_id": str(builder.id) if builder else None,
                                     "quality": base_quality,
                                 })
        else:
            new_base = BaseLayout(
                district=district,
                link=base_link,
                image_url=file_msg.attachments[0].url,
                records=[],
                expiration_date=expiration_ts,
                builder_id=str(builder.id) if builder else None,
                quality=base_quality,
                image_attachment=attachment_identity(file_msg),
                fingerprint_extraction=ExtractionState(source_revision=0, pipeline_id=PIPELINE_ID),
            )
            await new_base.insert()

        await ctx.send(
            f"{SUCCESS_EMOJI} You have successfully added a base for `{district}`. Fingerprint extraction pending.",
            ephemeral=True,
        )

    @base.subcommand(sub_cmd_name="remove", sub_cmd_description="Remove a base")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    async def base_remove(self, ctx: ipy.SlashContext, base_link: str):
        await ctx.defer(ephemeral=True)

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)

            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or base_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        district = DISTRICT_CODES_REVERSED[base_link.split('%')[2]]

        base = await BaseLayout.find_one(BaseLayout.link == base_link)
        if not base:
            await ctx.send(f"{ERROR_EMOJI} This base is not stored in the helper's base bank.", ephemeral=True)
            return

        await base.delete()

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully removed a base for `{district}`.")

    @base.subcommand(sub_cmd_name="renew", sub_cmd_description="Preserve a base link and renew its expiration")
    @ipy.max_concurrency(ipy.Buckets.USER, 1)
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="base_link",
        description="The stored base link",
        opt_type=ipy.OptionType.STRING,
        required=True,
    )
    async def base_renew(self, ctx: ipy.SlashContext, base_link: str) -> None:
        await helper_preserve.autorenew_base(self, ctx, base_link)

    @set_group.subcommand(sub_cmd_name="link", sub_cmd_description="Set a renewed link for a base")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="old_base_link",
        description="The old base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="new_base_link",
        description="The new base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    async def base_set_link(self, ctx: ipy.SlashContext, old_base_link: str, new_base_link: str):
        await ctx.defer(ephemeral=True)

        if not validators.url(new_base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)

            return

        if (not new_base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in new_base_link.split("=") or new_base_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        base = await BaseLayout.find_one(BaseLayout.link == old_base_link)
        if not base:
            await ctx.send(f"{ERROR_EMOJI} This base is not stored in the helper's base bank.", ephemeral=True)
            return

        base.link = new_base_link
        base.expiration_date = datetime.now().timestamp() + 15 * 86400
        await base.save_changes()
        await self.send_base_renewal_log(base)

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully renewed a base for `{base.district}`.")

    @base.subcommand(sub_cmd_name="list", sub_cmd_description="List bases of a certain district")
    @ipy.check(has_any(ipy.has_any_role(*LVL3_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(
        name="district",
        description="The base is for which district?",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES
    )
    @ipy.slash_option(
        name="embed_type",
        opt_type=ipy.OptionType.STRING,
        description="The type of embed to display",
        choices=[
            ipy.SlashCommandChoice(name="Performance", value="Performance"),
            ipy.SlashCommandChoice(name="Defense Log", value="Defense Log")
        ]
    )
    @ipy.slash_option(
        name="base_quality",
        description="The quality of the base",
        opt_type=ipy.OptionType.STRING,
        choices=BASE_QUALITY_CHOICES
    )
    @ipy.slash_option(
        name="builder",
        description="The builder of the base",
        opt_type=ipy.OptionType.USER
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def base_list(self, ctx: ipy.SlashContext, district: str = "Any", embed_type: str = "Performance",
                        base_quality: str = "Any", builder: str = None, hidden: bool = True):
        await ctx.defer(ephemeral=hidden)

        if not ctx.author.has_any_role(LVL1_ROLES):
            builder = ctx.author

        button_builder_state = str(builder.id) if builder else "Any"
        query = _public_list_query(self.bot, district, base_quality, button_builder_state)
        base_doc, total = await _one_listed_base(query, 0)

        # handle empty results
        if base_doc is None:
            msg = f"{ERROR_EMOJI} No bases found for `{district}`"
            if base_quality != "Any":
                msg += f" with **{base_quality}** quality"
            if builder:
                msg += f" built by {builder.mention}"
            msg += "."

            await ctx.send(msg, ephemeral=hidden)
            return

        guild = await member_guild(ctx)
        names = await base_embed_names(guild, base_doc, embed_type)
        embed = construct_base_embed(base_doc, embed_type, names=names)
        await ctx.send(
            embed=embed,
            ephemeral=hidden,
            components=_build_base_list_components(
                district, 0, embed_type, base_quality, button_builder_state, total,
            ),
        )

    @ipy.component_callback(re.compile(r"^base_right\|.*\|\d+\|.*\|.*$"))
    async def base_right_button(self, ctx: ipy.ComponentContext):
        if ctx.author and int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)
            return

        _, district, index_str, embed_type, base_quality, builder_id = ctx.custom_id.split("|")
        index = int(index_str) + 1

        query = _public_list_query(self.bot, district, base_quality, builder_id)
        base, total = await _one_listed_base(query, index)
        if base is None:
            await ctx.send(f"{ERROR_EMOJI} This page no longer exists.", ephemeral=True)
            return

        guild = await member_guild(ctx)
        embed = construct_base_embed(base, embed_type, names=await base_embed_names(guild, base, embed_type))
        await ctx.edit_origin(
            embed=embed,
            components=_build_base_list_components(
                district, index, embed_type, base_quality, builder_id, total,
            ),
        )

    @ipy.component_callback(re.compile(r"^base_left\|.*\|\d+\|.*$"))
    async def base_left_button(self, ctx: ipy.ComponentContext):
        if ctx.author and int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)
            return

        _, district, index_str, embed_type, base_quality, builder_id = ctx.custom_id.split("|")
        index = int(index_str) - 1

        query = _public_list_query(self.bot, district, base_quality, builder_id)
        base, total = await _one_listed_base(query, index)
        if base is None:
            await ctx.send(f"{ERROR_EMOJI} This page no longer exists.", ephemeral=True)
            return

        guild = await member_guild(ctx)
        embed = construct_base_embed(base, embed_type, names=await base_embed_names(guild, base, embed_type))
        await ctx.edit_origin(
            embed=embed,
            components=_build_base_list_components(
                district, index, embed_type, base_quality, builder_id, total,
            ),
        )

    @ipy.component_callback(re.compile(r"^base_page\|.*\|.*\|.*\|.*$"))
    async def base_page_button(self, ctx: ipy.ComponentContext):
        if ctx.author and int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)
            return

        modal_custom_id = ctx.custom_id.replace("base_page|", "base_page_modal|", 1)
        await ctx.send_modal(build_page_jump_modal(modal_custom_id))

    @ipy.modal_callback(re.compile(r"^base_page_modal\|.*\|.*\|.*\|.*$"))
    async def base_page_modal(self, ctx: ipy.ModalContext, **response):
        message = ctx.message
        if message is None:
            await ctx.send(f"{ERROR_EMOJI} The paginated message is no longer available.", ephemeral=True)
            return

        if ctx.author and int(ctx.author.id) != int(message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)
            return

        _, district, embed_type, base_quality, builder_id = ctx.custom_id.split("|")
        query = _public_list_query(self.bot, district, base_quality, builder_id)

        total = await query.count()
        if total == 0:
            await ctx.send(f"{ERROR_EMOJI} There are no matching bases anymore.", ephemeral=True)
            return

        try:
            page = parse_page_number(response["page"], total)
        except InvalidPageError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return

        index = page - 1
        base, total = await _one_listed_base(query, index, total=total)
        if base is None:
            await ctx.send(f"{ERROR_EMOJI} This page no longer exists.", ephemeral=True)
            return

        guild = await member_guild(ctx)
        embed = construct_base_embed(base, embed_type, names=await base_embed_names(guild, base, embed_type))
        await ctx.edit(
            "@original",
            embed=embed,
            components=_build_base_list_components(
                district, index, embed_type, base_quality, builder_id, total,
            ),
        )

    @ipy.component_callback("base_list_district_select")
    async def base_list_district_select(self, ctx: ipy.ComponentContext):
        if ctx.author and int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)
            return

        district = ctx.values[0]
        _, _, embed_type, base_quality, builder_id = _parse_base_list_state(ctx.message.components)
        query = _public_list_query(self.bot, district, base_quality, builder_id)
        base, total = await _one_listed_base(query, 0)
        if base is None:
            msg = f"{ERROR_EMOJI} No bases found for `{district}`"
            if base_quality != "Any":
                msg += f" with **{base_quality}** quality"
            if builder_id != "Any":
                msg += f" built by <@{builder_id}>"
            msg += "."
            await ctx.send(msg, ephemeral=True)
            return

        guild = await member_guild(ctx)
        embed = construct_base_embed(base, embed_type, names=await base_embed_names(guild, base, embed_type))
        await ctx.edit_origin(
            embed=embed,
            components=_build_base_list_components(
                district, 0, embed_type, base_quality, builder_id, total,
            ),
        )
        await ctx.send(f"{SUCCESS_EMOJI} The district type is changed to `{district}`.", ephemeral=True)

    @ipy.component_callback("base_list_embed_type_select")
    async def base_list_embed_type_select(self, ctx: ipy.ComponentContext):
        if ctx.author and int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)
            return

        embed_type = ctx.values[0]
        district, index, _, base_quality, builder_id = _parse_base_list_state(ctx.message.components)
        query = _public_list_query(self.bot, district, base_quality, builder_id)
        base, total = await _one_listed_base(query, index)
        if base is None:
            await ctx.send(f"{ERROR_EMOJI} This page no longer exists.", ephemeral=True)
            return

        guild = await member_guild(ctx)
        embed = construct_base_embed(base, embed_type, names=await base_embed_names(guild, base, embed_type))
        await ctx.edit_origin(
            embed=embed,
            components=_build_base_list_components(
                district, index, embed_type, base_quality, builder_id, total,
            ),
        )
        await ctx.send(f"{SUCCESS_EMOJI} The embed type is changed to `{embed_type}`.", ephemeral=True)

    private_group = base.group(name="private", description="")

    @private_group.subcommand(sub_cmd_name="add", sub_cmd_description="Add a private base")
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="base_ss",
        description="Screenshot of your base",
        opt_type=ipy.OptionType.ATTACHMENT,
        required=False
    )
    async def base_private_add(self, ctx: ipy.SlashContext, base_link: str, base_ss: ipy.Attachment | None = None):
        await ctx.defer(ephemeral=True)

        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered. Please register first.", ephemeral=True)
            return
        
        player_base_count = await BaseLayout.find(
            BaseLayout.builder_id == str(ctx.author.id),
            BaseLayout.quality == "Private"
        ).count()
        base_count_limit = 10 if player.elo < 1200 else 20
        if player_base_count >= base_count_limit:
            await ctx.send(f"{ERROR_EMOJI} You have reached the maximum number of private bases ({base_count_limit}). "
                           f"Please remove some of your private bases before adding a new one.", ephemeral=True)

            return

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)
            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or
                (len(base_link.split("%")) > 1 and base_link.split("%")[1] != "3ACC")):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)
            return

        helper_base = await BaseLayout.find_one(
            BaseLayout.link == base_link, BaseLayout.quality != "Private"
        )
        if helper_base:
            await ctx.send(f"{ERROR_EMOJI} This base link is already registered as a helper base.", ephemeral=True)
            return

        # handle image upload
        token = secrets.token_hex(8)
        temp_path = f"temp/{token}.webp"

        downloaded = False
        file_msg = None
        try:
            if base_ss is not None:
                downloaded = await download_base_screenshot(self.bot, ctx, base_ss.url, temp_path)
                if not downloaded:
                    return
            else:
                downloaded = await capture_base_screenshot(
                    base_link, str(ctx.author.id), temp_path, ctx=ctx, keep_secret=True,
                )
            if downloaded:
                file_msg = await self.bot.owner.send(file=ipy.File(temp_path), silent=True)
                if not file_msg.attachments:
                    await ctx.send(f"{ERROR_EMOJI} Something went wrong when uploading the image.", ephemeral=True)
                    return
        except ipy.errors.HTTPException:
            if base_ss is not None:
                raise
            await ctx.send(f"{ERROR_EMOJI} The screenshot could not be uploaded. No base was changed.", ephemeral=True)
            return
        finally:
            if downloaded or os.path.exists(temp_path):
                await delete_file(temp_path)

        # update database
        expiration_ts = datetime.now().timestamp() + 15 * 86400
        district = DISTRICT_CODES_REVERSED[base_link.split('%')[2]]
        
        # check if base already exists (even if private)
        existing_base = await BaseLayout.find_one(
            BaseLayout.link == base_link, 
            BaseLayout.quality == "Private"
        )
        if existing_base:
            if file_msg is None:
                await ctx.send(f"{ERROR_EMOJI} Screenshot unavailable. Your previous base was kept.",
                               ephemeral=True)
                return
            await replace_source(existing_base, file_msg.attachments[0].url, district,
                                 attachment=attachment_identity(file_msg), metadata={
                                     "expiration_date": expiration_ts, "builder_id": str(ctx.author.id),
                                 })
        else:
            new_base = BaseLayout(
                district=district,
                link=base_link,
                image_url=file_msg.attachments[0].url if file_msg else UNAVAILABLE_IMAGE,
                records=[],
                expiration_date=expiration_ts,
                builder_id=str(ctx.author.id),
                quality="Private",
                image_attachment=attachment_identity(file_msg) if file_msg else None,
                fingerprint_extraction=ExtractionState(
                    source_revision=0, pipeline_id=PIPELINE_ID,
                ) if file_msg else None,
            )
            await new_base.insert()

        image_status = "Fingerprint extraction pending." if file_msg else "Screenshot unavailable."
        await ctx.send(f"{SUCCESS_EMOJI} You have successfully added a private base for `{district}`. "
                       f"{image_status}", ephemeral=True)

    @private_group.subcommand(sub_cmd_name="list", sub_cmd_description="List your private bases")
    @ipy.slash_option(
        name="district",
        description="Filter by district",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES
    )
    @ipy.slash_option(
        name="embed_type",
        opt_type=ipy.OptionType.STRING,
        description="The type of embed to display",
        choices=[
            ipy.SlashCommandChoice(name="Performance", value="Performance"),
            ipy.SlashCommandChoice(name="Defense Log", value="Defense Log")
        ]
    )
    async def base_private_list(self, ctx: ipy.SlashContext, district: str = "Any", embed_type: str = "Performance"):
        await ctx.defer(ephemeral=True)

        player = await Player.find_one(Player.user_id == str(ctx.author.id), fetch_links=True)
        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered. Please register first.", ephemeral=True)
            return

        query = BaseLayout.find(BaseLayout.quality == "Private", BaseLayout.builder_id == str(ctx.author.id))

        if district != "Any":
            query = query.find(BaseLayout.district == district)

        bases = await query.sort("_id").to_list()

        if not bases:
            msg = f"{ERROR_EMOJI} You have no private bases"
            if district != "Any":
                msg += f" for `{district}`"
            msg += "."
            await ctx.send(msg, ephemeral=True)
            return

        # pagination and buttons
        index = 0
        base = bases[index]
        
        is_preset = False
        if base.district in player.base_presets:
            preset_base = player.base_presets[base.district]
            is_preset = preset_base and preset_base.link == base.link

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"p_base_left|{district}|{index - 1}",
            disabled=True,
        )

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"p_base_right|{district}|{index + 1}",
            disabled=(len(bases) - 1 <= index)
        )
        
        preset_label = "Preset Remove" if is_preset else "Preset Add"
        preset_style = ipy.ButtonStyle.DANGER if is_preset else ipy.ButtonStyle.SUCCESS
        preset_action = "remove" if is_preset else "add"
        preset_button = ipy.Button(
            style=preset_style,
            label=preset_label,
            custom_id=f"p_base_preset|{district}|{index}|{base.id}|{preset_action}"
        )

        remove_button = ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Delete",
            custom_id=f"p_base_delete|{district}|{index}|{base.id}"
        )

        guild = await member_guild(ctx)
        embed = construct_base_embed(base, embed_type, names=await base_embed_names(guild, base, embed_type))
        await ctx.send(
            embed=embed, ephemeral=True, 
            components=ipy.ActionRow(
                left_button, right_button, preset_button, remove_button
            )
        )

    @ipy.component_callback(re.compile(r"^p_base_(left|right)\|.*\|\d+$"))
    async def p_base_pagination(self, ctx: ipy.ComponentContext):
        if ctx.author and int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.", ephemeral=True)
            return

        await ctx.defer(edit_origin=True)
        action, district, index_str = ctx.custom_id.split("|")
        action = action.split("_")[1]
        index = int(index_str)
        
        if action == "left":
            index -= 1
        elif action == "right":
            index += 1
            
        await update_private_base_message(ctx, district, index)

    @ipy.component_callback(re.compile(r"^p_base_preset\|[^|]+\|\d+(?:\|[0-9a-f]{24}(?:\|(?:add|remove))?)?$"))
    async def p_base_preset(self, ctx: ipy.ComponentContext):
        if ctx.author and int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        _, district, index_str, *identity = ctx.custom_id.split("|")
        index = int(index_str)

        base = await find_private_base_action(ctx, district, identity[0] if identity else None)
        if base is None:
            await ctx.edit(ctx.message, components=[])
            await ctx.send(f"{ERROR_EMOJI} Base not found.", ephemeral=True)
            return
        player = await Player.find_one(Player.user_id == str(ctx.author.id), fetch_links=True)
        
        if not player:
            # Should not happen if they are adding private bases, but good to check
            await ctx.send(f"{ERROR_EMOJI} Player not found.", ephemeral=True)
            return

        remove_requested = identity[1] == "remove" if len(identity) > 1 else None
        if remove_requested is None:
            for row in ctx.message.components:
                for component in row.components:
                    if component.custom_id == ctx.custom_id and component.label in {"Preset Add", "Preset Remove"}:
                        remove_requested = component.label == "Preset Remove"
        if remove_requested is None:
            await ctx.edit(ctx.message, components=[])
            await ctx.send(f"{ERROR_EMOJI} Reopen your private bases to use this preset control.", ephemeral=True)
            return
        if remove_requested:
            await clear_private_base_preset(player, base)
            action_str = "removed from"
        else:
            await player.set({f"base_presets.{base.district}": base})
            action_str = "added to"
        await ctx.send(f"{SUCCESS_EMOJI} Base {action_str} presets.", ephemeral=True)
        await update_private_base_message(ctx, district, index)

    @ipy.component_callback(re.compile(r"^p_base_delete\|[^|]+\|\d+(?:\|[0-9a-f]{24})?$"))
    async def p_base_delete(self, ctx: ipy.ComponentContext):
        if ctx.author and int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        _, district, index_str, *identity = ctx.custom_id.split("|")
        index = int(index_str)

        base = await find_private_base_action(ctx, district, identity[0] if identity else None)
        if base is None:
            await ctx.edit(ctx.message, components=[])
            await ctx.send(f"{ERROR_EMOJI} Base not found.", ephemeral=True)
            return
        
        player = await Player.find_one(Player.user_id == str(ctx.author.id), fetch_links=True)
        if player:
            await clear_private_base_preset(player, base)

        deleted = await base.get_pymongo_collection().delete_one({
            "_id": base.id, "quality": "Private", "builder_id": str(ctx.author.id), "district": base.district,
        })
        if deleted.deleted_count == 0:
            await ctx.edit(ctx.message, components=[])
            await ctx.send(
                f"{ERROR_EMOJI} This base changed or is no longer available to delete. Reopen your private bases.",
                ephemeral=True,
            )
            return
        await ctx.send(f"{SUCCESS_EMOJI} Base deleted.", ephemeral=True)
        
        # update message, index might need adjustment if it was the last element
        await update_private_base_message(ctx, district, index)

    @set_group.subcommand(sub_cmd_name="expired", sub_cmd_description="Set a base as expired")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    async def base_set_expired(self, ctx: ipy.SlashContext, base_link: str):
        await ctx.defer(ephemeral=True)

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)

            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or base_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        base = await BaseLayout.find_one(BaseLayout.link == base_link)
        if not base:
            await ctx.send(f"{ERROR_EMOJI} This base is not stored in the helper's base bank.")

            return

        base.expiration_date = datetime.now().timestamp()
        await base.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} This is base is now marked as expired.")

    @set_group.subcommand(sub_cmd_name="quality", sub_cmd_description="Set the base builder for a base")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="base_quality",
        description="Quality of the base",
        opt_type=ipy.OptionType.STRING,
        choices=BASE_QUALITY_CHOICES,
        required=True
    )
    async def base_set_quality(self, ctx: ipy.SlashContext, base_link: str, base_quality: str):
        await ctx.defer(ephemeral=True)

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)

            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or base_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        base = await BaseLayout.find_one(BaseLayout.link == base_link)
        if not base:
            await ctx.send(f"{ERROR_EMOJI} This base is not stored in the helper's base bank.")
            return

        base.quality = base_quality
        await base.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully set the quality of the base as **{base_quality}**.")

    @set_group.subcommand(sub_cmd_name="builder", sub_cmd_description="Set the base builder for a base")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="builder",
        description="Builder of the base",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    async def base_set_builder(self, ctx: ipy.SlashContext, base_link: str, builder: ipy.User):
        await ctx.defer(ephemeral=True)

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)

            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or base_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        base = await BaseLayout.find_one(BaseLayout.link == base_link)
        if not base:
            await ctx.send(f"{ERROR_EMOJI} This base is not stored in the helper's base bank.")
            return

        await base.set({"builder_id": str(builder.id) if not builder.bot else None})

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully set {builder.mention} as the builder.")

    @set_group.subcommand(sub_cmd_name="image", sub_cmd_description="Set the image of a base")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="image_link",
        description="The image link of the base",
        opt_type=ipy.OptionType.STRING,
        required=False
    )
    async def base_set_image(self, ctx: ipy.SlashContext, base_link: str, image_link: str | None = None):
        await ctx.defer(ephemeral=True)

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)

            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or base_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        base = await BaseLayout.find_one(BaseLayout.link == base_link)
        if not base:
            await ctx.send(f"{ERROR_EMOJI} This base is not stored in the helper's base bank.", ephemeral=True)
            return

        if image_link is not None and "ephemeral-attachments" in image_link.split("/"):
            await ctx.send(f"{ERROR_EMOJI} The attachment link is marked as ephemeral.", ephemeral=True)
            return

        with tempfile.TemporaryDirectory(prefix="base-screenshot-") as directory:
            temp_path = os.path.join(directory, "image")
            if image_link is not None:
                downloaded = await download_base_screenshot(self.bot, ctx, image_link, temp_path)
            else:
                downloaded = await capture_base_screenshot(
                    base_link, str(ctx.author.id), temp_path, ctx=ctx, keep_secret=base.quality == "Private",
                )
                if not downloaded:
                    await ctx.send(f"{ERROR_EMOJI} Screenshot unavailable. Your previous image was kept.",
                                   ephemeral=True)
            if not downloaded:
                return
            try:
                file_msg = await self.bot.owner.send(file=ipy.File(temp_path), silent=True)
            except ipy.errors.HTTPException:
                if image_link is not None:
                    raise
                await ctx.send(f"{ERROR_EMOJI} The screenshot could not be uploaded. Your previous image was kept.",
                               ephemeral=True)
                return
            if not file_msg.attachments:
                await ctx.send(f"{ERROR_EMOJI} Something went wrong when uploading the image.", ephemeral=True)
                return

        await replace_source(base, file_msg.attachments[0].url, base.district,
                             attachment=attachment_identity(file_msg))

        await ctx.send(f"{SUCCESS_EMOJI} Screenshot saved. Fingerprint extraction pending.", ephemeral=True)

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

    @ipy.Task.create(ipy.IntervalTrigger(hours=24))
    async def base_expiration_warning(self):
        now = int(datetime.now().timestamp())
        warning_threshold = now + (3 * 86400)
        expiring_bases = await BaseLayout.find(
            BaseLayout.expiration_date <= warning_threshold
        ).to_list()

        warning_thread = await self.bot.fetch_channel(BASE_RENEWAL_THREAD)
        warned = False
        for base in expiring_bases:
            renew_button = ipy.Button(
                style=ipy.ButtonStyle.GRAY,
                label="Renew",
                custom_id="renew_base_button",
                emoji=ipy.PartialEmoji(name="🔄")
            )

            ts = ipy.Timestamp.fromtimestamp(base.expiration_date).format(ipy.TimestampStyles.RelativeTime)
            embed = ipy.Embed(
                title=f"**{base.district} Base Expiring Soon**",
                description=f"Expired {ts}" if base.expiration_date < now else f"Expires {ts}",
                fields=[
                    ipy.EmbedField(
                        name="**Old Base Link**",
                        value=base.link
                    )
                ],
                footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
                color=COLOR
            )
            embed.set_image(base.image_url)

            await warning_thread.send(embed=embed, components=renew_button)
            warned = True
            await helper_preserve.automatically_renew_base(self, base, str(self.bot.user.id))

        if warned:
            await warning_thread.send(LINE_SEPERATOR_GIF)

    @ipy.component_callback("renew_base_button")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    async def base_renew_button(self, ctx: ipy.ComponentContext):
        old_link = ctx.message.embeds[0].fields[0].value
        existing_base = await BaseLayout.find_one(BaseLayout.link == old_link)
        if not existing_base:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} It seems that the base is already renewed.", ephemeral=True)
            return

        modal = ipy.Modal(
            ipy.ParagraphText(
                label="Insert the renewed base link here",
                max_length=200,
                placeholder="Please make sure that the renewed link is correct!",
                custom_id="renewed_link",
                required=False
            ),
            title="Base Renew Modal",
            custom_id="renew_base_modal",
        )

        await ctx.send_modal(modal)

    @ipy.modal_callback("renew_base_modal")
    async def base_renew_modal(self, ctx: ipy.ModalContext, **response):
        await ctx.defer(ephemeral=True)

        old_link = ctx.message.embeds[0].fields[0].value
        new_link = response["renewed_link"]

        if (not new_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in new_link.split("=") or new_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        old_district = DISTRICT_CODES_REVERSED[old_link.split('%')[2]]
        new_district = DISTRICT_CODES_REVERSED[new_link.split('%')[2]]
        if old_district != new_district:
            await ctx.send(f"{ERROR_EMOJI} Your district of your renewed link is incorrect. The district of the link "
                           f"you provided is `{new_district}`, but the expected district is `{old_district}`.",
                           ephemeral=True)

            return

        if new_link and old_link == new_link:
            await ctx.send(f"{ERROR_EMOJI} The new link you have entered is the same as the old link.",
                           ephemeral=True)

            return

        base = await BaseLayout.find_one(BaseLayout.link == old_link)
        if not base:
            await ctx.send(f"{ERROR_EMOJI} The original base could not be found (it may have been deleted).",
                           ephemeral=True)
            return

        if new_link:
            base.link = new_link
        base.expiration_date = datetime.now().timestamp() + 15 * 86400

        await base.save_changes()
        await self.send_base_renewal_log(base)

        ctx.message.embeds[0].add_field(name="New Base Link", value=new_link)
        await ctx.message.edit(embeds=ctx.message.embeds,
                               components=utils.misc_utils.disable_components(*ctx.message.components))

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully renewed a base for `{new_district}`.",
                       ephemeral=True)

    async def warm_builder_names(self) -> None:
        names = _builder_names(self.bot)
        rows = await BaseLayout.find_all().project(BaseAutocompleteRow).to_list()
        guild = self.bot.get_guild(MAIN_GUILD)
        for row in rows:
            builder_id = str(row.builder_id) if row.builder_id else None
            if not builder_id or builder_id in names:
                continue
            cached = guild.get_member(builder_id) if guild is not None else None
            if cached is None:
                cached = self.bot.get_user(builder_id)
            name = _cached_user_name(cached)
            if name:
                names[builder_id] = name
                continue
            user = await self.bot.fetch_user(builder_id)
            name = _cached_user_name(user)
            if name:
                names[builder_id] = name
            await anyio.sleep(0.1)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Base commands loaded")
        asyncio.create_task(self.warm_builder_names())
        self.bot.base_fingerprints.start()
        self.refresh_similarity_buttons.start()

        if int(self.bot.user.id) == BOT_ID:
            await self.base_expiration_warning()

        self.base_expiration_warning.start()
