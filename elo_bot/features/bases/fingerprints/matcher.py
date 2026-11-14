from dataclasses import dataclass
from math import ceil

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import linear_sum_assignment
from scipy.spatial.distance import cdist

from elo_bot.features.bases.fingerprints.geometry import Geometry, SETTINGS, distinct, geometry, proposals, shortlist
from elo_bot.db.fingerprint_types import BaseFingerprint
from elo_bot.features.bases.fingerprints.types import LayoutMatch


@dataclass(frozen=True, slots=True)
class Fit:
    transform: NDArray[np.float64]
    query_indices: NDArray[np.int64]
    candidate_indices: NDArray[np.int64]
    residual: float
    spacing: float

    @property
    def order(self) -> tuple:
        return (-len(self.query_indices), self.residual, *self.transform)


def assign(layouts: tuple[Geometry, Geometry], transform: NDArray[np.float64]) -> Fit:
    query, candidate = layouts
    scale, translation = transform[0], transform[1:]
    spacing = min(query.spacing, scale * candidate.spacing)
    tolerance = SETTINGS["tolerance"] * spacing
    qi, ci, residuals = [], [], []
    for name in sorted(query.groups.keys() & candidate.groups.keys()):
        q, c = query.groups[name], candidate.groups[name]
        distances = cdist(query.points[q], scale * candidate.points[c] + translation)
        costs = np.where(distances <= tolerance, distances / tolerance, min(len(q), len(c)) + 1)
        rows, columns = linear_sum_assignment(costs)
        keep = distances[rows, columns] <= tolerance
        qi.extend(q[rows[keep]])
        ci.extend(c[columns[keep]])
        residuals.extend(distances[rows[keep], columns[keep]])
    return Fit(transform, np.array(qi, dtype=int), np.array(ci, dtype=int),
               float(np.median(residuals)) if residuals else float("inf"), spacing)


def refine(layouts: tuple[Geometry, Geometry], fit: Fit) -> NDArray[np.float64] | None:
    if len(fit.query_indices) < 2:
        return None
    query, candidate = layouts
    q, c = query.points[fit.query_indices], candidate.points[fit.candidate_indices]
    qm, cm = q.mean(axis=0), c.mean(axis=0)
    qvar, cvar = np.sum((q - qm) ** 2), np.sum((c - cm) ** 2)
    if min(qvar, cvar) <= 1e-24 or np.sum((q - qm) * (c - cm)) <= 0:
        return None
    scale = np.sqrt(qvar / cvar)
    if not np.isfinite(scale) or not SETTINGS["scale_min"] <= scale <= SETTINGS["scale_max"]:
        return None
    return np.array([scale, *(qm - scale * cm)])


def supported(layouts: tuple[Geometry, Geometry], fit: Fit) -> bool:
    query, candidate = layouts
    if (len(fit.query_indices) < SETTINGS["support"] or fit.residual > SETTINGS["median_residual"] * fit.spacing or
            len({query.classes[index] for index in fit.query_indices}) < SETTINGS["classes"]):
        return False
    for layout, indices in ((query, fit.query_indices), (candidate, fit.candidate_indices)):
        extent = np.ptp(layout.points, axis=0)
        inliers = layout.points[indices]
        if np.any(extent <= 1e-12) or np.any(np.ptp(inliers, axis=0) / extent < SETTINGS["coverage"]):
            return False
        eigenvalues = np.linalg.eigvalsh(np.cov(inliers.T))
        if (not np.isfinite(eigenvalues).all() or eigenvalues[-1] <= 0 or
                eigenvalues[0] / eigenvalues[-1] < SETTINGS["eigenvalue_ratio"]):
            return False
    return True


@dataclass(frozen=True, slots=True)
class Alignment:
    score: LayoutMatch
    pairs: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class AlignmentPolicy:
    fuzzy: bool
    ranking: bool = False


def score_layout(query: BaseFingerprint, candidate: BaseFingerprint, fuzzy: bool) -> LayoutMatch:
    return align_layout(query, candidate, fuzzy).score


def align_layout(query: BaseFingerprint, candidate: BaseFingerprint, fuzzy: bool) -> Alignment:
    return _align_layout(query, candidate, AlignmentPolicy(fuzzy))


