# Base fingerprint search

`/base find district screenshot top:5 min_similarity:80 fuzzy:false` searches accessible,
non-Private bases. Submission reviews offer **🔍 Find Similar** with top 10 fuzzy matches
at 80%, one image per page. Results are ephemeral, expire after 15 minutes, and recheck
access, privacy, source revision and compatible format when navigating. Search does not accept or
reject a submission. Results retain the raw base link and the Score, Quality and Builder fields.

Each result has a separate row with **↔️ Compare** and **🏷️ Show Labels**.
Compare sends an ephemeral side-by-side image attachment, without an embed or base link:
the background is slightly dimmed, while unmatched detected buildings are brightened inside
hollow gold outlines. There are no image labels or legends, and nothing is drawn over building centers.
Both hall classes are excluded, and
fuzzy comparisons use the same cannon/spear grouping as the search.

Compare downloads the two exact sources again, verifies their SHA-256 checksums, and uses
fresh schema-2 detections and the search matcher to locate differences. This supports legacy
records without guessing their hall origin. Fresh detections may differ from the saved search
score; missing detections can appear as differences. An unreliable alignment shows an error
instead of a misleading image. Changed, deleted or newly private results, expired sessions and
revoked access are checked before work and again before delivery. Compare is read-only and
never changes review decisions or saved fingerprints. Downloads and rendered files are temporary
and cleaned after completion, failure or cancellation. It uses the existing CPU worker and
dependencies; no database migration, new service or extra setup is required.

Show Labels sends an ephemeral image of the displayed base with colored detection boxes,
building names and confidence scores, without an embed or base link. It keeps the original
image colors and includes hall detections when present.
It uses fresh detections from the verified source, so legacy records work without a backfill.
Labels are rendered on the CPU worker using the detector's existing annotation utility, with
the same access checks, temporary-file cleanup and read-only behavior as Compare. This view
does not add labels to Compare. Search embeds no longer include the explanatory score footer.

## Relative geometry and compatibility

Every search uses one matcher, `relative-geometry-v1`, including v1-v1, v1-v2 and v2-v2 pairs.
It excludes both hall classes, fits one positive uniform scale and translation, and assigns
defenses one-to-one within their class. Fuzzy mode merges only cannon and spear. Rotation,
reflection, separate axis scales and perspective transforms are not supported. Non-square
images are converted back to equally scaled pixel coordinates before fitting.

The score is `100 * matched detected defenses / max(detected defenses in either image)`, rounded
to one decimal. It is not a probability that the bases are identical. Missing/extra defenses
reduce the score; hall-only changes do not. A score requires at least 20 matches across four
classes, sufficient spatial coverage in both images, two-dimensional support, low residuals
and an unambiguous alignment. Lowering the search threshold cannot bypass those checks.
No reliable match is distinct from failed extraction. Undetected details cannot be compared.

The defaults in the verified manifest are **proposed, uncalibrated settings**: tolerance 0.35
times spacing, median residual at most 0.15 times spacing, coverage at least 70% in both axes,
covariance eigenvalue ratio at least 0.05, and scale between 0.5 and 2.0. Work is bounded to
eight class pairs, 32 edges per pair, 8,192 hypotheses, 32 verified proposals and at most 96
assignment rounds. Truncation is reported; this is not a guarantee of the optimal alignment.

New extraction writes schema 2 (`capital-v2-sahi448-hall1-4d63ddd9`, `elo-4`,
`image-normalized-xy-v2`) with observed centers divided by image width/height. The hall may
be absent or the wrong class: those conditions produce distinct warnings and never
change the selected district. Extraction needs 12 non-hall detections, three non-hall classes
and nonzero extent in both axes. This floor is deliberately below the matching floor.
The detector uses two overlapping full-height tiles with 448-pixel inference, fixed confidence
0.25 and 20% overlap. SAHI 0.12.7 merges same-class detections with GREEDYNMM at IOS 0.5;
deterministic cross-class IoU cleanup at 0.8 keeps the highest-confidence overlapping detection.
Each tile uses `rect=False`, float32, no augmentation and YOLO IoU 0.5. There is no additional
full-image pass. `inference_runs=1` means one extraction round containing two serial tile
forward passes. Unexpected counts remain warnings; they never change the threshold or trigger
another attempt. Counts are a heuristic. Final detections are capped at 300 per image.
After overlap cleanup, retain only the highest-confidence detection across both hall classes.
Ties use class name and bounding-box coordinates for a deterministic choice. Other buildings
are retained. The winning hall keeps its detected class; it is not relabeled for the selected district.

