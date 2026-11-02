from dataclasses import dataclass
from itertools import combinations

import numpy as np
from numpy.typing import NDArray
from scipy.spatial import cKDTree
from scipy.spatial.distance import cdist

from common.base_fingerprints.artifacts import MANIFEST
from common.base_fingerprints.types import BaseFingerprint, HALL_TYPES


SETTINGS = MANIFEST["matcher"]


@dataclass(frozen=True, slots=True)
class Geometry:
    points: NDArray[np.float64]
    classes: tuple[str, ...]
    groups: dict[str, NDArray[np.int64]]
    spacing: float
    building_indices: tuple[int, ...] = ()


def geometry(layout: BaseFingerprint, fuzzy: bool) -> Geometry:
    length = max(layout.image_width, layout.image_height)
    points = sorted(("cannon_spear" if fuzzy and b.type in {"cannon", "spear"} else b.type,
                     b.x * layout.image_width / length, b.y * layout.image_height / length, b.conf, index)
                    for index, b in enumerate(layout.buildings) if b.type not in HALL_TYPES)
    classes = tuple(p[0] for p in points)
    coordinates = np.array([p[1:3] for p in points], dtype=float).reshape(-1, 2)
    groups = {name: np.flatnonzero(np.array(classes) == name) for name in sorted(set(classes))}
    spacing = 0.0
    if len(points) > 1:
        distances = cdist(coordinates, coordinates)
        distances[distances <= 1e-12] = np.inf
        spacing = float(np.median(distances.min(axis=1)))
    return Geometry(coordinates, classes, groups, spacing, tuple(p[4] for p in points))


def edges(layout: Geometry, pair: tuple[str, str]) -> tuple[NDArray[np.float64], bool]:
    starts = layout.points[layout.groups[pair[0]]]
    ends = layout.points[layout.groups[pair[1]]]
    first = np.repeat(starts, len(ends), axis=0)
    second = np.tile(ends, (len(starts), 1))
    vectors = second - first
    lengths = np.linalg.norm(vectors, axis=1)
    keep = lengths >= SETTINGS["edge_spacing"] * layout.spacing
    first, second, vectors, lengths = first[keep], second[keep], vectors[keep], lengths[keep]
    order = np.lexsort((second[:, 1], second[:, 0], first[:, 1], first[:, 0],
                        np.arctan2(vectors[:, 1], vectors[:, 0]), lengths))
    truncated = len(order) > SETTINGS["edges_per_pair"]
    if truncated:
        order = order[np.linspace(0, len(order) - 1, SETTINGS["edges_per_pair"], dtype=int)]
    return np.stack((first[order], second[order]), axis=1), truncated


def proposals(query: Geometry, candidate: Geometry) -> tuple[NDArray[np.float64], int, bool]:
    common = query.groups.keys() & candidate.groups.keys()
    pairs = sorted(combinations(sorted(common), 2), key=lambda pair: (
        len(query.groups[pair[0]]) * len(query.groups[pair[1]]) *
        len(candidate.groups[pair[0]]) * len(candidate.groups[pair[1]]), pair))
    truncated = len(pairs) > SETTINGS["class_pairs"]
    transforms = []
    count = 0
    for pair in pairs[:SETTINGS["class_pairs"]]:
        qe, qt = edges(query, pair)
        ce, ct = edges(candidate, pair)
        truncated |= qt or ct
        if not len(qe) or not len(ce):
            continue
        q = np.repeat(qe, len(ce), axis=0)
        c = np.tile(ce, (len(qe), 1, 1))
        count += len(q)
        u, v = q[:, 1] - q[:, 0], c[:, 1] - c[:, 0]
        scale = np.linalg.norm(u, axis=1) / np.linalg.norm(v, axis=1)
        translation = (q - scale[:, None, None] * c).mean(axis=1)
        tolerance = SETTINGS["tolerance"] * np.minimum(query.spacing, scale * candidate.spacing)
        residual = np.linalg.norm(q - (scale[:, None, None] * c + translation[:, None]), axis=2).max(axis=1)
        keep = ((np.sum(u * v, axis=1) > 0) & (scale >= SETTINGS["scale_min"]) &
                (scale <= SETTINGS["scale_max"]) & (residual <= 2 * tolerance))
        transforms.extend(np.column_stack((scale[keep], translation[keep])))
    if not transforms:
        return np.empty((0, 3)), count, truncated
    return np.unique(np.array(transforms), axis=0), count, truncated


def distinct(transforms: NDArray[np.float64], layouts: tuple[Geometry, Geometry]) -> NDArray[np.float64]:
    query, candidate = layouts
    if not len(transforms):
        return transforms
    distances = cdist(candidate.points, candidate.points)
    first, second = np.unravel_index(np.argmax(distances), distances.shape)
    ends = candidate.points[[first, second]]
    embedded = (transforms[:, :1, None] * ends + transforms[:, None, 1:]).reshape(-1, 4)
    tree = cKDTree(embedded)
    removed = np.zeros(len(transforms), dtype=bool)
    selected = []
    for index, transform in enumerate(transforms):
        if removed[index]:
            continue
        selected.append(index)
        tolerance = SETTINGS["dedup_tolerance"] * SETTINGS["tolerance"] * min(
            query.spacing, transform[0] * candidate.spacing)
        neighbors = np.asarray(tree.query_ball_point(embedded[index], np.sqrt(2) * tolerance), dtype=int)
        neighbors = neighbors[(neighbors > index) & ~removed[neighbors]]
        for start in range(0, len(neighbors), 64):
            indices = neighbors[start:start + 64]
            delta = transforms[indices] - transform
            displacement = delta[:, :1, None] * candidate.points + delta[:, None, 1:]
            close = np.linalg.norm(displacement, axis=2).max(axis=1) <= tolerance
            removed[indices[close]] = True
    return transforms[selected]


def shortlist(transforms: NDArray[np.float64], layouts: tuple[Geometry, Geometry]) -> NDArray[np.float64]:
    query, candidate = layouts
    supports = np.zeros(len(transforms), dtype=int)
    residuals = np.zeros(len(transforms))
    for name in sorted(query.groups.keys() & candidate.groups.keys()):
        q, c = query.points[query.groups[name]], candidate.points[candidate.groups[name]]
        qt, ct = cKDTree(q), cKDTree(c)
        for start in range(0, len(transforms), 64):
            chunk = transforms[start:start + 64]
            scale, translation = chunk[:, 0], chunk[:, 1:]
            tolerance = SETTINGS["tolerance"] * np.minimum(query.spacing, scale * candidate.spacing)
            forward = qt.query(scale[:, None, None] * c + translation[:, None])[0]
            reverse = ct.query((q - translation[:, None]) / scale[:, None, None])[0] * scale[:, None]
            supports[start:start + len(chunk)] += np.minimum(
                (forward <= tolerance[:, None]).sum(axis=1), (reverse <= tolerance[:, None]).sum(axis=1))
            residuals[start:start + len(chunk)] += np.minimum(forward / tolerance[:, None], 1).sum(axis=1)
            residuals[start:start + len(chunk)] += np.minimum(reverse / tolerance[:, None], 1).sum(axis=1)
    order = np.lexsort((transforms[:, 2], transforms[:, 1], transforms[:, 0], residuals, -supports))
    return transforms[order[:SETTINGS["verified_proposals"]]]
