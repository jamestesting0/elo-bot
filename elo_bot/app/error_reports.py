import sys
import traceback

import interactions as ipy

from elo_bot.config.ids import BUG_REPORT_CHANNEL
from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.discord import runtime
from elo_bot.discord.embeds import embed_color, footer_image


async def copy_error_to_channel(embed: ipy.Embed) -> None:
    try:
        channel = await runtime.bot.fetch_channel(BUG_REPORT_CHANNEL)
        if isinstance(channel, ipy.MessageableMixin):
            await channel.send(embed=embed, silent=True)
        else:
            print(f"Bug report channel {BUG_REPORT_CHANNEL} is unavailable", file=sys.stderr)
    except ipy.errors.HTTPException as error:
        print(f"Failed to post to bug report channel {BUG_REPORT_CHANNEL}: {error}", file=sys.stderr)


def log_error_traceback(error: BaseException) -> str:
    full_error = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    print(full_error, file=sys.stderr)
    return full_error


async def report_unexpected_error(error: BaseException) -> None:
    full_error = log_error_traceback(error)
    embed = ipy.Embed(
        title=f"**{str(error)[:252]}**",
        description=f"```\n{full_error[-(4096 - 8):]}\n```",
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=footer_image()),
        color=embed_color(),
    )
    await runtime.bot.owner.send(embed=embed, silent=True)
    await copy_error_to_channel(embed)