Schema 1 (`capital-v1-4d63ddd9`, `elo-1`, `hall-relative-image-xy-v1`) remains readable with its
original unique origin-hall validation. Compatibility checks the complete identity tuple,
including model SHA-256, district and source revision. Unknown or malformed formats do not
enter matching. The manifests live in `elo_bot/features/bases/fingerprints/`; the original verified
settings remain in `legacy_manifest.json`.
The earlier schema-2 pipeline (`capital-v2-relative-4d63ddd9`, `elo-2`) remains readable and
resumable through `previous_manifest.json`, preserving its adaptive full-image 640-pixel
extraction with up to three attempts. The initial SAHI pipeline (`capital-v2-sahi448-4d63ddd9`,
`elo-3`) remains resumable through `previous_sahi_manifest.json`, retaining its original hall detections.
All four pipelines share the same weights. No collection
or index migration is needed. Existing saved fingerprints are not automatically regenerated.

New review buttons stay disabled until the pending submission is saved. The submit command
confirms success only when Discord returns enabled buttons; a publication failure reports
that the submission is saved and will be retried. The minute-based review refresh also repairs
saved, unresolved reviews with the exact three disabled buttons and a matching Pending embed.
It fetches fresh message state and shares the resolution lock, preserving terminal reviews,
partially disabled controls and messages without a saved submission. Restarting the updated
bot lets existing stuck pending reviews recover without resubmitting them.

Staff add, accepted submissions, private add, preset add and image replacement save a
durable extraction request with the base. Image/district replacement advances its revision.
Metadata and link-only renewal do not request extraction. Private bases can be indexed for
maintenance, but are never candidates in either search entry point.

## Installation

Use Python 3.12 and the bot's existing `.venv` for all dependencies in `requirements.txt`.
The worker uses the running bot's Python interpreter in a subprocess, keeping inference
outside Discord's event loop. No separate inference environment is needed.

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/setup_base_finder.py check
```

The pinned weights are a regular Git file at `models/base_finder/best.pt` (40,544,741 bytes),
from upstream commit `23053818290d725eecfd6af0e95d533fc0ee7755`. The expected SHA-256 is
`4d63ddd9a95351c6cc9da0fe964e1e715357f268b9efa01ee3874d43d332a271`.
Loading verifies the artifact and all four immutable manifests, then warms the actual SAHI
path before announcing worker readiness. Local caches, logs and partial downloads
are ignored; the model is not. `scripts/setup_base_finder.py download` is an explicit repair
command: it retrieves only that pinned upstream artifact, locks concurrent installers and
atomically replaces a verified download. Startup and Discord commands never download weights
or install dependencies. `check` never downloads anything.

The worker interpreter is always `sys.executable`; there is no separate interpreter setting.
`BASE_FINDER_MODEL_DIR` defaults to `models/base_finder`; use persistent storage outside
replaceable deployments if needed. Relative model paths resolve against the repository root.
Main and beta must use the **same model
directory**, because the shared CUDA lifetime lock is stored there. Do not delete its lock file
while a worker is alive. Deployment databases and scratch directories remain separate.

Batch size 1 always selects CPU, including on GPU hosts. Batch sizes 2–32 require CUDA device
0. For a Linux CPU-only install, install the official Torch CPU wheels before the unified
requirements:

```sh
.venv/bin/python -m pip install torch==2.7.1 torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install -r requirements.txt
```

For CUDA 12.6, substitute the official `https://download.pytorch.org/whl/cu126` index in the
first command. Verify the actual driver/device on the target host. GPU batches, including
one-image tails and historical calibration reruns, retain CUDA; OOM never falls back to CPU.
Current extraction processes each image's two tiles serially on the selected device; the
requested job batch size does not change the measured one-tile prediction batch size.

## Regeneration and recovery

