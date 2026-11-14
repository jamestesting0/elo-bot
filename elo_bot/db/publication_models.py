from pydantic import BaseModel, ConfigDict, JsonValue


class MatchMessagePublication(BaseModel):
    """Final Discord payload bound to one committed match run."""

    model_config = ConfigDict(frozen=True)

    pending: bool = False
    message_id: int
    is_war: bool
    participants: tuple[str, ...]
    times: tuple[int | None, ...]
    hosts: tuple[str | None, ...]
    host_clans: tuple[str | None, ...]
    embeds: list[dict[str, JsonValue]]
    components: list[dict[str, JsonValue]]
