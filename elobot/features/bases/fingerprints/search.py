import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from io import BytesIO
from typing import Literal, assert_never

import interactions as ipy
from beanie import PydanticObjectId
from pydantic import ConfigDict, ValidationError
from pymongo.errors import PyMongoError

from elobot.features.bases.fingerprints.types import (
    BaseFingerprint, DISTRICTS, FingerprintError, HALL_TYPES, ImageAttachment, LayoutMatch,
    PIPELINE_IDENTITIES, MatchScore,
)
from elobot.config.emojis import BASE_QUALITY_EMOJIS, ERROR_EMOJI, LOADING_EMOJI
from elobot.config.presentation import COLOR
from elobot.config.ids import LVL1_ROLES, LVL3_ROLES, WHITELISTED_STAFFS
from elobot.discord.pagination import build_page_button, parse_page_number
from elobot.discord.mentions import mention_user
from elobot.discord.members import member_guild
from elobot.db import BaseLayout, PendingBaseSubmission


def can_search(ctx, review: bool = False) -> bool:
    return (ctx.author.has_any_role(LVL1_ROLES if review else LVL3_ROLES) or
            int(ctx.author.id) in WHITELISTED_STAFFS)


def visibility_filter(ctx, district: str) -> dict:
    query = {"district": district, "quality": {"$ne": "Private"}}
    if not ctx.author.has_any_role(LVL1_ROLES):
        query["builder_id"] = str(ctx.author.id)
    return query


def eligible(base: BaseLayout) -> bool:
    fingerprint = base.fingerprint
    return bool(fingerprint and fingerprint.source_revision == base.image_revision and
                fingerprint.district_id == DISTRICTS.get(base.district) and
                PIPELINE_IDENTITIES.get(fingerprint.pipeline_id) == (
                    fingerprint.schema_version, fingerprint.model_sha256,
                    fingerprint.extractor_revision, fingerprint.coordinate_system))


class UnverifiedMatch(MatchScore):
    model_config = ConfigDict(frozen=True)
    alignment_status: Literal["insufficient", "ambiguous"]


async def find_matches(service, query, filters: dict, *, top: int, minimum: float, fuzzy: bool):
    collection = BaseLayout.get_pymongo_collection()
    filters = {**filters, "quality": {"$ne": "Private"}}
    total = await collection.count_documents(filters)
    indexed = 0
    ranked = []
    batch = []

    async def score_batch():
        nonlocal ranked
        response = await service.cpu.request({"query": query.model_dump(mode="json"), "fuzzy": fuzzy, "ranking": True,
                                             "candidates": [base.fingerprint.model_dump(mode="json") for base in batch]})
        try:
            scores = [LayoutMatch.model_validate(value) for value in response["scores"]]
            if len(scores) != len(batch):
                raise ValueError("Score count does not match candidates")
        except (KeyError, TypeError, ValueError) as exc:
            raise FingerprintError("worker_invalid_response") from exc
        for base, score in zip(batch, scores, strict=True):
            counts = [sum(b.type not in HALL_TYPES for b in item.buildings) for item in (query, base.fingerprint)]
            if score.total != max(counts) or score.matched > min(counts):
                raise FingerprintError("worker_invalid_response")
            if score.similarity is None and (score.verified == 0 or score.total == 0):
                continue
            similarity = score.similarity if score.similarity is not None else round(100 * score.matched / score.total, 1)
            if similarity < minimum:
                continue
            fields = {"base_id": str(base.id), "source_revision": base.image_revision,
                      "similarity": similarity, "matched": score.matched, "total": score.total}
            match score.status:
                case "matched":
                    ranked.append(MatchScore(**fields))
                case "insufficient" | "ambiguous":
                    ranked.append(UnverifiedMatch(**fields, alignment_status=score.status))
                case unreachable:
                    assert_never(unreachable)
        ranked = sorted(ranked, key=lambda item: (-item.similarity, -item.matched, item.base_id))[:top]
        batch.clear()

    async for raw in collection.find(filters).batch_size(50):
        try:
            base = BaseLayout.model_validate(raw)
        except ValidationError:
            continue
        if eligible(base):
            indexed += 1
            batch.append(base)
            if len(batch) == 20:
                await score_batch()
    if batch:
        await score_batch()
    return ranked, indexed, total


