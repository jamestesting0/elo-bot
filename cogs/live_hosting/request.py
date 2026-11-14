import asyncio  # noqa: ANYIO_OK - Discord extension owns background expiry tasks beyond one interaction
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import re
import time
from typing import Final, Literal, Protocol
from weakref import WeakValueDictionary

import interactions as ipy

from common.base_store.live import live_pair_ready
from elobot.config.presentation import COLOR
from elobot.config.emojis import CROSS_EMOJI, ERROR_EMOJI, SUCCESS_EMOJI, TICK_EMOJI
from common.results.validation import is_match_finished
from common.matches.timing import hosting_started
from elobot.db import Match


LIVE_REQUEST_TTL_SECONDS: Final = 300
LIVE_REQUEST_PREFIX: Final = "live_match_request"
LIVE_REQUEST_PATTERN: Final = re.compile(
    rf"^{LIVE_REQUEST_PREFIX}\|(accept|reject)\|([^|]+)\|(\d+)\|(\d+)(?:\|([0-9a-f]{{16}}))?$"
)
LIVE_REQUEST_ACCEPT_PATTERN: Final = re.compile(
    LIVE_REQUEST_PATTERN.pattern.replace("(accept|reject)", "accept")
)
LIVE_REQUEST_REJECT_PATTERN: Final = re.compile(
    LIVE_REQUEST_PATTERN.pattern.replace("(accept|reject)", "reject")
)
type LiveRequestAction = Literal["accept", "reject"]


@dataclass(frozen=True, slots=True)
class LiveRequestIdentity:
    token: str
    requester_id: str
    issued_at_ms: int
    stored_reservation_id: str | None = None

    def custom_id(self, action: LiveRequestAction) -> str:
        suffix = f"|{self.stored_reservation_id}" if self.stored_reservation_id else ""
        return f"{LIVE_REQUEST_PREFIX}|{action}|{self.token}|{self.requester_id}|{self.issued_at_ms}{suffix}"

    @property
    def expires_at(self) -> float:
        return self.issued_at_ms / 1000 + LIVE_REQUEST_TTL_SECONDS


@dataclass(frozen=True, slots=True)
class ParsedLiveRequest:
    action: LiveRequestAction
    identity: LiveRequestIdentity


type AcceptedLiveRequest = Callable[
    [ipy.ComponentContext, LiveRequestIdentity, Match, int],
    Awaitable[None],
]


class LiveRequestRuntime(Protocol):
    bot: ipy.Client


def parse_live_request_id(custom_id: str) -> ParsedLiveRequest | None:
    matched = LIVE_REQUEST_PATTERN.fullmatch(custom_id)
    if matched is None:
        return None
    action, token, requester_id, issued_at_ms, stored_reservation_id = matched.groups()
    if action not in {"accept", "reject"}:
        return None
    return ParsedLiveRequest(
        action=action,
        identity=LiveRequestIdentity(
            token=token,
            requester_id=requester_id,
            issued_at_ms=int(issued_at_ms),
            stored_reservation_id=stored_reservation_id,
        ),
    )


def build_live_request_components(
        identity: LiveRequestIdentity,
        *,
        disabled: bool = False,
) -> ipy.ActionRow:
    return ipy.ActionRow(
        ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Reject",
            custom_id=identity.custom_id("reject"),
            emoji=CROSS_EMOJI,
            disabled=disabled,
        ),
        ipy.Button(
            style=ipy.ButtonStyle.SUCCESS,
            label="Accept",
            custom_id=identity.custom_id("accept"),
            emoji=TICK_EMOJI,
            disabled=disabled,
        ),
    )


