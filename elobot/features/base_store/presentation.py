import interactions as ipy

from elobot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elobot.config.emojis import ERROR_EMOJI
from elobot.features.base_store.slots import format_base_store_slot_status
from elobot.features.base_store.models import BaseStorePresentationRequest


def format_opponent_bases(request: BaseStorePresentationRequest) -> str:
    opponent = 1 - request.player
    bases = []
    for index in range(len(request.match.final_districts)):
        participant = request.match.participants[opponent]
        base = participant.bases[index] if index < len(participant.bases) else None
        district = request.match.final_districts[index] if index < len(request.match.final_districts) else None
        if base and district:
            bases.append(f"[{district}]({base.link})")
    return "\n".join(bases) if bases else "*No bases recorded yet.*"


def build_store_embed(request: BaseStorePresentationRequest, names: dict[str, str]) -> ipy.Embed:
    player_id = request.match.participants[request.player].user_id
    store = request.match.participants[request.player].base_store
    embed = ipy.Embed(
        title="**Bases Stored**" if request.bases_stored else "**Base Slot Reserved**",
        description=None,
        fields=[
            ipy.EmbedField(name="**Player**", value=names[player_id]),
            ipy.EmbedField(name="**Helper**", value=names[request.helper_id]),
            ipy.EmbedField(
                name="**Host Clan**",
                value=f"[{request.clan.name} ({request.clan.tag})]({request.clan.share_link})",
            ),
            ipy.EmbedField(name="**Slot**", value=store.slot if store else "Unknown"),
        ],
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR,
    )
    if request.failure_reason:
        embed.title = "**Base Storage Incomplete**"
    return embed


def build_manual_store_components(
        request: BaseStorePresentationRequest,
        slot: str,
) -> list[ipy.Button]:
    suffix = reservation_suffix(request)
    return [
        ipy.Button(
            style=ipy.ButtonStyle.GRAY,
            label="Start Match",
            custom_id=f"start_match|{request.player}|{request.clan.tag}|{slot}{suffix}",
            emoji="⚔️",
        ),
        ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Undo",
            custom_id=f"undo_base_storage|{request.match.token}|{request.player}{suffix}",
            emoji=ipy.PartialEmoji(name="🛑"),
        ),
    ]


def build_helper_store_components(request: BaseStorePresentationRequest) -> list[ipy.Button]:
    suffix = reservation_suffix(request)
    return [
        ipy.Button(
            style=ipy.ButtonStyle.GRAY,
            label="Start Match",
            custom_id=f"helper_stored_start|{request.match.token}|{request.player}{suffix}",
            emoji="⚔️",
        ),
        ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Undo",
            custom_id=f"helper_stored_undo|{request.match.token}|{request.player}{suffix}",
            emoji=ipy.PartialEmoji(name="🛑"),
        ),
    ]


def reservation_suffix(request: BaseStorePresentationRequest) -> str:
    store = request.match.participants[request.player].base_store
    return f"|{store.reservation_id}" if store and store.reservation_id else ""


def construct_start_slot_instruction(available_slots: list[str], clan_name: str) -> str:
    if len(available_slots) != 1:
        return ""
    return f"\n\nPlease use the non-occupied base slot `{available_slots[0]}` in **{clan_name}**."


def construct_occupied_slots_error(
        clan_tag: str,
        clan_name: str,
        slot_owners: dict[str, tuple[str, str]],
) -> str:
    slot_status = format_base_store_slot_status(clan_tag, {clan_tag: slot_owners})
    return (
        f"{ERROR_EMOJI} Both base slots in **{clan_name}** are occupied by other matches.\n"
        f"{slot_status}.\n\nPlease choose another clan."
    )
