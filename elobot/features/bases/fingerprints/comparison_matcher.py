from dataclasses import replace
from typing import Final

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import linear_sum_assignment
from scipy.spatial.distance import cdist

from elobot.features.bases.fingerprints.geometry import (
    SETTINGS,
    Geometry,
    distinct,
    geometry,
    proposals,
    shortlist,
)
from elobot.features.bases.fingerprints.matcher import Alignment, Fit, refine
from elobot.features.bases.fingerprints.types import BaseFingerprint, ComparisonMatch

TOLERANCE: Final = .5


def assign_comparison(layouts: tuple[Geometry, Geometry], transform: NDArray[np.float64]) -> Fit:
    query, candidate = layouts
    scale, translation = transform[0], transform[1:]
    spacing = min(query.spacing, scale * candidate.spacing)
    tolerance = TOLERANCE * spacing
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


def align_comparison(query: BaseFingerprint, candidate: BaseFingerprint, fuzzy: bool) -> Alignment:
    """Return the best available correspondences, including an empty match, for visual comparison."""
    first, second = geometry(query, fuzzy), geometry(candidate, fuzzy)
    total = max(len(first.points), len(second.points))
    empty = Alignment(ComparisonMatch(status="matched", similarity=0, matched=0, total=total, residual=0))
    if not len(first.points) or not len(second.points):
        return empty
    first = replace(first, spacing=first.spacing if np.isfinite(first.spacing) and first.spacing > 1e-12 else .025)
    second = replace(second, spacing=second.spacing if np.isfinite(second.spacing) and second.spacing > 1e-12 else .025)
    reversed_pair = (first.classes, tuple(first.points.flat)) > (second.classes, tuple(second.points.flat))
    if reversed_pair:
        first, second = second, first
    common = first.groups.keys() & second.groups.keys()
    if not common:
        return empty
    layouts = first, second
    transforms, proposed, truncated = proposals(*layouts)
    seeds = [np.array([1., 0., 0.])]
    for scale in (1., first.spacing / second.spacing):
        if SETTINGS["scale_min"] <= scale <= SETTINGS["scale_max"]:
            seeds.append(np.array([scale, *(np.median(first.points, axis=0) -
                                          scale * np.median(second.points, axis=0))]))
            for name in sorted(common)[:SETTINGS["class_pairs"]]:
                seeds.append(np.array([scale, *(np.median(first.points[first.groups[name]], axis=0) -
                                              scale * np.median(second.points[second.groups[name]], axis=0))]))
    combined = np.vstack([seeds, transforms])
    truncated |= len(combined) > 8192
    unique = distinct(combined[:8192], layouts)
    selected = shortlist(unique, layouts)
    fits = []
    rounds = 0
    for transform in selected:
        fit = assign_comparison(layouts, transform)
        rounds += 1
        best = fit
        for _ in range(SETTINGS["refinements"]):
            refined = refine(layouts, fit)
            if refined is None:
                break
            fit = assign_comparison(layouts, refined)
            rounds += 1
            if fit.order < best.order:
                best = fit
        fits.append(best)
    diagnostics = {"total": total, "proposals": proposed, "distinct_proposals": len(unique), "verified": len(selected),
                   "assignment_rounds": rounds, "truncated": truncated or len(unique) > len(selected)}
    if not fits:
        return empty
    fits.sort(key=lambda fit: fit.order)
    best = fits[0]
    matched = len(best.query_indices)
    score = ComparisonMatch(status="matched", matched=matched, similarity=round(100 * matched / total, 1),
                            residual=best.residual / best.spacing if matched else 0, **diagnostics)
    pairs = []
    for qi, ci in zip(best.query_indices, best.candidate_indices, strict=True):
        pair = first.building_indices[qi], second.building_indices[ci]
        pairs.append(pair[::-1] if reversed_pair else pair)
    return Alignment(score, tuple(sorted(pairs)))
