import interactions as ipy

from common.constants import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from common.finals.types import NODE_IDS
from common.utils import mention_user
from database_models import EloFinal


def pairing_view(final: EloFinal, owner: str, page: int, names: dict[str, str] | None = None):
    names = names or {}
    prefix = f"finalpair|{final.season_number}|{owner}|{final.draft_revision}|{page}|"
    current = f"QF{page + 1}"
    pair = final.manual_draft.get(current, [None, None])
    taken = {user for node, values in final.manual_draft.items() if node != current for user in values if user}
    lines = []
    for node in NODE_IDS[:4]:
        players = final.manual_draft.get(node)
        lines.append(f"{node}: " + (" vs. ".join(names.get(user, mention_user(None, user)) if user else "BYE"
                                               for user in players)
                                   if players else "TBD"))
    embed = ipy.Embed(title=f"**Season {final.season_number} Final · Pairing {page + 1}/4**", color=COLOR,
                      description="\n".join(lines))
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
    controls = []
    for side, action in enumerate(("a", "b")):
        options = [ipy.StringSelectOption(label=final.player_nicknames[user][:100], value=user,
                                         default=pair[side] == user)
                   for user in final.entrant_ids if user not in taken and user != pair[1 - side]]
        if side == 1:
            options.append(ipy.StringSelectOption(label="BYE", value="BYE", default=pair[side] is None))
        if options:
            controls.append(ipy.ActionRow(ipy.StringSelectMenu(*options, custom_id=prefix + action,
                                                              placeholder=f"Player {'A' if side == 0 else 'B / BYE'}")))
    controls.append(ipy.ActionRow(
        ipy.Button(label="Back", style=ipy.ButtonStyle.SECONDARY, custom_id=prefix + "back", disabled=page == 0),
        ipy.Button(label="Next", style=ipy.ButtonStyle.PRIMARY, custom_id=prefix + "next", disabled=page == 3),
        ipy.Button(label="Clear", style=ipy.ButtonStyle.SECONDARY, custom_id=prefix + "clear"),
        ipy.Button(label="Confirm", style=ipy.ButtonStyle.SUCCESS, custom_id=prefix + "confirm"),
        ipy.Button(label="Cancel", style=ipy.ButtonStyle.DANGER, custom_id=prefix + "cancel"),
    ))
    return embed, controls