class LiveRequestCoordinator:
    def __init__(self, runtime: LiveRequestRuntime, accept: AcceptedLiveRequest):
        self.runtime = runtime
        self.accept = accept
        self._locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self._expiry_tasks: set[asyncio.Task[None]] = set()

    async def post(
            self, ctx: ipy.SlashContext | ipy.ComponentContext, match: Match, requester_index: int,
            *, stored_reservation_id: str | None = None,
    ) -> None:
        opponent = await self.runtime.bot.fetch_user(match.participants[1 - requester_index].user_id)
        message = await ctx.channel.send(
            content=opponent.mention,
            embed=ipy.Embed(
                title="Live Match Request",
                description=(
                    "Accept to start this live match with Helper hosting. "
                    "This request expires in 5 minutes."
                ),
                color=COLOR,
            ),
        )
        identity = LiveRequestIdentity(
            token=str(match.token),
            requester_id=str(ctx.author.id),
            issued_at_ms=round(message.created_at.timestamp() * 1000),
            stored_reservation_id=stored_reservation_id,
        )
        components = build_live_request_components(identity)
        message.components = [components]
        await message.edit(components=components)
        self.schedule_expiry(ctx.channel, int(message.id), identity)
        await ctx.send(f"{SUCCESS_EMOJI} Live match request sent to {opponent.mention}.", ephemeral=True)

    def schedule_expiry(
            self,
            channel: ipy.GuildChannel,
            message_id: int,
            identity: LiveRequestIdentity,
    ) -> None:
        task = asyncio.create_task(self.expire(channel, message_id, identity))
        self._expiry_tasks.add(task)
        task.add_done_callback(self._expiry_tasks.discard)

    async def expire(
            self,
            channel: ipy.GuildChannel,
            message_id: int,
            identity: LiveRequestIdentity,
    ) -> None:
        await asyncio.sleep(max(0, identity.expires_at - time.time()))
        lock = self._locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            try:
                message = await channel.fetch_message(message_id)
            except (ipy.errors.NotFound, ipy.errors.Forbidden):
                return
            if message is None or not self._is_pending(message, identity):
                return
            try:
                await self._resolve_message(message, identity, "Live match request expired.")
            except (ipy.errors.NotFound, ipy.errors.Forbidden):
                return

    async def resolve(self, ctx: ipy.ComponentContext) -> None:
        parsed = parse_live_request_id(ctx.custom_id)
        if parsed is None:
            await ctx.send(f"{ERROR_EMOJI} This live match request is invalid.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        message_id = int(ctx.message.id)
        lock = self._locks.setdefault(message_id, asyncio.Lock())
        accepted: tuple[Match, int] | None = None
        async with lock:
            try:
                message = await ctx.channel.fetch_message(message_id)
            except (ipy.errors.NotFound, ipy.errors.Forbidden):
                await ctx.send(
                    f"{ERROR_EMOJI} This live match request has already been resolved or is stale.",
                    ephemeral=True,
                )
                return
            match = await Match.find_one({"token": parsed.identity.token})
            validation = self._validate(ctx, message, match, parsed.identity)
            if validation is not None:
                await ctx.send(f"{ERROR_EMOJI} {validation}", ephemeral=True)
                return
            assert message is not None and match is not None
            if time.time() >= parsed.identity.expires_at:
                await self._resolve_message(message, parsed.identity, "Live match request expired.")
                await ctx.send(f"{ERROR_EMOJI} This live match request has expired.", ephemeral=True)
                return
            requester_index = next(
                index
                for index, participant in enumerate(match.participants[:2])
                if participant.user_id == parsed.identity.requester_id
            )
            if parsed.action == "reject":
                await self._resolve_message(
                    message,
                    parsed.identity,
                    f"Live match request rejected by {ctx.author.mention}.",
                )
                await ctx.send(f"{SUCCESS_EMOJI} Live match request rejected.", ephemeral=True)
                return
            await self._resolve_message(
                message,
                parsed.identity,
                f"Live match request accepted by {ctx.author.mention}. Starting Helper hosting...",
            )
            accepted = (match, requester_index)
        assert accepted is not None
        await ctx.send(
            f"{SUCCESS_EMOJI} Live match request accepted. Starting Helper hosting...",
            ephemeral=True,
        )
        await self.accept(ctx, parsed.identity, *accepted)

    @staticmethod
    async def _resolve_message(
            message: ipy.Message,
            identity: LiveRequestIdentity,
            status: str,
    ) -> None:
        components = build_live_request_components(identity, disabled=True)
        message.components = [components]
        await message.edit(content=status, components=components)

    @staticmethod
    def _is_pending(message: ipy.Message, identity: LiveRequestIdentity) -> bool:
        rows = getattr(message, "components", [])
        components = [component for row in rows for component in getattr(row, "components", [])]
        return (
            len(components) == 2
            and {component.custom_id for component in components} == {
                identity.custom_id("accept"),
                identity.custom_id("reject"),
            }
            and all(not component.disabled for component in components)
            and bool(getattr(message, "embeds", []))
            and message.embeds[0].title == "Live Match Request"
        )

    def _validate(
            self,
            ctx: ipy.ComponentContext,
            message: ipy.Message | None,
            match: Match | None,
            identity: LiveRequestIdentity,
    ) -> str | None:
        if message is None or not self._is_pending(message, identity):
            return "This live match request has already been resolved or is stale."
        if match is None or str(match.thread) != str(ctx.channel.id):
            return "This live match request no longer matches this thread."
        participants = match.participants[:2]
        requester_indexes = [
            index for index, participant in enumerate(participants)
            if participant.user_id == identity.requester_id
        ]
        if len(participants) != 2 or len(requester_indexes) != 1:
            return "This live match request no longer matches its participants."
        opponent_id = participants[1 - requester_indexes[0]].user_id
        if str(ctx.author.id) != opponent_id:
            return "Only the requester's opponent can accept or reject this request."
        if is_match_finished(match, player=0) or is_match_finished(match, player=1):
            return "This match has already finished."
        if hosting_started(match):
            return "This match is already being started."
        if identity.stored_reservation_id and not live_pair_ready(match, identity.stored_reservation_id):
            return "The stored bases changed or are no longer ready. Use the current storage message."
        return None
