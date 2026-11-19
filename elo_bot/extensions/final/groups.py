import interactions as ipy

from elo_bot.config.ids import GUILD_IDS

final = ipy.SlashCommand(name="final", description="Elo Final management commands",
                        scopes=GUILD_IDS, dm_permission=False)

bracket_group = final.group(name="bracket", description="Create and view the Final bracket")
round_group = final.group(name="round", description="Prepare Final rounds")
match_group = final.group(name="match", description="View and edit Final results")
predictions_group = final.group(name="predictions", description="Open, close or view predictions")
betting_group = final.group(name="betting", description="Manage Final betting markets")
