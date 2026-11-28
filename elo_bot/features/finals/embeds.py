import interactions as ipy

from elo_bot.discord.embeds import embed_color


def final_season_label(season: int) -> str:
    return f"Season {season} Final"


def final_embed(season: int, title: str, *, description: str | None = None) -> ipy.Embed:
    return ipy.Embed(title=f"**{title}**", author=ipy.EmbedAuthor(name=final_season_label(season)),
                     description=description, color=embed_color())
