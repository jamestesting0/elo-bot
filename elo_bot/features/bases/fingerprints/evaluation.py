from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from elo_bot.db.fingerprint_types import BaseFingerprint
from elo_bot.features.bases.fingerprints.types import DISTRICTS, ExtractionResult, FingerprintError, LayoutMatch


class Fixture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    file: str = Field(min_length=1)
    district: str
    image_id: str | None = None
    layout_id: str | None = None
    split: Literal["calibration", "held_out"] | None = None
    expected_usable: bool | None = Field(default=None, strict=True)
    capture_id: str | None = None
    derived: bool = False
    hall_condition: Literal["present", "natural_missing", "controlled_removed"] | None = None

    @field_validator("district")
    @classmethod
    def valid_district(cls, value: str) -> str:
        if value not in DISTRICTS:
            raise ValueError("Unknown district")
        return value

    @property
    def labeled(self) -> bool:
        return bool(self.image_id and self.layout_id and self.split and self.expected_usable is not None)


class PairEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    query: int = Field(ge=0)
    candidate: int = Field(ge=0)
    fuzzy: bool
    result: LayoutMatch | None
    legacy_similarity: float | None = None


def legacy_score(query: BaseFingerprint, candidate: BaseFingerprint, fuzzy: bool) -> float | None:
    if not query.anchor_found or not candidate.anchor_found:
        return None
    import numpy as np
    from scipy.optimize import linear_sum_assignment
    from scipy.spatial.distance import cdist

    groups = []
    for fingerprint in (query, candidate):
        hall = next(b for b in fingerprint.buildings if b.type == fingerprint.anchor_type)
        grouped = defaultdict(list)
        for building in fingerprint.buildings:
            name = "cannon_spear" if fuzzy and building.type in {"cannon", "spear"} else building.type
            grouped[name].append((round(building.x - hall.x, 4), round(building.y - hall.y, 4)))
        groups.append(grouped)
    matched = 0
    for name in groups[0].keys() & groups[1].keys():
        distances = cdist(groups[0][name], groups[1][name])
        rows, columns = linear_sum_assignment(distances)
        matched += int(np.sum(distances[rows, columns] <= .05))
    return round(100 * matched / max(query.total_detections, candidate.total_detections), 1)


def load_fixtures(directory: Path) -> tuple[list[Fixture], list[str]]:
    raw = json.loads((directory / "fixtures.json").read_text())
    if not isinstance(raw, list) or not raw:
        raise FingerprintError("fixture_manifest_requires_nonempty_list")
    fixtures = [Fixture.model_validate(row) for row in raw]
    problems = []
    identities, layouts, captures, digests = set(), {}, {}, {}
    for index, item in enumerate(fixtures):
        path = (directory / item.file).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise FingerprintError("fixture_path_outside_directory")
        if not path.is_file():
            problems.append(f"missing_file:{item.file}")
        else:
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest in digests:
                previous = fixtures[digests[digest]]
                problems.append(f"duplicate_image_bytes:{previous.file}:{item.file}")
                if previous.layout_id != item.layout_id:
                    problems.append(f"inconsistent_duplicate_label:{item.file}")
            digests[digest] = index
        if item.image_id in identities and item.image_id is not None:
            problems.append(f"duplicate_image_id:{item.image_id}")
        identities.add(item.image_id)
        if item.layout_id:
            group = item.district, item.split
            if item.layout_id in layouts and layouts[item.layout_id] != group:
                problems.append(f"split_leakage_or_district_conflict:{item.layout_id}")
            layouts[item.layout_id] = group
        if item.capture_id:
            group = item.layout_id, item.split
            if item.capture_id in captures and captures[item.capture_id] != group:
                problems.append(f"capture_split_leakage:{item.capture_id}")
            captures[item.capture_id] = group
        if item.derived and not item.capture_id:
            problems.append(f"derived_capture_id_missing:{item.file}")
    if any(any(value is not None for value in (item.image_id, item.layout_id, item.split, item.expected_usable))
           for item in fixtures) and not all(item.labeled for item in fixtures):
        problems.append("missing_labels:every_image_requires_image_id_layout_id_split_expected_usable")
    return fixtures, problems


