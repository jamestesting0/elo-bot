import interactions as ipy

base = ipy.SlashCommand(name="match", description="", dm_permission=True)
base_group = base.group(name="base", description="")
war_group = base.group(name="war", description="")
set_group = base.group(name="set", description="")
result_group = base.group(name="result", description="")
