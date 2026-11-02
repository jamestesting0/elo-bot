import interactions as ipy

from common.constants import BASE_STORE_SLOTS
from database_models import Match


async def resolve_stored_base_message_ids(
        bot: ipy.Client | None,
        match: Match,
        player: int,
) -> tuple[int, ...] | None:
    """Resolve only exact legacy controls; None prevents cleanup when runtime identity is incomplete."""
    store = match.participants[player].base_store
    message_id = store.message_id if store else None
    if bot is None:
        return () if message_id is None else None
    if message_id is not None:
        return (message_id,)
    if store is None:
        if player < len(match.times) and match.times[player]:
            return ()
        if match.thread:
            try:
                await bot.http.get_channel(int(match.thread))
            except ipy.errors.NotFound:
                return ()
        return None
    if store.reservation_id is not None:
        return ()
    if not match.thread or not match.token or not store.clan_tag or store.slot not in BASE_STORE_SLOTS:
        return None

    try:
        channel = await bot.cache.fetch_channel(int(match.thread), force=True)
    except ipy.errors.NotFound:
        return ()
    if channel is None:
        return None

    legacy_controls = {
        f"helper_stored_start|{match.token}|{player}",
        f"helper_stored_undo|{match.token}|{player}",
        f"undo_base_storage|{match.token}|{player}",
        f"start_match|{player}|{store.clan_tag}|{store.slot}",
    }
    message_ids = []
    async for message in channel.history(limit=0):
        if message.author.id != bot.user.id:
            continue
        custom_ids = {
            custom_id
            for row in message.components or []
            for component in getattr(row, "components", ())
            if (custom_id := getattr(component, "custom_id", None)) is not None
        }
        if custom_ids and custom_ids <= legacy_controls:
            message_ids.append(int(message.id))
    return tuple(message_ids)