`/base extract base_link:<autocomplete selection or base link>` queues CPU extraction for one
stored base using the same staff checks as bulk regeneration. Autocomplete uses stable IDs
and cached builder names. Staff can select public bases and their own private bases; the bot
owner retains access to all bases. An already pending/running request for the current pipeline
is reported and reused.
Completed or failed requests can be regenerated without replacing the last valid fingerprint.
The command confirms the saved request, not completed inference; the existing worker performs
the extraction and saves its final status. Private bases remain excluded from similarity searches.

Use `/base extract_status job_id:<returned ID>` to check that request without starting another
extraction. It reports the saved state and failure reason, including requests still waiting to
be picked up by the worker. Status is ephemeral and uses the same staff/private-base access
checks; missing, deleted or inaccessible bases return an unavailable-job response.
The ID always refers to that exact request or run, including when a newer request exists.
Previously saved v1 and earlier v2 outboxes, tasks, leases and bulk enrollment finish with
their original extraction semantics. Missing historical pipeline fields mean v1; new
constructors explicitly request the SAHI pipeline. A fresh single extraction can supersede
an older request without allowing its stale worker to overwrite current state.
Unsupported runs remain waiting and unsupported automatic tasks remain pending with
an `unsupported_pipeline` error. They keep their IDs and membership.

After deployment is separately authorized:

1. Install the compatible reader/runtime and run the model check. Confirm the quality gate below.
2. Existing valid fingerprints remain searchable; forced backfill is unnecessary. For explicit
   regeneration, use `/base extract` or `/base extract_all batch_size:1`. Use a larger requested batch only after CUDA
   validation. The command uses `/base add` staff checks and preflights before creating work.
3. Repeat the command while active to retrieve status. It resumes the existing run. After a
   completed run, another invocation creates fresh regeneration work and reports the previous
   run's final counts. Private and expired bases are included in regeneration.
4. Inspect succeeded, failed, superseded and deleted counts. Replace unavailable screenshots,
   correct setup failures, then run regeneration again. An error is not a successful extraction.

A status message is posted to the configured review channel only if its permissions can be
verified as staff-only. Its IDs are persisted, so progress does not require a live interaction
token. If publication fails, committed work remains saved and repeating the command retrieves
status. `/base info` includes the latest pending/running/ready/failed state.

Enrollment uses an ObjectId cutoff at the start of the run's UTC second and a saved keyset
cursor. Bases created in that second or later use automatic ingestion. A partial unique index
prevents duplicate enrollment; another permits only one active run per deployment. Startup
explicitly installs the new indexes despite the existing Beanie `skip_indexes=True` setting.
Backups already enumerate all non-system collections, including the two new job collections.

Recovery materializes pending base requests and reclaims expired leases (10 minutes, at most
three processing attempts). A conditional base update checks revision, district, request,
pipeline, claim token and lease. The fingerprint and completion receipt are committed together;
task recovery handles a crash before task finalization. Failed same-source regeneration retains
the prior valid fingerprint. Replacing a source immediately makes it ineligible. Workers never
recreate deleted bases or overwrite records, builders, presets or other concurrent metadata.

The CPU worker stays warm. A CUDA worker holds a cross-process device lock until the run ends;
shutdown terminates and reaps workers, and parent death terminates them. Scratch images are
bounded to 12 MiB/24 million pixels, downloads to 30 seconds and inference to 180 seconds.
Cleanup targets only feature-owned deployment directories. Single and bulk extraction download
images to temporary files using browser headers. Saved message/attachment identity forces a
fresh Discord message fetch; expired legacy Discord URLs (HTTP 403/404) get one attempt via
`POST /attachments/refresh-urls`. Only a renewed URL for the same attachment is accepted.
Refreshing a signature does not change the base's image URL or revision. Downloads retain
their timeout, size and public-host checks, and temporary files are removed on success,
failure or cancellation. Deleted images or failed URL renewal still report `source_unavailable`;
retry `/base extract` after recovery, or replace the screenshot if it is no longer available.

## Validation and upgrades

Routine offline tests:

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_base*.py' -v
.venv/bin/python -m unittest discover -v tests
cmp main.py beta.py
```

The separately invoked integration suite uses **only** an isolated MongoDB at
`mongodb://127.0.0.1:27028` and random `fingerprint_test_*` databases, dropped on teardown.
Do not map this port to a real database. Start a disposable Mongo container, run
`.venv/bin/python -m unittest discover -s tests/integration -v`, then remove that container.
The worker tests also exercise actual subprocess shutdown and GPU lock contention.

