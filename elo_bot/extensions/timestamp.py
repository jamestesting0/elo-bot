import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

import interactions as ipy
from anyio import fail_after
from pymongo.errors import ConnectionFailure

from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.core.timestamp_dates import TimestampInputError
from elo_bot.core.timestamps import Candidate
from elo_bot.db.timestamp_models import TimestampDraft
from elo_bot.features.timestamps.drafts import consume_draft, get_draft, remember_zone, saved_zone
from elo_bot.features.timestamps.publishing import TimestampPublicationError, publish_timestamp
from elo_bot.features.timestamps.ui import candidate_prompt, submitted_candidates, timestamp_choices
from elo_bot.features.timestamps.zones import find_zones, pick_zone


type TimestampContext = ipy.SlashContext | ipy.ComponentContext | ipy.ModalContext
_DRAFT_TTL: Final = timedelta(minutes=15)
_AUTOCOMPLETE_LOOKUP_SECONDS: Final = 0.75
_EXPIRED: Final = "This timestamp selection expired. Run `/timestamp` again."
_FIRST_USE: Final = (
    "Select a country or time zone in `country` the first time you use `/timestamp`.\n\n"
    "I’ll remember your choice. To change it, select a different one the next time you run the command."
)

@dataclass(frozen=True, slots=True)
class TimestampRequest:
    expression: str
    anchor: datetime
    zone: ZoneInfo


