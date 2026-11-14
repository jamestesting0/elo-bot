"""Snapshots of the Discord interaction surface and the MongoDB persistence contract.

    python -m tests._contract_snapshots surface|persistence   # print one snapshot as JSON
    python -m tests._contract_snapshots --update               # rewrite tests/snapshots/ after an intended change

The surface covers the synced command payload, every command, component, modal and autocomplete callback,
listeners and Task triggers. The persistence contract covers every Beanie document's settings, JSON schema and
the declared index lists. Each snapshot is collected in a fresh interpreter with dummy credentials.
"""
import enum
import functools
import importlib
import inspect
import json
import os
import pkgutil
import re
import sys
from pathlib import Path

DUMMY_ENV = {
    "BOT_MAIN_TOKEN": "dummy-main-token",
    "BOT_BETA_TOKEN": "dummy-beta-token",
    "COC_API_EMAIL": "dummy@example.invalid",
    "COC_API_PASSWORD": "dummy-coc-password",
    "MONGO_DB_URL": "mongodb://dummy.invalid:27017",
    "HELPER_API_BASE_URL": "https://helper.invalid",
    "HELPER_API_KEY": "dummy-helper-key",
}
UNSET_ENV = ("BASE_FINDER_MODEL_DIR", "FINAL_TEST_MONGO_URI", "INTEGRATION_MONGO_URI")
SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"
SNAPSHOTS = {"surface": "interaction_surface.json", "persistence": "persistence_contract.json"}

_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")
_MOCK_ID = re.compile(r" id='\d+'")
_OBJ_ID = re.compile(r"object at 0x[0-9a-fA-F]+")
_FUNC = re.compile(r"<(function|bound method|coroutine object|class|module) '?([\w.<>]+)'?")
_BOUND_OBJECT = re.compile(r"<(?:[\w]+\.)+(\w+) object>")
SIMPLE = (str, int, float, bool, type(None))


def snapshot_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in UNSET_ENV}
    env.update(DUMMY_ENV)
    env["PYTHONHASHSEED"] = "0"
    return env


def normalize_repr(text: str) -> str:
    text = _ADDR.sub("", text)
    text = _MOCK_ID.sub("", text)
    text = _OBJ_ID.sub("object", text)
    text = _BOUND_OBJECT.sub(r"<\1 object>", text)
    return _FUNC.sub(lambda match: f"<{match.group(1)} {match.group(2).rsplit('.', 1)[-1]}", text)


def describe_fn(fn) -> dict:
    while isinstance(fn, functools.partial):
        fn = fn.func
    out = {"name": getattr(fn, "__name__", type(fn).__name__)}
    qualname = getattr(fn, "__qualname__", "")
    if "<locals>" in qualname:
        out["factory"] = qualname
    values = []
    for cell in getattr(fn, "__closure__", None) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        values.append(describe_fn(value)["name"] if callable(value) else simple(value))
    if values:
        out["closure"] = values
    return out


