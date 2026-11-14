import asyncio
import re

import interactions as ipy

from elobot.db import Player, RecordedMatch
from elobot.discord.decorators import create_async_task, lock_files

MATCH_ID_CHOICE_LIMIT = 25


def _numeric_id(match_id: object) -> int:
    text = str(match_id)
    return int(text) if text.isdigit() else -1


def _bump_match_choice_epoch(bot: ipy.Client) -> int:
    epoch = getattr(bot, "match_id_choice_epoch", 0) + 1
    bot.match_id_choice_epoch = epoch
    return epoch


def _readable_display_name(user: object) -> str | None:
    try:
        name = user.display_name
    except AttributeError:
        return None
    if not isinstance(name, str) or not name:
        return None
    return name


def _link_id(ref: object) -> str | None:
    if isinstance(ref, str):
        return ref
    match_id = getattr(ref, "id", None)
    if match_id is None and isinstance(ref, dict):
        match_id = ref.get("$id", ref.get("id"))
    if match_id is None:
        return None
    return str(match_id)


def _player_id_projection() -> dict:
    return {
        "player_ids": {
            "$map": {
                "input": {"$objectToArray": "$players"},
                "as": "player",
                "in": "$$player.k",
            }
        }
    }


def _match_choice_pipeline(match_ids: list[str] | None = None) -> list[dict]:
    pipeline = []
    if match_ids is not None:
        pipeline.append({"$match": {"_id": {"$in": match_ids}}})
    pipeline.append({"$project": _player_id_projection()})
    return pipeline


async def _load_match_choice_rows(match_ids: list[str] | None = None) -> list[dict]:
    if match_ids is not None and not match_ids:
        return []
    cursor = await RecordedMatch.get_pymongo_collection().aggregate(_match_choice_pipeline(match_ids))
    rows = [row async for row in cursor]
    rows.sort(key=lambda row: _numeric_id(row["_id"]), reverse=True)
    return rows


async def _load_player_histories() -> list[tuple[str, list[str]]]:
    histories = []
    cursor = Player.get_pymongo_collection().find({}, {"user_id": 1, "matches": 1})
    async for document in cursor:
        user_id = document.get("user_id")
        if user_id is None:
            continue
        match_ids = []
        for ref in document.get("matches") or []:
            match_id = _link_id(ref)
            if match_id is not None:
                match_ids.append(match_id)
        histories.append((str(user_id), match_ids))
    return histories


async def _player_match_ids(player_id: str) -> list[str] | None:
    document = await Player.get_pymongo_collection().find_one(
        {"user_id": player_id}, {"matches": 1}
    )
    if document is None:
        return None
    match_ids = []
    for ref in document.get("matches") or []:
        match_id = _link_id(ref)
        if match_id is not None:
            match_ids.append(match_id)
    return match_ids


def _pair_ids(row: dict) -> tuple[str, str] | None:
    player_ids = row.get("player_ids") or []
    if len(player_ids) < 2:
        return None
    return str(player_ids[0]), str(player_ids[1])


def _row_user_ids(rows: list[dict]) -> set[str]:
    user_ids = set()
    for row in rows:
        pair = _pair_ids(row)
        if pair is not None:
            user_ids.update(pair)
    return user_ids


def _stored_choice(name: str, match_id: str, search: str) -> dict[str, str]:
    return {"name": name[:100], "value": str(match_id), "search": search}


def _user_match_choice(opponent_name: str, match_id: str) -> dict[str, str]:
    return _stored_choice(
        f"vs. {opponent_name} ({match_id})",
        match_id,
        f"{opponent_name} {match_id}".casefold(),
    )


def _global_match_choice(first_name: str, second_name: str, match_id: str) -> dict[str, str]:
    return _stored_choice(
        f"{first_name} vs. {second_name} ({match_id})",
        match_id,
        f"{first_name} {second_name} {match_id}".casefold(),
    )


def _user_choices_for_player(player_id: str, rows: list[dict], names: dict[str, str]) -> list[dict[str, str]]:
    choices = []
    for row in rows:
        pair = _pair_ids(row)
        if pair is None:
            continue
        first, second = pair
        if player_id == first:
            opponent_id = second
        elif player_id == second:
            opponent_id = first
        else:
            continue
        choices.append(_user_match_choice(names.get(opponent_id, opponent_id), str(row["_id"])))
    return choices


