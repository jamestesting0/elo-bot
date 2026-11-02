import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import random
import resource
import shutil
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.base_fingerprints.artifacts import MANIFEST, MANIFEST_SHA256, SCRATCH
from common.base_fingerprints.evaluation import PairEvaluation, legacy_score, load_fixtures, quality_report
from common.base_fingerprints.runtime import InferenceWorker, selected_device
from common.base_fingerprints.types import (
    BaseFingerprint, DISTRICTS, ExtractionResult, FingerprintError, ImageInput, LayoutMatch,
    MODEL_SHA256, PIPELINE_ID, utcnow,
)


async def evaluate_pairs(worker, fixtures, extractions):
    pairs, timings = [], []
    for split in ("calibration", "held_out"):
        for fuzzy in (False, True):
            for qi, query in enumerate(fixtures):
                if query.split != split:
                    continue
                candidates = [ci for ci, candidate in enumerate(fixtures) if
                              ci != qi and candidate.split == split and candidate.district == query.district]
                usable = []
                qfp = extractions[qi].fingerprint
                for ci in candidates:
                    cfp = extractions[ci].fingerprint
                    if qfp is None or cfp is None:
                        pairs.append(PairEvaluation(query=qi, candidate=ci, fuzzy=fuzzy, result=None))
                    else:
                        usable.append(ci)
                for offset in range(0, len(usable), 20):
                    batch = usable[offset:offset + 20]
                    started = time.perf_counter()
                    response = await worker.request({"query": qfp.model_dump(mode="json"), "fuzzy": fuzzy,
                        "candidates": [extractions[ci].fingerprint.model_dump(mode="json") for ci in batch]})
                    timings.append({"query": query.image_id, "split": split, "fuzzy": fuzzy, "count": len(batch),
                                    "seconds": time.perf_counter() - started})
                    results = [LayoutMatch.model_validate(value) for value in response["scores"]]
                    for ci, result in zip(batch, results, strict=True):
                        pairs.append(PairEvaluation(query=qi, candidate=ci, fuzzy=fuzzy, result=result,
                            legacy_similarity=legacy_score(qfp, extractions[ci].fingerprint, fuzzy)))
    return pairs, timings


def benchmark_layout(count: int, seed: int) -> BaseFingerprint:
    generator = random.Random(seed)
    classes = ["cannon", "tesla", "crusher", "air_bomb", "wizard_tower"]
    buildings = [{"type": classes[index % 5], "x": generator.uniform(.1, .9),
                  "y": generator.uniform(.1, .9), "conf": .9} for index in range(count)]
    return BaseFingerprint(
        schema_version=2, pipeline_id=PIPELINE_ID, model_sha256=MODEL_SHA256,
        extractor_revision=MANIFEST["extractor_revision"], coordinate_system="image-normalized-xy-v2",
        source_revision=0, source_image_sha256="0" * 64, source_image_url="synthetic-benchmark",
        district_id=1, generated_at=utcnow(), image_width=1000, image_height=1000,
        anchor_found=False, anchor_type=None, total_detections=count, confidence_threshold=.25,
        inference_runs=1, expected_count_match=False, buildings=buildings,
        warnings=["Synthetic geometry for timing only; not image detections."])


async def benchmark(worker):
    import numpy as np

    runs = []
    for count, repetitions in ((50, 4), (60, 4), (70, 4), (300, 3)):
        query = benchmark_layout(count, 41)
        payload = {"query": query.model_dump(mode="json"), "fuzzy": False,
                   "candidates": [benchmark_layout(count, 41 if i % 2 == 0 else 100 + i).model_dump(mode="json")
                                  for i in range(20)]}
        await worker.request(payload)
        for _ in range(repetitions):
            start = time.perf_counter()
            response = await worker.request(payload)
            elapsed = time.perf_counter() - start
            runs.append({"detections": count, "candidates": 20, "seconds": elapsed,
                         "scores": response["scores"]})
    normal = [run["seconds"] for run in runs if run["detections"] < 300]
    stress = [run["seconds"] for run in runs if run["detections"] == 300]
    p95 = float(np.percentile(normal, 95))
    return {"evidence": "synthetic geometry, warmed CPU worker including IPC; no image accuracy claim",
            "normal_p95_seconds": p95, "stress_max_seconds": max(stress), "normal_target_seconds": 3,
            "stress_target_seconds": 30, "targets_met": p95 < 3 and max(stress) < 30, "batches": runs}