@dataclass
class SearchSession:
    owner_id: str
    district: str
    matches: list[MatchScore]
    warnings: list[str]
    review_id: str | None = None
    message_id: str | None = None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    expires_at: float = field(default_factory=lambda: time.monotonic() + 900)
    page: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    query: BaseFingerprint | None = None
    query_attachment: ImageAttachment | None = None
    fuzzy: bool = False


class SearchSessions:
    def __init__(self):
        self.sessions: dict[str, SearchSession] = {}
        self.pending: set[tuple[str, str]] = set()
        self.comparing: set[str] = set()

    def add(self, session: SearchSession) -> None:
        self.sessions = {key: value for key, value in self.sessions.items()
                         if value.expires_at > time.monotonic() and
                         (value.owner_id, value.review_id) != (session.owner_id, session.review_id)}
        if len(self.sessions) >= 256:
            self.sessions.pop(next(iter(self.sessions)))
        self.sessions[session.token] = session


async def render_page(session: SearchSession, ctx, *, page_number: str | None = None) -> dict:
    visible = []
    bases = {}
    for score in session.matches:
        filters = visibility_filter(ctx, session.district)
        filters["_id"] = PydanticObjectId(score.base_id)
        try:
            base = await BaseLayout.find_one(filters)
        except ValidationError:
            continue
        if base and eligible(base) and base.image_revision == score.source_revision:
            visible.append(score)
            bases[score.base_id] = base
    if not can_search(ctx, review=bool(session.review_id)):
        return {"content": f"{ERROR_EMOJI} You no longer have access to this search.",
                "embeds": [], "components": []}
    session.matches = visible
    session.page = max(0, min(session.page, len(visible) - 1))
    if not visible:
        return {"content": f"{ERROR_EMOJI} No results remain available. Run a new search.",
                "embeds": [], "components": []}
    if page_number is not None:
        session.page = parse_page_number(page_number, len(visible)) - 1
    score = visible[session.page]
    base = bases[score.base_id]
    warnings = list(dict.fromkeys([*session.warnings, *base.fingerprint.warnings]))
    score_text = f"{score.similarity:.1f}% - {score.matched}/{score.total}"
    match score:
        case UnverifiedMatch():
            score_text += " (estimate)"
        case MatchScore():
            pass
        case unreachable:
            assert_never(unreachable)
    if session.review_id:
        submission = await PendingBaseSubmission.get(session.review_id)
        if submission is None or submission.resolution is not None:
            warnings.append("This review has since been resolved or removed. These results are read-only.")
    guild = await member_guild(ctx)
    embed = ipy.Embed(
        title=f"**Similar Base {session.page + 1}/{len(visible)}**",
        description=f"{base.link}\n",
        color=COLOR,
        fields=[ipy.EmbedField(name="**Score**", value=score_text),
                ipy.EmbedField(name="**Quality**", value=f"{BASE_QUALITY_EMOJIS[base.quality]} {base.quality}"),
                ipy.EmbedField(name="**Builder**", value=mention_user(
                    await guild.fetch_member(base.builder_id), base.builder_id, mention_type="Builder")
                    if base.builder_id else "Anonymous")],
    )
    if warnings:
        embed.add_field(name="Warnings", value="\n".join(warnings)[:1024])
    embed.set_image(base.image_url)
    controls = [ipy.ActionRow(
        ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="◀️️",
                   custom_id=f"base_similar|{session.token}|previous", disabled=session.page == 0),
        build_page_button(session.page + 1, len(visible), f"base_similar|{session.token}|page"),
        ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="▶️",
                   custom_id=f"base_similar|{session.token}|next", disabled=session.page == len(visible) - 1),
    )]
    if session.query is not None:
        controls.append(ipy.ActionRow(
            ipy.Button(style=ipy.ButtonStyle.SECONDARY, label="Compare", emoji="↔️",
                       custom_id=f"base_compare|{session.token}|{score.base_id}"),
            ipy.Button(style=ipy.ButtonStyle.SECONDARY, label="Show Labels", emoji="🏷️",
                       custom_id=f"base_labels|{session.token}|{score.base_id}"),
        ))
    return {"content": "", "embeds": [embed], "components": controls}


