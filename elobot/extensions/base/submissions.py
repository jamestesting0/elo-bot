import asyncio
import logging
import os
import secrets
from datetime import datetime
from typing import assert_never

import anyio
import interactions as ipy
import validators
from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError, PyMongoError

import elobot.extensions.base.groups as groups
from elobot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elobot.config.game import BASE_TIERS
from elobot.config.ids import (
    BASE_SUBMISSION_CHANNEL,
    COMMAND_CHANNEL,
    LVL1_ROLES,
    LVL3_ROLES,
    WHITELISTED_STAFFS,
)
from elobot.db import BaseLayout, PendingBaseSubmission
from elobot.db.base_submission_models import SubmissionAcceptance, SubmissionRejection
from elobot.discord.checks import has_any, has_any_id
from elobot.discord.members import member_guild
from elobot.discord.mentions import mention_user
from elobot.features.base_submission.models import (
    AcceptDecision,
    RejectDecision,
    ResolutionDecision,
    SubmissionDraft,
)
from elobot.features.base_submission.parsing import parse_base_district
from elobot.features.base_submission.presentation import (
    ACCEPT_BUTTON_ID,
    ACCEPT_MODAL_ID,
    FIND_SIMILAR_BUTTON_ID,
    REJECT_BUTTON_ID,
    REJECT_MODAL_ID,
    build_resolution_embed,
    build_review_components,
    build_review_embed,
    mark_review_embed,
)
from elobot.features.bases.fingerprints.images import attachment_identity, download_base_screenshot
from elobot.features.bases.fingerprints.types import PIPELINE_ID, ExtractionState
from elobot.features.bases.layouts.queries import _resolve_builder_id
from elobot.features.bases.layouts.ui import _iter_message_components
from elobot.features.bases.screenshots import capture_base_screenshot
from elobot.integrations.files import delete_file


class BaseSubmissionCommands:
    base = groups.base

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