async def check(args):
    fixtures, problems = load_fixtures(args.fixtures)
    worker = InferenceWorker(selected_device(args.batch_size), "local-check")
    directory = SCRATCH / "local-check" / str(uuid4())
    directory.mkdir(parents=True)
    report = {"device": worker.device, "batch_size": args.batch_size, "images": [],
              "matcher_revision": MANIFEST["matcher"]["revision"], "matcher_settings": MANIFEST["matcher"],
              "frozen_manifest_sha256": MANIFEST_SHA256, "settings_tuned": False,
              "hardware": {"platform": platform.platform(), "machine": platform.machine(),
                           "processor": platform.processor(), "logical_cpus": os.cpu_count()},
              "legacy_comparison": "Offline old scorer on the same detected points with a valid hall; not historical extraction."}
    start = time.monotonic()
    try:
        await worker.start()
        sources, indices = [], []
        extractions = [ExtractionResult(error="source_unavailable") for _ in fixtures]
        for index, fixture in enumerate(fixtures):
            path = (args.fixtures / fixture.file).resolve()
            if not path.is_file():
                continue
            target = directory / f"{index}.image"
            shutil.copyfile(path, target)
            sources.append(ImageInput(path=str(target), source_revision=0, source_image_url=fixture.file,
                                      district_id=DISTRICTS[fixture.district], pipeline_id=PIPELINE_ID))
            indices.append(index)
        for offset in range(0, len(sources), args.batch_size):
            results = await worker.extract(sources[offset:offset + args.batch_size])
            for index, result in zip(indices[offset:offset + args.batch_size], results, strict=True):
                extractions[index] = result
        for fixture, result in zip(fixtures, extractions, strict=True):
            report["images"].append({"fixture": fixture.model_dump(), **result.model_dump(mode="json")})
        report["extraction_seconds_including_startup"] = round(time.monotonic() - start, 3)
        pairs, timings = [], []
        if all(fixture.labeled for fixture in fixtures) and not problems:
            pairs, timings = await evaluate_pairs(worker, fixtures, extractions)
        report.update(quality_report(fixtures, extractions, pairs, problems))
        report["pairs"] = [pair.model_dump(mode="json") for pair in pairs]
        report["score_batches"] = timings
        if args.benchmark:
            if worker.device != "cpu":
                raise FingerprintError("CPU_benchmark_requires_batch_size_1")
            report["performance"] = await benchmark(worker)
        report["seconds"] = round(time.monotonic() - start, 3)
    finally:
        await worker.close()
        report["child_peak_rss_platform_units"] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        report["rss_unit"] = "bytes" if sys.platform == "darwin" else "KiB"
        shutil.rmtree(directory)
        report["cleanup"] = "worker reaped; scratch directory removed"
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--benchmark", action="store_true", help="Measure warmed CPU batches on synthetic geometry")
    arguments = parser.parse_args()
    try:
        report = asyncio.run(check(arguments))
        print(json.dumps(report, indent=2, allow_nan=False))
        passed = report["quality_gate"] in {"passed", "smoke_only"} and not report["problems"]
        if report["quality_gate"] == "smoke_only":
            passed &= not any(row["error"] for row in report["images"])
        passed &= report.get("performance", {}).get("targets_met", True)
        sys.exit(0 if passed else 2)
    except (FingerprintError, OSError, ValueError) as error:
        print(json.dumps({"quality_gate": "blocked", "error": str(error)}))
        sys.exit(2)
