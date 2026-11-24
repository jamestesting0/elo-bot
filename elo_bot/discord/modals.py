from typing import assert_never

import interactions as ipy
from interactions.client.utils import dict_filter_none
from pydantic import JsonValue


class ModalSelectLabel(ipy.LabelComponent):
    """Omit the message-only disabled flag and unset fields emitted by interactions.py 6.0."""

    def to_dict(self) -> dict[str, JsonValue]:
        label = super().to_dict()
        select = dict_filter_none(label["component"])
        select.pop("disabled", None)
        if "options" in select:
            select["options"] = [dict_filter_none(option) for option in select["options"]]
        label["component"] = select
        return label


def modal_user_id(user: ipy.User | ipy.Member | str) -> str:
    match user:
        case str():
            return user
        case ipy.User(id=user_id) | ipy.Member(id=user_id):
            return str(user_id)
        case unreachable:
            assert_never(unreachable)