def simple(value):
    if isinstance(value, enum.Enum):
        return f"{type(value).__name__}.{value.name}"
    if isinstance(value, SIMPLE):
        return value
    if isinstance(value, (set, frozenset)):
        return sorted((simple(v) for v in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [simple(v) for v in value]
    if isinstance(value, dict):
        return {str(k): simple(v) for k, v in value.items()}
    if isinstance(value, re.Pattern):
        return {"pattern": value.pattern, "flags": value.flags}
    if callable(value) and not isinstance(value, type):
        return describe_fn(value)
    return normalize_repr(repr(value))


def describe_attrs_obj(obj, skip=()) -> dict:
    import attrs
    out = {"type": type(obj).__name__}
    fields = attrs.fields_dict(type(obj)) if attrs.has(type(obj)) else vars(obj)
    for name in fields:
        if name in skip:
            continue
        value = getattr(obj, name, None)
        if name == "extension":
            out[name] = getattr(value, "name", None)
        elif name in ("callback", "error_callback", "pre_run_callback", "post_run_callback"):
            out[name] = describe_fn(value) if value else None
        elif name == "checks":
            out[name] = [describe_fn(c) for c in value or []]
        elif name == "autocomplete_callbacks":
            out[name] = {k: describe_fn(v) for k, v in (value or {}).items()}
        else:
            out[name] = simple(value)
    return out


def describe_task(owner: str, name: str, task) -> dict:
    trigger = task.trigger
    state = {key: simple(value) for key, value in vars(trigger).items()
             if key not in ("last_call_time", "_last_call_time", "next_fire_time")}
    return {"owner": owner, "attr": name, "callback": describe_fn(task.callback),
            "trigger": type(trigger).__name__, "state": state}


def collect_surface() -> dict:
    import interactions as ipy
    from interactions.models.internal.application_commands import application_commands_to_dict

    from elo_bot.app import backups, root_callbacks
    from elo_bot.app.extensions import EXTENSIONS

    bot = ipy.Client()
    for name in EXTENSIONS:
        bot.load_extension(name)
    root_callbacks.register(bot)
    root_tasks = {id(v): v for module in (*root_callbacks.ROOT_MODULES, backups)
                  for v in vars(module).values() if isinstance(v, ipy.Task)}
    payload = application_commands_to_dict(bot.interactions_by_scope, bot)
    commands = [{"scope": str(scope), "resolved": resolved, **describe_attrs_obj(cmd)}
                for scope, entries in bot.interactions_by_scope.items() for resolved, cmd in entries.items()]

    def callbacks(literal: dict, regex: dict) -> dict:
        return {
            "literal": [{"id": key, **describe_attrs_obj(cmd, skip=("listeners",))} for key, cmd in literal.items()],
            "regex": [{"pattern": key.pattern, "flags": key.flags, **describe_attrs_obj(cmd, skip=("listeners",))}
                      for key, cmd in regex.items()],
        }

    tasks = [describe_task(ext_name, attr, value) for ext_name, ext in bot.ext.items()
             for attr, value in inspect.getmembers(type(ext)) if isinstance(value, ipy.Task)]
    tasks += [describe_task("<root>", task.callback.__name__, task)
              for task in sorted(root_tasks.values(), key=lambda task: task.callback.__name__)]
    return {
        "payload": {str(scope): cmds for scope, cmds in payload.items()},
        "extensions": [
            {"name": name, "checks": [describe_fn(c) for c in ext.extension_checks],
             "prerun": [describe_fn(c) for c in ext.extension_prerun],
             "postrun": [describe_fn(c) for c in ext.extension_postrun],
             "error": describe_fn(ext.extension_error) if ext.extension_error else None,
             "description": ext.description}
            for name, ext in sorted(bot.ext.items())
        ],
        "commands": commands,
        "components": callbacks(bot._component_callbacks, bot._regex_component_callbacks),
        "modals": callbacks(bot._modal_callbacks, bot._regex_modal_callbacks),
        "listeners": {event: [describe_attrs_obj(listener) for listener in items]
                      for event, items in sorted(bot.listeners.items())},
        "global_autocomplete": {k: describe_fn(v) for k, v in getattr(bot, "_global_autocompletes", {}).items()},
        "tasks": tasks,
    }


def collect_persistence() -> dict:
    from beanie import Document

    import elo_bot
    from elo_bot.app.extensions import EXTENSIONS

    for name in EXTENSIONS:
        importlib.import_module(name)
    modules = {info.name: importlib.import_module(info.name)
               for info in pkgutil.walk_packages(elo_bot.__path__, "elo_bot.")}
    documents = {}
    for module in modules.values():
        for name, cls in vars(module).items():
            if not (isinstance(cls, type) and issubclass(cls, Document) and cls is not Document):
                continue
            if cls.__module__ != module.__name__ or name in documents:
                continue
            settings = getattr(cls, "Settings", None)
            settings_state = {key: simple(getattr(settings, key))
                              for key in sorted(vars(settings)) if settings and not key.startswith("__")}
            documents[name] = {"settings": settings_state, "schema": cls.model_json_schema()}
    index_constants, seen = {}, set()
    for module in modules.values():
        for name, value in vars(module).items():
            if name.endswith("_INDEXES") and isinstance(value, (list, tuple)) and id(value) not in seen:
                seen.add(id(value))
                index_constants.setdefault(name, []).append([simple(getattr(item, "document", item)) for item in value])
    return {"documents": dict(sorted(documents.items())),
            "index_constants": {k: sorted(v, key=repr) for k, v in sorted(index_constants.items())}}


def render(snapshot: dict) -> str:
    return json.dumps(snapshot, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> None:
    if argv == ["--update"]:
        import subprocess
        SNAPSHOT_DIR.mkdir(exist_ok=True)
        for kind, filename in SNAPSHOTS.items():
            result = subprocess.run([sys.executable, "-m", "tests._contract_snapshots", kind], env=snapshot_env(),
                                    cwd=SNAPSHOT_DIR.parents[1], capture_output=True, text=True, check=True)
            (SNAPSHOT_DIR / filename).write_text(result.stdout)
            print(f"wrote tests/snapshots/{filename}")
        return
    if len(argv) != 1 or argv[0] not in SNAPSHOTS:
        raise SystemExit(__doc__)
    os.environ.update(DUMMY_ENV)
    for key in UNSET_ENV:
        os.environ.pop(key, None)
    collect = collect_surface if argv[0] == "surface" else collect_persistence
    sys.stdout.write(render(collect()))


if __name__ == "__main__":
    main(sys.argv[1:])