def _global_choices(rows: list[dict], names: dict[str, str]) -> list[dict[str, str]]:
    choices = []
    for row in rows:
        pair = _pair_ids(row)
        if pair is None:
            continue
        first, second = pair
        choices.append(_global_match_choice(
            names.get(first, first), names.get(second, second), str(row["_id"])
        ))
    return choices


def _new_global_choices(rows: list[dict], names: dict[str, str], known_ids: set[str]) -> list[dict[str, str]]:
    return [
        choice for choice in _global_choices(rows, names)
        if choice["value"] not in known_ids
    ]


def _choice_ids(choices: object) -> set[str]:
    if not isinstance(choices, list):
        return set()
    return {choice["value"] for choice in choices}


def drop_match_id_choice(bot: ipy.Client, match_id: str) -> None:
    choices = getattr(bot, "match_id_choices", None)
    if not isinstance(choices, dict):
        return
    match_id = str(match_id)
    for key, stored in list(choices.items()):
        if not isinstance(stored, list):
            continue
        remaining = [choice for choice in stored if choice.get("value") != match_id]
        if len(remaining) != len(stored):
            choices[key] = remaining
            _bump_match_choice_epoch(bot)


def _match_choice_lock(bot: ipy.Client):
    file_locks = getattr(bot, "file_locks", None)
    if not isinstance(file_locks, dict):
        return None
    lock = file_locks["match_id_choices.dict"]
    if not asyncio.iscoroutinefunction(getattr(lock, "acquire", None)):
        return None
    return lock


async def forget_match_id_choice(bot: ipy.Client, match_id: str) -> None:
    lock = _match_choice_lock(bot)
    if lock is None:
        drop_match_id_choice(bot, match_id)
        return
    async with lock:
        drop_match_id_choice(bot, match_id)


async def forget_replaced_match_choice(bot: ipy.Client, match: object) -> None:
    """Drop a recorded match from autocomplete after a corrected result replaces it."""
    operation = getattr(match, "result_operation", None)
    previous = getattr(operation, "previous_record", None) if operation is not None else None
    if previous is None or getattr(previous, "id", None) is None:
        return
    previous_id = str(previous.id)
    current = getattr(operation, "record", None)
    if current is not None and str(getattr(current, "id", "")) == previous_id:
        return
    await forget_match_id_choice(bot, previous_id)


async def clear_match_id_choices(bot: ipy.Client) -> None:
    def clear() -> None:
        choices = getattr(bot, "match_id_choices", None)
        if not isinstance(choices, dict):
            return
        choices.clear()
        _bump_match_choice_epoch(bot)

    lock = _match_choice_lock(bot)
    if lock is None:
        clear()
        return
    async with lock:
        clear()


def _merge_global_choices(bot: ipy.Client, new_choices: list[dict[str, str]]) -> None:
    if not new_choices:
        return
    new_choices.sort(key=lambda choice: _numeric_id(choice["value"]), reverse=True)
    current = bot.match_id_choices.get("all")
    if not isinstance(current, list) or not current:
        bot.match_id_choices["all"] = new_choices
    else:
        newest_current = _numeric_id(current[0]["value"])
        if _numeric_id(new_choices[-1]["value"]) > newest_current:
            bot.match_id_choices["all"] = new_choices + current
        else:
            combined = new_choices + current
            combined.sort(key=lambda choice: _numeric_id(choice["value"]), reverse=True)
            bot.match_id_choices["all"] = combined
    _bump_match_choice_epoch(bot)


def _cached_display_name(bot: ipy.Client, guild: ipy.Guild | None, user_id: str) -> str | None:
    get_member = getattr(guild, "get_member", None) if guild is not None else None
    if get_member is not None:
        member = get_member(user_id)
        if member is not None:
            return _readable_display_name(member)
    get_user = getattr(bot, "get_user", None)
    if get_user is not None:
        user = get_user(user_id)
        if user is not None:
            return _readable_display_name(user)
    return None