For smoke checks, prepare a directory with `fixtures.json`:

```json
[{"file": "barbarian.png", "district": "Barbarian Camp"}]
```

```sh
.venv/bin/python scripts/check_base_fingerprints.py --fixtures /path/to/fixtures --batch-size 1
.venv/bin/python scripts/check_base_fingerprints.py --fixtures /path/to/fixtures --batch-size 8
.venv/bin/python scripts/check_base_fingerprints.py --fixtures /path/to/fixtures --batch-size 1 --benchmark
```

The checker rejects paths outside the fixture directory (including escaping symlinks), reports
missing files and every extraction failure, and never scores an image against itself. A file/district
manifest remains `smoke_only`, without accuracy statistics. Exit 2 means a validation gate, input,
extraction or performance check failed; the JSON report explains the result.

For labeled evaluation use these fields on every entry:

```json
[{"image_id":"bc-a-1","file":"barbarian-a-1.png","district":"Barbarian Camp",
  "layout_id":"bc-a","split":"calibration","expected_usable":true,
  "capture_id":"bc-a-raw-1","derived":false,"hall_condition":"natural_missing"}]
```

Use `calibration` or `held_out` splits. Keep all images of one layout in one split, and label
known duplicates consistently. Derived crops/resizes must carry their raw capture's `capture_id`
and `derived:true`; they cannot stand in for independent captures. `hall_condition` is optional
metadata with `present`, `natural_missing` or `controlled_removed`. Duplicate image bytes, duplicate
IDs, partial labels and split leakage block evaluation. Same-layout pairs are positives;
different layouts within a district are negatives. Candidates never cross splits.

The minimum gate requires four layouts per district and two independent raw captures per layout:
72 images total, with two layouts per district for calibration and two held out. Include
similar-count negatives and natural missed-hall images from both Peak and non-Peak districts.
Controlled removal supplements those natural cases. The checker reports each missing condition.

Settings are frozen by verified manifest digest before either split runs. Tune only using
calibration data, then freeze a new matcher revision before inspecting held-out results.
Every designated usable query must rank a correct candidate first at 80%, and no negative pair
may pass 80%. Reports separate strict/fuzzy precision and recall, abstentions, missed positives,
accepted negatives, rankings, hall availability, counts, residuals, proposal truncation and batch
timings. An offline comparison uses the old scorer on the same hall-present detections and reports
ranking losses; it does not recreate the old extraction pipeline. This is a regression gate,
not statistical accuracy certification. Rejecting all queries fails the gate.

`--benchmark` measures warmed CPU batches of 20 candidates with synthetic geometry, including IPC,
at 50/60/70 detections and the 300-detection bound. It reports hardware, child peak RSS (bytes on
macOS, KiB on Linux), actual proposals and assignment counts. Targets are normal p95 under 3 seconds
and every stress batch under 30 seconds. Synthetic timing does not measure image accuracy.

Local evidence on 2026-09-29: two annotated upstream screenshots now extract without halls
(32 and 23 detections); no labeled/raw all-district dataset is available. On the local ARM Mac,
normal-batch p95 was 0.49 seconds and stress maximum 1.07 seconds across 12 normal and 3 stress
batches. These timings are not target-server capacity evidence. CUDA is unavailable.
Nine-district accuracy, natural Peak failures, real GPU agreement/tails/OOM and target-server
capacity remain validation blockers.
No upstream license/reuse permission was found; distribution permission remains a release
blocker before publishing the tracked weights. No deployment or production backfill was run.

Deployment requires separate authorization. Keep all four readers installed together while
historical work is pending; this implementation resumes each known pipeline without changing its ID. Future
extraction changes require a new identity and verified manifest, while scoring changes require
a matcher revision. Do not redefine v1 or delete artifacts referenced by jobs.

After any v2 write, rollback requires a reader that supports v2, or a separately planned restore.
An old binary cannot be assumed to parse v2 BaseLayout documents; rerunning extraction with it
is not a safe rollback procedure. No live migration, restore, production backfill or deployment
was performed for this change.

Reference: [pinned upstream model and algorithms](https://github.com/Preet-Kotak/clan-capital-base-finder/tree/23053818290d725eecfd6af0e95d533fc0ee7755),
[official Torch wheel combinations](https://pytorch.org/get-started/previous-versions/).
