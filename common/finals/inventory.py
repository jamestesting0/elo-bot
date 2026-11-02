import json
import sys
import time
from collections import Counter
from pathlib import Path


def inventory(records: list[dict], seasons: list[dict] | None = None,
              mappings: dict[str, str] | None = None) -> dict:
    counts = Counter(row.get("season_number") for row in records)
    report = {
        "mode": "offline-dry-run",
        "duplicates": [season for season, count in counts.items() if count > 1],
        "needs_season_mapping": [str(row.get("_id")) for row in records if not row.get("season_id")],
        "legacy_score_records": [str(row.get("_id")) for row in records if any(
            row.get(field) for field in ("quarter_finals", "semi_finals", "final")
        )],
        "records": len(records),
        "proposals": [],
        "blocked": [],
    }
    archives = {}
    for archive in seasons or []:
        archives.setdefault(str(archive.get("_id")), []).append(archive)
    for row in records:
        identity = str(row.get("_id"))
        if row.get("schema_version") == 2:
            continue
        mapping = (mappings or {}).get(identity)
        candidates = archives.get(mapping, [])
        if len(candidates) != 1 or candidates[0].get("end", time.time() + 1) > time.time():
            report["blocked"].append({"id": identity, "reason": "Supply one explicit completed Season document mapping."})
            continue
        if identity in report["legacy_score_records"]:
            report["blocked"].append({"id": identity, "reason": "Retain legacy raw history; three-district scores cannot become seven-district results."})
            continue
        entrants = list(row.get("player_nicknames", {}))
        if len(entrants) > 8 or any(not str(user).isdigit() for user in entrants):
            report["blocked"].append({"id": identity, "reason": "Resolve roster capacity or invalid user identities."})
            continue
        archive = candidates[0]
        report["proposals"].append({"id": identity, "set": {"schema_version": 2,
            "season_id": mapping, "season_number": archive["season_number"], "entrant_ids": entrants,
            "active": False, "state": "REGISTRATION", "revision": 0, "setup_revision": 0}})
    proposed_seasons = Counter(proposal["set"]["season_number"] for proposal in report["proposals"])
    report["mapped_season_conflicts"] = [season for season, count in proposed_seasons.items() if count > 1]
    return report


if __name__ == "__main__":
    export = json.loads(Path(sys.argv[1]).read_text())
    if isinstance(export, list):
        report = inventory(export)
    else:
        if export.get("environment") not in {"elo_bot", "elo_beta"}:
            raise ValueError("Identify the exported environment as elo_bot or elo_beta.")
        report = inventory(export["finals"], export.get("seasons"), export.get("season_mappings"))
        report["environment"] = export["environment"]
    print(json.dumps(report, indent=2))