def _names_from_cache(bot: ipy.Client, guild: ipy.Guild | None, user_ids: set[str]) -> dict[str, str]:
    return {
        user_id: _cached_display_name(bot, guild, user_id) or user_id
        for user_id in user_ids
    }


async def _fetch_display_names(bot: ipy.Client, user_ids: set[str]) -> dict[str, str]:
    if not user_ids:
        return {}

    async def fetch(user_id: str) -> tuple[str, str]:
        try:
            user = await bot.fetch_user(user_id)
        except ipy.errors.HTTPException:
            return user_id, user_id
        except RuntimeError as error:
            if "already locked" not in str(error):
                raise
            return user_id, user_id
        if user is None:
            return user_id, user_id
        return user_id, _readable_display_name(user) or user_id

    # GET /users shares one rate-limit bucket. interactions.py raises if two
    # requests lock that bucket at the same time.
    lock = getattr(bot, "_display_name_fetch_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        bot._display_name_fetch_lock = lock
    names = {}
    async with lock:
        for user_id in user_ids:
            fetched_id, name = await fetch(user_id)
            names[fetched_id] = name
    return names


async def _display_names(bot: ipy.Client, guild: ipy.Guild | None, user_ids: set[str]) -> dict[str, str]:
    names = _names_from_cache(bot, guild, user_ids)
    missing = {user_id for user_id in user_ids if _cached_display_name(bot, guild, user_id) is None}
    names.update(await _fetch_display_names(bot, missing))
    return names


def select_match_id_choices(
        choices: list[dict[str, str]], user_input: str, limit: int = MATCH_ID_CHOICE_LIMIT
) -> list[dict[str, str]]:
    def visible(choice: dict[str, str]) -> dict[str, str]:
        return {"name": choice["name"], "value": choice["value"]}

    if not user_input:
        selected = []
        seen = set()
        for choice in choices:
            if choice["value"] in seen:
                continue
            seen.add(choice["value"])
            selected.append(visible(choice))
            if len(selected) == limit:
                break
        return selected

    query = user_input.casefold()
    exact = []
    id_prefix = []
    name_prefix = []
    contains = []
    for choice in choices:
        value = choice["value"]
        folded_value = value.casefold()
        search = choice["search"]
        if folded_value == query:
            exact.append(choice)
        elif folded_value.startswith(query):
            id_prefix.append(choice)
        elif search.startswith(query):
            name_prefix.append(choice)
        elif query in search:
            contains.append(choice)

    selected = []
    seen = set()
    for bucket in (exact, id_prefix, name_prefix, contains):
        for choice in bucket:
            if choice["value"] in seen:
                continue
            seen.add(choice["value"])
            selected.append(visible(choice))
            if len(selected) == limit:
                return selected
    return selected


def _option_user_id(value: object) -> str:
    user_id = getattr(value, "id", None)
    if user_id is None:
        return str(value)
    return str(user_id)


async def refresh_user_match_id_choices(bot: ipy.Client, guild: ipy.Guild | None, player_id: str) -> None:
    player_id = str(player_id)
    match_ids = await _player_match_ids(player_id)
    if not match_ids:
        if bot.match_id_choices.get(player_id):
            bot.match_id_choices[player_id] = []
            _bump_match_choice_epoch(bot)
        return

    existing = bot.match_id_choices.get(player_id)
    if not isinstance(existing, list):
        existing = []
    existing_ids = {choice["value"] for choice in existing}
    current_ids = list(dict.fromkeys(match_ids))
    missing_ids = [match_id for match_id in current_ids if match_id not in existing_ids]
    removed = existing_ids - set(current_ids)
    global_ids = _choice_ids(bot.match_id_choices.get("all"))
    missing_global = [match_id for match_id in current_ids if match_id not in global_ids]
    if not missing_ids and not removed:
        if missing_global:
            rows = await _load_match_choice_rows(missing_global)
            names = await _display_names(bot, guild, _row_user_ids(rows))
            _merge_global_choices(bot, _new_global_choices(rows, names, global_ids))
        return

    rows = await _load_match_choice_rows(current_ids if removed or not existing else missing_ids)
    names = await _display_names(bot, guild, _row_user_ids(rows))
    if removed or not existing:
        updated = _user_choices_for_player(player_id, rows, names)
    else:
        updated = _user_choices_for_player(player_id, rows, names) + existing
    updated.sort(key=lambda choice: _numeric_id(choice["value"]), reverse=True)
    bot.match_id_choices[player_id] = updated
    _bump_match_choice_epoch(bot)
    _merge_global_choices(bot, _new_global_choices(rows, names, _choice_ids(bot.match_id_choices.get("all"))))


def _choice_cache(
        rows: list[dict], histories: list[tuple[str, list[str]]], names: dict[str, str]
) -> dict[str, list[dict[str, str]]]:
    row_by_id = {str(row["_id"]): row for row in rows}
    choices = {"all": _global_choices(rows, names)}
    for player_id, match_ids in histories:
        ordered_rows = [
            row_by_id[match_id]
            for match_id in sorted(set(match_ids), key=_numeric_id, reverse=True)
            if match_id in row_by_id
        ]
        player_choices = _user_choices_for_player(player_id, ordered_rows, names)
        if player_choices:
            choices[player_id] = player_choices
    return choices


def _recent_match_choice_pipeline(user_input: str) -> list[dict]:
    pipeline = []
    if user_input.isdigit():
        pipeline.append({"$match": {"_id": {"$regex": f"^{re.escape(user_input)}"}}})
    pipeline.extend((
        {"$sort": {"time": -1}},
        {"$limit": MATCH_ID_CHOICE_LIMIT},
        {"$project": _player_id_projection()},
    ))
    return pipeline


async def fallback_global_match_choices(
        bot: ipy.Client, guild: ipy.Guild | None, user_input: str
) -> list[dict[str, str]]:
    """Answer a staff match-id autocomplete before the full history cache exists."""
    if user_input and not user_input.isdigit():
        return []
    cursor = await RecordedMatch.get_pymongo_collection().aggregate(_recent_match_choice_pipeline(user_input))
    rows = [row async for row in cursor]
    rows.sort(key=lambda row: _numeric_id(row["_id"]), reverse=True)
    names = _names_from_cache(bot, guild, _row_user_ids(rows))
    return select_match_id_choices(_global_choices(rows, names), user_input)


async def publish_match_id_choices(
        bot: ipy.Client, guild: ipy.Guild | None
) -> tuple[list[dict], list[tuple[str, list[str]]], dict[str, str], set[str], int]:
    rows = await _load_match_choice_rows()
    histories = await _load_player_histories()
    user_ids = _row_user_ids(rows)
    names = _names_from_cache(bot, guild, user_ids)
    bot.match_id_choices = _choice_cache(rows, histories, names)
    epoch = _bump_match_choice_epoch(bot)
    missing = {user_id for user_id in user_ids if _cached_display_name(bot, guild, user_id) is None}
    return rows, histories, names, missing, epoch


def _apply_match_id_choice_names(
        bot: ipy.Client, rows: list[dict], histories: list[tuple[str, list[str]]], names: dict[str, str],
        fetched: dict[str, str], epoch: int,
) -> None:
    if getattr(bot, "match_id_choice_epoch", None) != epoch:
        return
    bot.match_id_choices = _choice_cache(rows, histories, {**names, **fetched})
    _bump_match_choice_epoch(bot)


async def hydrate_match_id_choice_names(bot: ipy.Client, snapshot: tuple | None) -> None:
    if not snapshot:
        return
    rows, histories, names, missing, epoch = snapshot
    fetched = await _fetch_display_names(bot, missing)
    if not fetched:
        return
    lock = _match_choice_lock(bot)
    if lock is None:
        _apply_match_id_choice_names(bot, rows, histories, names, fetched, epoch)
        return
    async with lock:
        _apply_match_id_choice_names(bot, rows, histories, names, fetched, epoch)


async def rebuild_match_id_choices(bot: ipy.Client, guild: ipy.Guild | None) -> None:
    await hydrate_match_id_choice_names(bot, await publish_match_id_choices(bot, guild))


@create_async_task
@lock_files("match_id_choices.dict")
async def fetch_user_match_id_choices(bot: ipy.Client, guild: ipy.Guild, player_id: str):
    await refresh_user_match_id_choices(bot, guild, player_id)
