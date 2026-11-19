from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Concatenate, Literal, assert_never

import interactions as ipy

from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.config.ids import ELO_BLACKLIST_ROLE, GUILD_IDS, LVL1_ROLES
from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal, Player
from elo_bot.features.finals import lifecycle


async def final_member(ctx: ipy.InteractionContext, edition: EloFinal | None = None) -> None:
    if ctx.guild_id not in GUILD_IDS or (edition is not None and edition.guild_id != ctx.guild_id):
        raise FinalError("Use this command in the Final's server.")
    if ctx.author.has_role(ELO_BLACKLIST_ROLE):
        raise FinalError("You are banned from using Elo commands. Please contact the staff team if you have doubts.")


def final_action[Owner, **P](
    callback: Callable[Concatenate[Owner, ipy.InteractionContext, P], Awaitable[None]],
) -> Callable[Concatenate[Owner, ipy.InteractionContext, P], Awaitable[None]]:
    """Answer expected domain failures using the interaction's acknowledged state."""
    @wraps(callback)
    async def wrapped(self: Owner, ctx: ipy.InteractionContext, *args: P.args, **kwargs: P.kwargs) -> None:
        try:
            await final_member(ctx)
            await callback(self, ctx, *args, **kwargs)
        except FinalError as error:
            if isinstance(ctx, ipy.ModalContext) and ctx.deferred and ctx.edit_origin:
                await ctx.edit("@original", content=f"{ERROR_EMOJI} {error}", embeds=[], components=[])
            elif isinstance(ctx, ipy.ComponentContext) and ctx.deferred and ctx.editing_origin:
                await ctx.edit_origin(content=f"{ERROR_EMOJI} {error}", embeds=[], components=[])
            else:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
    return wrapped


type FinalAccess = Literal["member", "manager", "player"]


class FinalChecks:
    async def _access(self, ctx: ipy.InteractionContext, edition: EloFinal | None = None,
                      *, access: FinalAccess = "member") -> None:
        await final_member(ctx, edition)
        match access:
            case "member":
                return
            case "manager":
                if not any(ctx.author.has_role(role) for role in LVL1_ROLES):
                    raise ipy.errors.CommandCheckFailure(ctx.command, ipy.has_any_role(*LVL1_ROLES), ctx)
            case "player":
                if await Player.find_one({"user_id": str(ctx.author.id)}) is None:
                    raise FinalError("Register with `/player register` first.")
            case unreachable:
                assert_never(unreachable)

    async def _edition(self, ctx: ipy.InteractionContext, season: int | None = None,
                       *, access: FinalAccess = "member") -> EloFinal:
        edition = await lifecycle.final_for_view(season)
        await self._access(ctx, edition, access=access)
        return edition

    async def _ongoing_edition(self, ctx: ipy.InteractionContext, final_id: str | None = None,
                               *, access: FinalAccess = "member") -> EloFinal:
        edition = await lifecycle.ongoing_final(final_id)
        await self._access(ctx, edition, access=access)
        return edition
