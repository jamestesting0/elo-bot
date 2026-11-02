from typing import Awaitable, Callable

import interactions as ipy

TYPE_CHECK_FUNCTION = Callable[[ipy.BaseContext], Awaitable[bool]]


def has_any(*checks: TYPE_CHECK_FUNCTION) -> TYPE_CHECK_FUNCTION:
    async def check(ctx: ipy.BaseContext) -> bool:
        for check_func in checks:
            if await check_func(ctx):
                return True
        return False

    return check


def has_any_id(*user_ids: int) -> TYPE_CHECK_FUNCTION:
    async def check(ctx: ipy.BaseContext) -> bool:
        return int(ctx.author.id) in user_ids

    return check
