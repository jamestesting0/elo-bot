"""Patch one name at several import sites with a single shared object.

A test that patched `module.Name` replaced `Name` for every function of that module. When those
functions now live in several modules, `patch_sites(("a.Name", "b.Name"), ...)` gives each site the
same mock, exactly like the original single patch. It accepts every `unittest.mock.patch` argument and
works as a decorator, a context manager and with `start()`/`stop()`.
"""
from unittest import mock


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