def rank_layout(query: BaseFingerprint, candidate: BaseFingerprint, fuzzy: bool) -> LayoutMatch:
    """Retain tentative match counts for ranking without certifying an unreliable alignment."""
    return _align_layout(query, candidate, AlignmentPolicy(fuzzy, ranking=True)).score


def _align_layout(query: BaseFingerprint, candidate: BaseFingerprint, policy: AlignmentPolicy) -> Alignment:
    first, second = geometry(query, policy.fuzzy), geometry(candidate, policy.fuzzy)
    total = max(len(first.points), len(second.points))
    if query.district_id != candidate.district_id:
        return Alignment(LayoutMatch(status="insufficient", total=total))
    for item in (first, second):
        if (len(item.points) < SETTINGS["support"] or not np.isfinite(item.spacing) or item.spacing <= 1e-12 or
                np.any(np.ptp(item.points, axis=0) <= 1e-12)):
            return Alignment(LayoutMatch(status="insufficient", total=total))
    # A canonical direction makes the bounded shortlist identical when a pair is reversed.
    reversed_pair = (first.classes, tuple(first.points.flat)) > (second.classes, tuple(second.points.flat))
    if reversed_pair:
        first, second = second, first
    layouts = first, second
    common = first.groups.keys() & second.groups.keys()
    if (len(common) < SETTINGS["classes"] or
            sum(min(len(first.groups[key]), len(second.groups[key])) for key in common) < SETTINGS["support"]):
        return Alignment(LayoutMatch(status="insufficient", total=total))
    transforms, proposed, truncated = proposals(*layouts)
    unique = distinct(transforms, layouts)
    selected = shortlist(unique, layouts) if len(unique) else unique
    fits = []
    rounds = 0
    for transform in selected:
        fit = assign(layouts, transform)
        rounds += 1
        best = fit
        for _ in range(SETTINGS["refinements"]):
            refined = refine(layouts, fit)
            if refined is None:
                break
            fit = assign(layouts, refined)
            rounds += 1
            if fit.order < best.order:
                best = fit
        if supported(layouts, best) or (policy.ranking and len(best.query_indices) > 0):
            fits.append(best)
    diagnostics = dict(total=total, proposals=proposed, distinct_proposals=len(unique), verified=len(selected),
                       assignment_rounds=rounds, truncated=truncated or len(unique) > len(selected))
    if not fits:
        return Alignment(LayoutMatch(status="insufficient", **diagnostics))
    fits.sort(key=lambda fit: fit.order)
    best = fits[0]
    tolerance = SETTINGS["tolerance"] * best.spacing
    distinct_fits = [best]
    for fit in fits[1:]:
        if all(np.linalg.norm((fit.transform[0] - seen.transform[0]) * second.points +
                              fit.transform[1:] - seen.transform[1:], axis=1).max() >
               SETTINGS["dedup_tolerance"] * tolerance for seen in distinct_fits):
            distinct_fits.append(fit)
    allowance = max(SETTINGS["ambiguity_inliers"], ceil(SETTINGS["ambiguity_fraction"] * total))
    ambiguous = any(len(best.query_indices) - len(fit.query_indices) <= allowance and
                    np.linalg.norm((fit.transform[0] - best.transform[0]) * second.points +
                                   fit.transform[1:] - best.transform[1:], axis=1).max() >
                    SETTINGS["ambiguity_tolerance"] * tolerance for fit in distinct_fits[1:])
    matched = len(best.query_indices)
    reliable = not ambiguous and supported(layouts, best)
    score = LayoutMatch(status="ambiguous" if ambiguous else "matched" if reliable else "insufficient", matched=matched,
                        similarity=round(100 * matched / total, 1) if reliable else None,
                        residual=best.residual / best.spacing, **diagnostics)
    pairs = []
    if reliable:
        for qi, ci in zip(best.query_indices, best.candidate_indices, strict=True):
            pair = first.building_indices[qi], second.building_indices[ci]
            pairs.append(pair[::-1] if reversed_pair else pair)
    return Alignment(score, tuple(sorted(pairs)))
