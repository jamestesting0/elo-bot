from typing import Literal

from interactions import Member, User


def mention_user(user: User | Member | None, user_id: int | str = 0,
                 mention_type: Literal["Player", "Builder", "Unknown Builder", "Helper"] = "Player"):
    if user:
        return f"**[{user.display_name}](<https://discordapp.com/users/{user.id}>)**"

    if mention_type == "Player":
        name_filler = "❌Invalid Player"
    elif mention_type == "Builder":
        name_filler = "❓Unknown Builder"
    else:
        name_filler = "❌Invalid Helper"

    return f"**[{name_filler}](<https://discordapp.com/users/{user_id}>)**"
