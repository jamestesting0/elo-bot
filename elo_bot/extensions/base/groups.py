import interactions as ipy

from elo_bot.config.ids import (
    GUILD_IDS,
)

BASE_QUALITY_CHOICES = [
    ipy.SlashCommandChoice(name="Average", value="Average"),
    ipy.SlashCommandChoice(name="Good", value="Good"),
    ipy.SlashCommandChoice(name="Great", value="Great"),
    ipy.SlashCommandChoice(name="Excellent", value="Excellent")
]

base = ipy.SlashCommand(name="base", description="", scopes=GUILD_IDS)
set_group = base.group(name="set", description="")
private_group = base.group(name="private", description="")
