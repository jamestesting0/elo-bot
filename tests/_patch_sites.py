"""Patch one name at several import sites with a single shared object.

A test that patched `module.Name` replaced `Name` for every function of that module. When those
functions now live in several modules, `patch_sites(("a.Name", "b.Name"), ...)` gives each site the
same mock, exactly like the original single patch. It accepts every `unittest.mock.patch` argument and
works as a decorator, a context manager and with `start()`/`stop()`.

`patch_family(MATCH_MODULE_FAMILY, name, ...)` does the same for code that picks the name at runtime:
it patches every module of the family that binds the same object as the name's first binder.
"""
import importlib
from unittest import mock

# The modules that together replaced the former cogs/match_commands.py.
MATCH_MODULE_FAMILY = (
    "elo_bot.extensions.match",
    "elo_bot.features.base_store.match_adapters",
    "elo_bot.extensions.match.base_store",
    "elo_bot.extensions.match.bases",
    "elo_bot.extensions.match.dashboards",
    "elo_bot.extensions.match.hosting",
    "elo_bot.extensions.match.lifecycle",
    "elo_bot.extensions.match.queue",
    "elo_bot.extensions.match.result_store",
    "elo_bot.extensions.match.results",
    "elo_bot.extensions.match.settings",
    "elo_bot.features.queue.policy",
    "elo_bot.features.queue.state",
    "elo_bot.features.queue.matching",
    "elo_bot.features.matches.occupancy",
    "elo_bot.features.matches.resources",
    "elo_bot.features.matches.lifecycle",
    "elo_bot.features.matches.attack_bases",
    "elo_bot.features.matches.tips",
    "elo_bot.features.results.recording",
    "elo_bot.features.hosting.live.preflight",
)


# The modules that together hold BaseCommands (elo_bot/extensions/base/).
BASE_MODULE_FAMILY = (
    "elo_bot.extensions.base",
    "elo_bot.extensions.base.extraction",
    "elo_bot.extensions.base.leaderboard",
    "elo_bot.extensions.base.listing",
    "elo_bot.extensions.base.manage",
    "elo_bot.extensions.base.private",
    "elo_bot.extensions.base.search",
    "elo_bot.extensions.base.submissions",
)

class _SitesPatch(mock._patch):
    extra_targets: tuple[str, ...] = ()

    def __enter__(self):
        value = super().__enter__()
        self._extra_patches = [mock.patch(target, new=value) for target in self.extra_targets]
        for extra in self._extra_patches:
            extra.__enter__()
        return value

    def __exit__(self, *exc_info):
        for extra in reversed(self._extra_patches):
            extra.__exit__(*exc_info)
        return super().__exit__(*exc_info)

    def copy(self):
        patcher = super().copy()
        patcher.__class__ = _SitesPatch
        patcher.extra_targets = self.extra_targets
        return patcher


def patch_sites(targets: tuple[str, ...], *args, **kwargs):
    patcher = mock.patch(targets[0], *args, **kwargs)
    patcher.__class__ = _SitesPatch
    patcher.extra_targets = tuple(targets[1:])
    return patcher


def patch_family(family: tuple[str, ...], name: str, *args, **kwargs):
    modules = [importlib.import_module(module) for module in family]
    binders = [module for module in modules if hasattr(module, name)]
    if not binders:
        return patch_sites((f"{family[0]}.{name}",), *args, **kwargs)
    original = getattr(binders[0], name)
    targets = tuple(f"{module.__name__}.{name}" for module in binders if getattr(module, name) is original)
    return patch_sites(targets, *args, **kwargs)