def quality_report(fixtures: list[Fixture], extractions: list[ExtractionResult],
                   pairs: list[PairEvaluation], problems: list[str]) -> dict:
    failures = sum(item.fingerprint is None for item in extractions)
    report = {"extraction_failures": failures, "problems": list(problems),
              "quality_gate": "smoke_only", "accuracy_claim": "No statistical accuracy certification.",
              "fixture_coverage": {"images": len(fixtures), "minimum_images": 72,
                 "district_counts": dict(Counter(item.district for item in fixtures)),
                 "missing_districts": sorted(set(DISTRICTS) - {item.district for item in fixtures})}}
    if not all(item.labeled for item in fixtures):
        report["coverage_gaps"] = ["labeled_calibration_and_held_out_dataset_missing",
                                   "independent_raw_captures_and_natural_missed_halls_unverified"]
        if any(any(value is not None for value in (item.image_id, item.layout_id, item.split, item.expected_usable))
               for item in fixtures):
            report["quality_gate"] = "blocked"
        return report
    report["quality_gate"] = "blocked"
    modes = {}
    for split in ("calibration", "held_out"):
        for fuzzy in (False, True):
            rows = [pair for pair in pairs if pair.fuzzy == fuzzy and fixtures[pair.query].split == split]
            expected = {(q, c) for q, query in enumerate(fixtures) for c, candidate in enumerate(fixtures)
                        if q != c and query.split == candidate.split == split and query.district == candidate.district}
            complete = {(pair.query, pair.candidate) for pair in rows} == expected and len(rows) == len(expected)
            if not complete:
                report["problems"].append(f"incomplete_or_duplicate_pairs:{split}:{fuzzy}")
            counts = Counter()
            hall_counts = defaultdict(Counter)
            by_query = defaultdict(list)
            accepted_negatives = []
            for pair in rows:
                query, candidate = fixtures[pair.query], fixtures[pair.candidate]
                if pair.query == pair.candidate or query.image_id == candidate.image_id:
                    raise FingerprintError("self_match_in_evaluation")
                if query.split != candidate.split or query.district != candidate.district:
                    raise FingerprintError("cross_split_or_district_pair")
                positive = query.layout_id == candidate.layout_id
                accepted = pair.result is not None and pair.result.similarity is not None and pair.result.similarity >= 80
                counts["positive_pairs" if positive else "negative_pairs"] += 1
                outcome = ("accepted_positives" if positive else "accepted_negatives") if accepted else (
                    "missed_positives" if positive else "rejected_negatives")
                counts[outcome] += 1
                abstained = pair.result is None or pair.result.similarity is None
                counts["abstentions"] += abstained
                hall = "/".join("present" if extractions[index].fingerprint and
                                extractions[index].fingerprint.anchor_found else "absent_or_unusable"
                                for index in (pair.query, pair.candidate))
                hall_counts[hall][outcome] += 1
                hall_counts[hall]["pairs"] += 1
                hall_counts[hall]["abstentions"] += abstained
                by_query[pair.query].append(pair)
                if accepted and not positive:
                    accepted_negatives.append([query.image_id, candidate.image_id])
            rankings = []
            legacy_losses = []
            for index, fixture in enumerate(fixtures):
                if fixture.split != split or not fixture.expected_usable:
                    continue
                candidates = by_query[index]
                ranked = sorted((p for p in candidates if p.result and p.result.similarity is not None and
                                 p.result.similarity >= 80), key=lambda p: (
                    -p.result.similarity, -p.result.matched, fixtures[p.candidate].image_id))
                correct_rank = next((rank for rank, pair in enumerate(ranked, 1)
                                     if fixtures[pair.candidate].layout_id == fixture.layout_id), None)
                old_ranked = sorted((p for p in candidates if p.legacy_similarity is not None and
                                     p.legacy_similarity >= 80), key=lambda p: (
                    -p.legacy_similarity, fixtures[p.candidate].image_id))
                old_correct = bool(old_ranked and fixtures[old_ranked[0].candidate].layout_id == fixture.layout_id)
                if old_correct and correct_rank != 1:
                    legacy_losses.append(fixture.image_id)
                rankings.append({"image_id": fixture.image_id, "correct_rank": correct_rank,
                                 "candidate_count": len(candidates), "accepted_count": len(ranked),
                                 "legacy_rank1_correct": old_correct if old_ranked else None})
            tp, fp, fn = counts["accepted_positives"], counts["accepted_negatives"], counts["missed_positives"]
            modes[f"{split}/{'fuzzy' if fuzzy else 'strict'}"] = {
                **{key: counts[key] for key in ("positive_pairs", "negative_pairs", "accepted_positives",
                   "accepted_negatives", "missed_positives", "rejected_negatives", "abstentions")},
                "precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None,
                "rankings": rankings, "by_hall": dict(hall_counts), "accepted_negative_ids": accepted_negatives,
                "legacy_ranking_losses": legacy_losses,
                "passed": complete and bool(rankings) and fp == 0 and all(row["correct_rank"] == 1 for row in rankings),
            }
    report["modes"] = modes
    coverage = []
    for district in DISTRICTS:
        for split in ("calibration", "held_out"):
            layouts = defaultdict(set)
            for item in fixtures:
                if item.district == district and item.split == split and not item.derived:
                    layouts[item.layout_id].add(item.capture_id or item.image_id)
            enough = sum(len(captures) >= 2 for captures in layouts.values())
            if enough < 2:
                coverage.append(f"{district}/{split}:need_2_layouts_with_2_independent_raw_captures;have_{enough}")
    for peak in (True, False):
        if not any(item.hall_condition == "natural_missing" and item.expected_usable and result.fingerprint and
                   not result.fingerprint.anchor_found and (item.district == "Capital Peak") == peak
                   for item, result in zip(fixtures, extractions, strict=True)):
            coverage.append(f"natural_missing_hall:{'Peak' if peak else 'non-Peak'}")
    if not any(fixtures[p.query].layout_id != fixtures[p.candidate].layout_id and
               extractions[p.query].fingerprint and extractions[p.candidate].fingerprint and
               abs(extractions[p.query].fingerprint.total_detections -
                   extractions[p.candidate].fingerprint.total_detections) <= 5 for p in pairs):
        coverage.append("similar_count_hard_negatives_missing")
    report["coverage_gaps"] = coverage
    report["unexpected_extraction"] = [item.image_id for item, result in zip(fixtures, extractions, strict=True)
                                      if item.expected_usable != (result.fingerprint is not None)]
    if (not report["problems"] and not coverage and not report["unexpected_extraction"] and
            all(m["passed"] for m in modes.values())):
        report["quality_gate"] = "passed"
    return report