class TimestampCommands(ipy.Extension):
    @ipy.slash_command(name="timestamp", description="Post a Discord timestamp from a date or time")
    @ipy.slash_option(name="when", description="When it is, such as in two hours or Thursday at 19:00",
                      opt_type=ipy.OptionType.STRING, required=True, autocomplete=True, max_length=100)
    @ipy.slash_option(name="country", description="Choose your country or time zone the first time",
                      opt_type=ipy.OptionType.STRING, autocomplete=True, max_length=100)
    async def timestamp(self, ctx: ipy.SlashContext, when: str, country: str | None = None) -> None:
        await ctx.defer(ephemeral=True)
        owner = int(ctx.author.id)
        if country is None:
            zone = await saved_zone(owner)
            if zone is None:
                await ctx.send(f"{ERROR_EMOJI} {_FIRST_USE}", ephemeral=True)
                return
        else:
            selection = pick_zone(country)
            if selection is None:
                await ctx.send(f"{ERROR_EMOJI} Choose a country or time zone from the `country` suggestions.",
                               ephemeral=True)
                return
            zone = selection.key
            await remember_zone(owner, zone)

        request = TimestampRequest(when, ctx.id.created_at, ZoneInfo(zone))
        try:
            candidates = submitted_candidates(request.expression, request.anchor, request.zone)
        except TimestampInputError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await self._deliver(ctx, request, candidates)

    @timestamp.autocomplete("country")
    async def country_autocomplete(self, ctx: ipy.AutocompleteContext) -> None:
        await ctx.send([ipy.SlashCommandChoice(name=zone.label, value=zone.key)
                        for zone in find_zones(ctx.input_text)[:25]])

    @timestamp.autocomplete("when")
    async def when_autocomplete(self, ctx: ipy.AutocompleteContext) -> None:
        text = ctx.input_text
        country = ctx.kwargs.get("country")
        if isinstance(country, str) and country:
            selection = pick_zone(country)
            zone = selection.key if selection else None
        else:
            try:
                with fail_after(_AUTOCOMPLETE_LOOKUP_SECONDS):
                    zone = await saved_zone(int(ctx.author_id))
            except (TimeoutError, ConnectionFailure):
                await ctx.send([ipy.SlashCommandChoice(name="Time zone lookup unavailable · Select country",
                                                        value=text or "in two hours")])
                return
        choices = timestamp_choices(text, ctx.id.created_at, ZoneInfo(zone) if zone else None)
        if not choices:
            hint = "Try 19:00, tmrw at 19:00, or in 2 hours" if zone else "Select country to see dates and times"
            choices = [ipy.SlashCommandChoice(name=hint, value=text or "in two hours")]
        await ctx.send(choices)

    @staticmethod
    async def _reply(ctx: TimestampContext, content: str,
                     control: ipy.Button | ipy.StringSelectMenu | None = None) -> None:
        components = [control] if control else []
        if isinstance(ctx, ipy.ComponentContext):
            await ctx.edit_origin(content=content, components=components)
        else:
            await ctx.send(content, components=components, ephemeral=True)

    async def _post(self, ctx: TimestampContext, candidate: datetime) -> None:
        try:
            await publish_timestamp(ctx, candidate)
        except TimestampPublicationError as error:
            await self._reply(ctx, f"{ERROR_EMOJI} {error}")
            return
        if isinstance(ctx, ipy.SlashContext):
            with suppress(ipy.errors.NotFound):
                await ctx.delete()
        else:
            await self._reply(ctx, f"{SUCCESS_EMOJI} Timestamp posted.")

    async def _deliver(self, ctx: TimestampContext, request: TimestampRequest, candidates: tuple[Candidate, ...]) -> None:
        if len(candidates) == 1 and isinstance(candidates[0], datetime):
            await self._post(ctx, candidates[0])
            return
        draft = await TimestampDraft(id=int(ctx.id), owner_id=int(ctx.author.id), channel_id=int(ctx.channel_id),
                                     expression=request.expression, anchor=int(request.anchor.timestamp()),
                                     zone=request.zone.key, expires_at=datetime.now(UTC) + _DRAFT_TTL).insert()
        content, control = candidate_prompt(draft, candidates)
        await self._reply(ctx, content, control)

    async def _selected(self, ctx: ipy.ComponentContext | ipy.ModalContext,
                        index_text: str) -> tuple[TimestampDraft, Candidate] | None:
        _, draft_id, owner, *_ = ctx.custom_id.split("|")
        if int(owner) != ctx.author.id:
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.", ephemeral=True)
            return None
        draft = await get_draft(int(draft_id), int(owner), int(ctx.channel_id))
        if draft is None:
            await ctx.send(f"{ERROR_EMOJI} {_EXPIRED}", ephemeral=True)
            return None
        try:
            candidates = submitted_candidates(draft.expression, datetime.fromtimestamp(draft.anchor, UTC),
                                             ZoneInfo(draft.zone))
        except TimestampInputError:
            await ctx.send(f"{ERROR_EMOJI} {_EXPIRED}", ephemeral=True)
            return None
        if not index_text.isascii() or not index_text.isdecimal() or len(index_text) > 2:
            await ctx.send(f"{ERROR_EMOJI} {_EXPIRED}", ephemeral=True)
            return None
        index = int(index_text)
        if index >= len(candidates):
            await ctx.send(f"{ERROR_EMOJI} {_EXPIRED}", ephemeral=True)
            return None
        return draft, candidates[index]

    async def _claim(self, ctx: ipy.ComponentContext | ipy.ModalContext, draft: TimestampDraft) -> bool:
        if not await consume_draft(draft):
            await ctx.send(f"{ERROR_EMOJI} {_EXPIRED}", ephemeral=True)
            return False
        await ctx.defer(edit_origin=True)
        return True

    @ipy.component_callback(re.compile(r"^timestamp_choose\|[0-9]{1,20}\|[0-9]{1,20}$"))
    async def choose_timestamp(self, ctx: ipy.ComponentContext) -> None:
        index = ctx.values[0] if ctx.values else "0"
        result = await self._selected(ctx, index)
        if result is None:
            return
        draft, candidate = result
        if isinstance(candidate, datetime):
            if await self._claim(ctx, draft):
                await self._post(ctx, candidate)
            return
        await ctx.send_modal(ipy.Modal(
            ipy.ShortText(label="Time", custom_id="clock", placeholder="19:00 or 7pm", required=True, max_length=20),
            title="Choose a time", custom_id=f"timestamp_time|{draft.id}|{draft.owner_id}|{index}",
        ))

    @ipy.modal_callback(re.compile(r"^timestamp_time\|[0-9]{1,20}\|[0-9]{1,20}\|[0-9]{1,2}$"))
    async def enter_clock(self, ctx: ipy.ModalContext, clock: str) -> None:
        result = await self._selected(ctx, ctx.custom_id.rsplit("|", 1)[-1])
        if result is None:
            return
        draft, candidate = result
        if isinstance(candidate, datetime):
            await ctx.send(f"{ERROR_EMOJI} {_EXPIRED}", ephemeral=True)
            return
        request = TimestampRequest(f"{candidate.isoformat()} at {clock}",
                                   datetime.fromtimestamp(draft.anchor, UTC), ZoneInfo(draft.zone))
        try:
            candidates = submitted_candidates(request.expression, request.anchor, request.zone)
        except TimestampInputError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        if await self._claim(ctx, draft):
            await self._deliver(ctx, request, candidates)