async def show_result_image(sessions: SearchSessions, service, ctx, *, labels: bool = False) -> None:
    title = "Show Labels" if labels else "Compare"
    action = "Labels" if labels else "Comparison"
    filename = "base-labels.png" if labels else "base-comparison.png"
    _, token, base_id = ctx.custom_id.split("|")
    session = sessions.sessions.get(token)
    if session and (session.owner_id != str(ctx.author.id) or session.message_id != str(ctx.message.id)):
        await ctx.send(f"{ERROR_EMOJI} These search controls belong to another response or user.", ephemeral=True)
        return
    if session is None or session.expires_at <= time.monotonic():
        sessions.sessions.pop(token, None)
        await ctx.send(f"{ERROR_EMOJI} This search expired. Run a new search.", ephemeral=True)
        return
    if not can_search(ctx, review=bool(session.review_id)):
        await ctx.send(f"{ERROR_EMOJI} You no longer have access to this search.", ephemeral=True)
        return
    if token in sessions.comparing or len(sessions.comparing) >= 4:
        await ctx.send(f"{LOADING_EMOJI} An image is already pending. Please wait and try again.", ephemeral=True)
        return
    sessions.comparing.add(token)
    try:
        await ctx.defer(ephemeral=True)

        def validate_session():
            if (sessions.sessions.get(token) is not session or session.expires_at <= time.monotonic() or
                    session.query is None):
                raise FingerprintError("This search expired. Run a new search")
            if not can_search(ctx, review=bool(session.review_id)):
                raise FingerprintError("You no longer have access to this search")
            if not 0 <= session.page < len(session.matches) or session.matches[session.page].base_id != base_id:
                raise FingerprintError(f"The selected result changed. Use {title} on the current page")
            return session.matches[session.page]

        async def current_base():
            score = validate_session()
            base = await BaseLayout.find_one({**visibility_filter(ctx, session.district),
                                              "_id": PydanticObjectId(base_id)})
            validate_session()
            if base is None or not eligible(base) or base.image_revision != score.source_revision:
                raise FingerprintError("This result changed or is no longer available. Run a new search")
            return base

        base = await current_base()
        original = base.fingerprint.model_copy(deep=True)
        if labels:
            content = await service.labels(session.query, base, attachment=session.query_attachment)
        else:
            content, _ = await service.compare(session.query, base, fuzzy=session.fuzzy,
                                               attachment=session.query_attachment)
        async with session.lock:
            fresh = await current_base()
            if fresh.fingerprint != original:
                raise FingerprintError("This result changed. Run a new search")
            with BytesIO(content) as image:
                attachment = ipy.File(image, file_name=filename)
                await ctx.send(file=attachment, ephemeral=True)
    except FingerprintError as exc:
        await ctx.send(f"{ERROR_EMOJI} {action} unavailable: {exc}.", ephemeral=True)
    except PyMongoError:
        await ctx.send(f"{ERROR_EMOJI} {action} data is temporarily unavailable. Please retry.", ephemeral=True)
    except ValidationError:
        await ctx.send(f"{ERROR_EMOJI} This result is no longer compatible. Run a new search.", ephemeral=True)
    except asyncio.CancelledError:
        try:
            await ctx.send(f"{ERROR_EMOJI} The image request was cancelled. Please retry {title}.", ephemeral=True)
        except ipy.errors.HTTPException:
            logging.warning("Could not publish cancellation for a base image request")
        raise
    except ipy.errors.HTTPException:
        await ctx.send(f"{ERROR_EMOJI} The image could not be delivered. Please retry {title}.", ephemeral=True)
    finally:
        sessions.comparing.discard(token)
