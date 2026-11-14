from elo_bot.config.game import DISTRICT_CODES_REVERSED


def parse_base_district(link: str) -> str | None:
    parts = link.split("%")
    if not link.startswith("https://link.clashofclans.com") or "OpenLayout&id" not in link.split("="):
        return None
    if len(parts) < 3 or parts[1] != "3ACC":
        return None
    return DISTRICT_CODES_REVERSED.get(parts[2])
