"""Process-wide handles for code that runs outside an interaction.

`elo_bot.app.client.create_client` sets both once at startup. Error listeners, CoC events and backup
tasks read them; extensions keep using `self.bot`.
"""
import interactions as ipy

VERSION: str
bot: ipy.Client
