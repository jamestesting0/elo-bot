"""Commands and listeners that live outside any extension.

interactions.py used to collect these from the `__main__` module at login, after every extension had
loaded, in attribute-name order. `register` reproduces exactly that order and timing.
"""
from interactions.models.internal.callback import CallbackObject

from elo_bot.app import errors, events
from elo_bot.features.admin import commands as admin_commands
from elo_bot.features.sticky import commands as sticky_commands

ROOT_MODULES = (errors, events, admin_commands, sticky_commands)


def root_callbacks() -> list[CallbackObject]:
    members = {
        name: value
        for module in ROOT_MODULES
        for name, value in vars(module).items()
        if isinstance(value, CallbackObject)
    }
    return [members[name] for name in sorted(members)]


def register(bot) -> None:
    for callback in root_callbacks():
        try:
            bot.add_command(callback)
        except TypeError:
            continue
