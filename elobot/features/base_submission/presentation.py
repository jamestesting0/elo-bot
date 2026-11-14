from typing import Final, assert_never

import interactions as ipy

from elobot.features.base_submission.models import (
    AcceptDecision,
    RejectDecision,
    ResolutionDecision,
    ReviewStatus,
    SubmissionDraft,
)
from elobot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT, GREEN_COLOR, RED_COLOR
from elobot.config.emojis import CROSS_EMOJI, TICK_EMOJI
from elobot.db import PendingBaseSubmission
from elobot.discord.mentions import mention_user


ACCEPT_BUTTON_ID: Final = "base_submission_accept"
REJECT_BUTTON_ID: Final = "base_submission_reject"
FIND_SIMILAR_BUTTON_ID: Final = "base_submission_find_similar"
ACCEPT_MODAL_ID: Final = "base_submission_accept_modal"
REJECT_MODAL_ID: Final = "base_submission_reject_modal"


def build_review_components(disabled: bool = False) -> ipy.ActionRow:
    return ipy.ActionRow(
        ipy.Button(
            style=ipy.ButtonStyle.SUCCESS,
            label="Accept",
            custom_id=ACCEPT_BUTTON_ID,
            emoji=TICK_EMOJI,
            disabled=disabled,
        ),
        ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Reject",
            custom_id=REJECT_BUTTON_ID,
            emoji=CROSS_EMOJI,
            disabled=disabled,
        ),
        ipy.Button(
            style=ipy.ButtonStyle.SECONDARY, label="Find Similar", emoji="🔍",
            custom_id=FIND_SIMILAR_BUTTON_ID, disabled=disabled,
        ),
    )


def build_review_embed(draft: SubmissionDraft) -> ipy.Embed:
    fields = [
        ipy.EmbedField(name="Submitter", value=draft.submitter_display),
        ipy.EmbedField(name="Builder", value=draft.builder_display),
        ipy.EmbedField(name="District", value=draft.district),
        ipy.EmbedField(name="Proposed Quality", value=draft.proposed_quality),
    ]
    if draft.reason:
        fields.append(ipy.EmbedField(name="Submission Reason", value=draft.reason))
    fields.append(ipy.EmbedField(name="Review Status", value=ReviewStatus.PENDING.value))
    embed = ipy.Embed(
        title="Base Submission Review",
        description=draft.link,
        fields=fields,
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR,
    )
    embed.set_image(draft.image_url)
    return embed


def _decision_label(decision: ResolutionDecision) -> str:
    match decision:
        case AcceptDecision():
            return ReviewStatus.ACCEPTED.value
        case RejectDecision():
            return ReviewStatus.REJECTED.value
        case unreachable:
            assert_never(unreachable)


def build_resolution_embed(submission: PendingBaseSubmission, decision: ResolutionDecision) -> ipy.Embed:
    match decision:
        case AcceptDecision(quality=quality, note=note):
            title = "Base Submission Accepted"
            color = GREEN_COLOR
            fields = [
                ipy.EmbedField(name="District", value=submission.district),
                ipy.EmbedField(name="Quality", value=quality),
            ]
            if note:
                fields.append(ipy.EmbedField(name="Moderator Note", value=note))
        case RejectDecision(reason=reason):
            title = "Base Submission Rejected"
            color = RED_COLOR
            fields = [
                ipy.EmbedField(name="District", value=submission.district),
                ipy.EmbedField(name="Reason", value=reason),
            ]
        case unreachable:
            assert_never(unreachable)
    embed = ipy.Embed(
        title=title,
        description=submission.link,
        fields=fields,
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=color,
    )
    embed.set_image(submission.image_url)
    return embed


def mark_review_embed(
    submission: PendingBaseSubmission,
    decision: ResolutionDecision,
    reviewer_id: str,
    names: dict[str, str],
) -> ipy.Embed:
    status = f"{_decision_label(decision)} by {names[reviewer_id]}"
    match decision:
        case AcceptDecision(builder_id=builder_id):
            builder_display = names[builder_id] if builder_id else mention_user(None, mention_type="Builder")
        case RejectDecision():
            builder_display = (names[submission.builder_id] if submission.builder_id
                               else mention_user(None, mention_type="Builder"))
        case unreachable:
            assert_never(unreachable)
    fields = [
        ipy.EmbedField(name="Submitter", value=names[submission.submitter_id]),
        ipy.EmbedField(name="Builder", value=builder_display),
        ipy.EmbedField(name="District", value=submission.district),
        ipy.EmbedField(name="Proposed Quality", value=submission.proposed_quality),
    ]
    if submission.reason:
        fields.append(ipy.EmbedField(name="Submission Reason", value=submission.reason))
    fields.append(ipy.EmbedField(name="Review Status", value=status))
    match decision:
        case AcceptDecision():
            color = GREEN_COLOR
        case RejectDecision(reason=reason):
            color = RED_COLOR
            fields.append(ipy.EmbedField(name="Rejection Reason", value=reason))
        case unreachable:
            assert_never(unreachable)
    embed = ipy.Embed(
        title="Base Submission Review",
        description=submission.link,
        fields=fields,
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=color,
    )
    embed.set_image(submission.image_url)
    return embed
