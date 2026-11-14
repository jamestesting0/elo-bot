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
    "elobot.extensions.match",
    "elobot.features.base_store.match_adapters",
    "elobot.extensions.match.base_store",
    "elobot.extensions.match.bases",
    "elobot.extensions.match.dashboards",
    "elobot.extensions.match.hosting",
    "elobot.extensions.match.lifecycle",
    "elobot.extensions.match.queue",
    "elobot.extensions.match.results",
    "elobot.features.queue.policy",
    "elobot.features.queue.state",
    "elobot.features.queue.matching",
    "elobot.features.matches.occupancy",
    "elobot.features.matches.resources",
    "elobot.features.matches.lifecycle",
    "elobot.features.matches.attack_bases",
    "elobot.features.matches.tips",
    "elobot.features.results.recording",
    "elobot.features.hosting.live.preflight",
)


# The modules that together hold BaseCommands (elobot/extensions/base/).
BASE_MODULE_FAMILY = (
    "elobot.extensions.base",
    "elobot.extensions.base.extraction",
    "elobot.extensions.base.leaderboard",
    "elobot.extensions.base.listing",
    "elobot.extensions.base.manage",
    "elobot.extensions.base.private",
    "elobot.extensions.base.search",
    "elobot.extensions.base.submissions",
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
