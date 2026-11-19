import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Final


_COUNTRY_NAMES: Final = {"GB": "United Kingdom", "KR": "South Korea", "KP": "North Korea"}
_POPULAR_ZONES: Final = (
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu",
    "America/Toronto", "America/Winnipeg", "America/Edmonton", "America/Vancouver", "America/Halifax", "America/St_Johns",
    "Australia/Sydney", "Australia/Brisbane", "Australia/Adelaide", "Australia/Perth", "Europe/Berlin",
)
_PRIORITY: Final = {key: rank for rank, key in enumerate(_POPULAR_ZONES)}
_STARTING_ZONES: Final = ("America/New_York", "America/Los_Angeles", "Europe/London", "Europe/Berlin",
                          "America/Toronto", "Asia/Kolkata", "Australia/Sydney", "Asia/Tokyo", "UTC")


def _search_text(text: str) -> str:
    text = "".join(character for character in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(character))
    return " ".join("".join(character if character.isalnum() else " " for character in text).split())


@dataclass(frozen=True, slots=True)
class CountryZone:
    key: str
    label: str
    exact_terms: tuple[str, ...]
    search_terms: tuple[str, ...]


@cache
def _directory() -> tuple[CountryZone, ...]:
    resource = files("tzdata.zoneinfo")
    countries = dict(line.split("\t", 1) for line in (resource / "iso3166.tab").read_text(encoding="utf-8").splitlines()
                     if line and not line.startswith("#"))
    rows = [line.split("\t", 3) for line in (resource / "zone.tab").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")]
    zones_per_country = Counter(row[0] for row in rows)
    zones = [CountryZone("UTC", "UTC", ("utc",), ("utc", "coordinated", "universal", "time"))]
    for code, _, key, *notes in rows:
        country = _COUNTRY_NAMES.get(code, countries[code])
        city = key.rsplit("/", 1)[-1].replace("_", " ")
        region = notes[0].split(" (")[0] if notes else ""
        region_label = f"{region} ({city})" if code == "US" and region and region != city else city
        label = f"{country} · {region_label}" if zones_per_country[code] > 1 else country
        aliases = "UK Britain England" if code == "GB" else "USA US America" if code == "US" else ""
        exact_terms = tuple(_search_text(term) for term in (country, city, code, key, *aliases.split()))
        search_terms = tuple(_search_text(f"{country} {code} {city} {region} {aliases}").split())
        zones.append(CountryZone(key, label[:100], exact_terms, search_terms))
    return tuple(sorted(zones, key=lambda zone: (_PRIORITY.get(zone.key, len(_PRIORITY)), zone.label)))


def find_zones(query: str) -> tuple[CountryZone, ...]:
    normalized = _search_text(query)
    exact = tuple(zone for zone in _directory() if normalized in zone.exact_terms)
    if exact:
        return exact
    words = normalized.split()
    if not words:
        by_key = {zone.key: zone for zone in _directory()}
        return tuple(by_key[key] for key in _STARTING_ZONES)
    return tuple(zone for zone in _directory()
                 if all(any(term.startswith(word) for term in zone.search_terms) for word in words))


def pick_zone(query: str) -> CountryZone | None:
    for zone in _directory():
        if zone.key.casefold() == query.strip().casefold():
            return zone
    matches = find_zones(query)
    return matches[0] if len(matches) == 1 else None
