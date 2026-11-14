import re

from interactions import Client


def parse_commands(bot: Client, input_text, scope: int = 0):
    pattern = r'`/([a-zA-Z]+(?:\s[a-zA-Z]+)*)\??`'
    return re.sub(pattern, lambda m: bot.mention_command(f"{m.group(1)}", scope), input_text)
