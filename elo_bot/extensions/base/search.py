import logging
import re
import time

import anyio
import interactions as ipy
from pymongo.errors import PyMongoError

import elo_bot.extensions.base.groups as groups
from elo_bot.config.emojis import ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI
from elo_bot.config.ids import (
    BASE_SUBMISSION_CHANNEL,
    LVL1_ROLES,
    LVL3_ROLES,
    WHITELISTED_STAFFS,
)
from elo_bot.db import PendingBaseSubmission
from elo_bot.discord.checks import has_any, has_any_id
from elo_bot.discord.pagination import InvalidPageError, build_page_jump_modal
from elo_bot.features.base_submission.models import (
    ReviewStatus,
)
from elo_bot.features.base_submission.presentation import (
    ACCEPT_BUTTON_ID,
    FIND_SIMILAR_BUTTON_ID,
    REJECT_BUTTON_ID,
    build_review_components,
)
from elo_bot.features.bases.fingerprints.search import (
    SearchSession,
    can_search,
    find_matches,
    render_page,
    show_result_image,
    visibility_filter,
)
from elo_bot.features.bases.fingerprints.types import DISTRICTS, FingerprintError
from elo_bot.features.bases.layouts.ui import _iter_message_components


class BaseSearchCommands:
    base = groups.base

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
