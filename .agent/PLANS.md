# Project Plans
Last updated: 2026-05-19 CST

## MultiScene20 Dynamic RF Fusorosa Recompute 2026-05-19

Dynamic RF has been restarted after the fusorosa sealed-underbody exporter
change. The active production policy is intentionally simple: future Sionna
exports seal `vehicle.mitsubishi.fusorosa` in-place in the generated
`car_<actor_id>.ply`; no CARLA collection, trajectory, TX, or static RF rerun is
needed.

Cleanup state before restart:

```text
accepted episodes: 3000
episodes with fusorosa RF/Sionna artifacts cleared: 2793
episodes without fusorosa preserved: 207
```

Active GPU0-only RF resume:

```text
script/log: datasets/DynamicRadioMap/MultiScene20/logs/run_dynamic_rf_quality_filter_gpu0_latest.sh
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.pid
log: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.log
launcher pid: 997156
main pid: 997265
command: python3 scripts/drd.py process-multi-scene-rf --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --use-gpu --gpu-ids 0 --rf-workers 1
```

Next steps are to let the RF resume finish, then run the normal multi-scene
finalization/index validation. Do not rerun CARLA trajectory collection or
static RF for this fusorosa seal intervention.

## MultiScene20 Dynamic RF Running 2026-05-15

Dynamic RF is now running with two workers on GPU IDs 0 and 1. Handles:

```text
datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_2gpu_latest.pid
datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_2gpu_latest.log
datasets/DynamicRadioMap/MultiScene20/logs/run_dynamic_rf_quality_filter_2gpu_latest.sh
```

The script performs dynamic RF, finalizes the global index, and validates 3000
complete RF episodes. Resume by rerunning the latest script if interrupted. Do
not rerun static RF unless TX/reference geometry changes.

## MultiScene20 Static RF Completed / Dynamic RF Gate 2026-05-15

Static RF cache is complete for the quality-filtered TX catalogs: 800/800 static
TX maps validated, with 184 maps generated and 616 existing maps reused. Do not
rerun static RF unless TX positions or reference exports change.

Remaining blocker for dynamic RF is trajectory collection for
`town04_opt_junction_1249` and assignment rebuild. After replacement collection
finishes, run:

```bash
python3 scripts/drd.py promote-tx-catalogs \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --source candidate_quality_filter
python3 scripts/drd.py repair-multi-scene-metadata \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
```

Then verify 20 active scenes, 3000 accepted episodes, selected-5 assignments, and
static cache validation before starting dynamic RF.

## MultiScene20 TX Quality-Filter Promotion Plan 2026-05-15

A reviewed sidecar set `candidate_quality_filter` now exists for all active
MultiScene20 scenes. It keeps existing good TX IDs and replaces only TXs failing
quality constraints: lane-exclusion clearance <0.5 m, distance to center >44 m,
or distance to route centerline >8 m. The sidecar has 800/800 TXs and zero
post-filter violations.

While replacement trajectory collection is still active, do not promote. After
collection reaches 150 accepted episodes for each replacement scene, inspect:

```text
datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization_quality_filter_20260515/tx_routes_contact_sheet.html
```

If accepted, run `promote-tx-catalogs --source candidate_quality_filter`, then
`repair-multi-scene-metadata`, verify 3000 active accepted episodes and selected-5
assignments, then resume RF/static cache.

## MultiScene20 No-Building Scene Replacement Plan 2026-05-15

The three Town04 scenes that produced no Sionna building geometry after forced
re-export are replaced in the active resolved config and are being recollected
with a replacement-only config. Current replacements are:

```text
town04_opt_junction_1368 -> town04_opt_junction_1249
town04_opt_junction_1061 -> town04_opt_junction_0255
town04_opt_junction_1176 -> town04_opt_junction_0850
```

Active collection command is stored at
`datasets/DynamicRadioMap/MultiScene20/logs/collect_replacement_set2_1gpu_latest.command.sh`
and uses one CARLA worker on GPU 1. Do not start a second replacement collection
process. Monitor accepted counts for only these three scenes until each reaches
150/150. After completion, run metadata repair for the full resolved config,
verify 20 active scenes x 150 accepted episodes and selected-5 TX assignments,
then resume RF/static cache generation on the full resolved config.

## MultiScene20 RF Execution Plan Update 2026-05-14

RF generation is now authorized and running on one GPU only. The selected GPU is
physical GPU 1 (`CUDA_VISIBLE_DEVICES=1`), leaving GPU 0 free except for desktop
processes. The active background script is:

```text
datasets/DynamicRadioMap/MultiScene20/logs/run_rf_pipeline_gpu1_latest.sh
log: datasets/DynamicRadioMap/MultiScene20/logs/rf_pipeline_gpu1_latest.log
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/rf_pipeline_gpu1_latest.pid
```

The script first builds zero-vehicle static RSS cache for all 40 TX per scene,
then validates static metadata, then runs selected-5 dynamic RF through
`process-multi-scene-rf --use-gpu --gpu-ids 1 --rf-workers 1`, finalizes the
multi-scene index, and verifies dynamic RF completeness. If the job or server is
interrupted, rerun the same script; static TXs and dynamic episodes are skipped
only after completeness validation.




## MultiScene20 TX/Collection Coordination Update 2026-05-14

CARLA trajectory collection is complete: `3000 / 3000` accepted trajectories
across all 20 scenes, with no active CARLA/collect processes or RPC/TM ports.
The CARLA lifecycle fix has been validated by the completed 2-worker recovery
run.

The interrupted TX promotion state has now been resolved. All 20 formal
`scene_static/tx_catalog.json` files use `roadside_proxy_lane_exclusion_v1`, and
all 3000 trajectory-QA-accepted episodes have valid selected-5
`tx_assignment.json`. Formal TX visualization was refreshed at:

```text
datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization/tx_routes_contact_sheet.html
```

Next pre-RF gate: prepare zero-vehicle static RSS cache for the 40 candidates per
scene, then run selected-5 dynamic RF only after user authorization. Do not rerun
CARLA trajectory collection unless explicitly requested.


## MultiScene20 CARLA Runtime Stability Note 2026-05-14

The no-rendering lifecycle crash root cause was addressed in code and validated
by the successful 2-worker recovery collection. The stable trajectory-only setup
is now:

```text
CARLA server: CarlaUE4.sh -RenderOffScreen, not -nullrhi
CARLA world: no_rendering_mode=true for trajectory-only attempts
GPU assignment: 2 workers, --gpu-ids 0,1, ports 2100/2110 and 8100/8110
cleanup: always attempt DestroyActor if the CARLA port is still available
same-town attempts: reuse loaded world; no force_clean_slate reload
sync exit: keep no_rendering_mode=true while leaving synchronous mode
crash diagnostics: per-start DRD_CARLA_START markers and since-last-start counts
```

Latest recovery summary:

```text
status: ok
accepted_total: 3000 / 3000
worker 0 signature_counts_since_last_start: all zero
worker 1 signature_counts_since_last_start: all zero
latest summary: datasets/DynamicRadioMap/MultiScene20/indexes/multi_scene_collection_summary.json
latest log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_latest.log
```

## TX Placement Lane-Exclusion Review Plan 2026-05-13

Lane-exclusion TX placement is implemented as a sidecar review path, not yet
promoted to formal MultiScene20 catalogs. The generated review artifacts are:

```text
datasets/DynamicRadioMap/MultiScene20/indexes/tx_catalog_regeneration_candidate_lane_exclusion_latest.json
datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization_lane_exclusion/tx_routes_contact_sheet.html
datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization_lane_exclusion/*_tx_routes.svg
```

All 20 scenes have `tx_catalog_candidate_lane_exclusion.json` with 40 candidates
and `inside_drivable_lane_accepted = 0`. Human review is still required before
promotion, especially `town05_opt_junction_2086_tx_routes.svg` because it was the
scene with visually bad roadside-proxy candidates.

If the review passes, run promotion only when no CARLA collection is active:

```bash
python3 scripts/drd.py promote-tx-catalogs \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --source candidate_lane_exclusion
```

Promotion backs up formal TX catalog/summary files and rebuilds selected-5
episode TX assignments. Do not run Sionna/RF from old formal catalogs if the
intent is to use these reviewed lane-exclusion TXs.

## MultiScene20 Active CARLA Collection 2026-05-12 CST

The active MultiScene20 pre-Sionna collection was repaired and resumed. Three
unfinished scenes with >=500 attempts and <5% acceptance were quarantined and
replaced by validated simple-junction candidates from the discovery catalog:

```text
town02_opt_junction_0076 -> town01_opt_junction_0143
town05_opt_junction_0979 -> town05_opt_junction_0359
town04_opt_junction_0916 -> town04_opt_junction_1197
```

The new active config still has 20 scenes, and
`datasets/DynamicRadioMap/MultiScene20/scenes/` has been cleaned so it contains
exactly those active scene directories. CARLA-only collection is running with 4
workers, GPU round-robin `0,1,0,1`, no-rendering mode, high restart budget, and
static unfinished-first round-robin scheduling:

```bash
python3 scripts/drd.py collect-multi-scene \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --carla-workers 4 \
  --gpu-ids 0,1 \
  --rpc-ports 2100,2110,2120,2130 \
  --tm-ports 8100,8110,8120,8130 \
  --target-accepted-per-scene 150 \
  --max-carla-restarts-per-scene 1000 \
  --no-rendering-mode
```

Do not start a second collection process. Monitor
`datasets/DynamicRadioMap/MultiScene20/indexes/current_live_status_latest.json`
and per-worker logs under `supervisor_logs/carla_parallel/`. Continue monitoring
acceptance rates; do not run Sionna RT/RF until trajectory collection reaches
target and the user explicitly authorizes RF. After stopping/completion, run a
final metadata repair pass:

```bash
python3 scripts/drd.py repair-multi-scene-metadata --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
```



## Code Cleanup Update 2026-05-12 CST

Removed unused delegation-only CARLA shims and unused standalone diagnostic /
compatibility modules from `scripts/dynamic_radio_dataset/`. While MultiScene20
collection is running, avoid editing active runtime modules loaded by new CARLA
subprocess attempts (`carla/collect.py`, `carla/runner.py`, `qa/trajectory_qa.py`,
`plans/*`, `multi_scene/*`) unless a live failure requires it.

## Milestone

Current milestone is to finish the formal single-scene Town10 release package:

```text
config: configs/dynamic_radio/dynamic_radiomap_town10_300.yaml
root: datasets/DynamicRadioMap/Town10
target: 300 trajectory-QA-accepted episodes / 900 episode-TX rows
release package after success-only pruning: episodes/, reference_scene/, scene_static/
```

Collection has reached the 300 accepted trajectory target. RF is incomplete:
262 episodes have RSS outputs and 38 episodes failed in the GPU RF stage. The
next gate is RF diagnosis/resume, then finalize, verify-release, and prune only
after verification passes.

## Near-Term Plan

1. Generate a read-only failure report for `datasets/DynamicRadioMap/Town10`.
2. Inspect representative failed RF episodes and reproduce one failed
   `process-episode` command under the Sionna env if needed.
3. Fix only the root cause if it is code/runtime related; do not lower QA
   thresholds, reshape bad arrays, skip TXs, or fake RSS.
4. Resume `process-rf` with the formal config. Completeness checks should skip
   the 262 finished episodes and process only incomplete accepted episodes.
5. Run `finalize`, `verify-release --expected-episodes 300`, and only then
   `prune-release`.

## Refactor Direction

The current structural cleanup is intentionally narrow:

- Keep formal CLI behavior, artifact schema, QA policy, and RF numeric path
  stable.
- Keep orchestration small; do not introduce a new large orchestrator.
- Keep tests and diagnostics outside the formal generation path.
- Move reusable runtime, RF scheduling, per-episode RF jobs, and diagnostics
  into clear packages.

Completed split:

- `geometry/regions.py`, `geometry/collision.py`: region math, distance,
  rectangle, building, and SAT collision helpers.
- `raster/traffic_grid.py`: building/vehicle rasterization and traffic-grid
  assembly.
- `qa/metrics.py`: pure trajectory/RSS metric helpers.
- `qa/rf_policy.py`: RF scene/per-TX QA decisions and config dataclasses.
- `rf/rss_compute.py`: formal Sionna RSS numeric generation.
- `rf/processing.py`: formal process-rf API, trajectory-QA gate, completeness
  checks, GPU worker slots.
- `rf/runtime.py`: Sionna Python/env/cache setup shared by orchestration and
  manual episode jobs.
- `rf/episode_job.py`: single-episode export/RSS/QA artifact generation using
  `rf/rss_compute.py`.
- `rf/artifacts.py`, `rf/scene_static.py`: RF artifact and scene-static helper
  APIs.
- `indexing/finalize.py`, `indexing/episode_index.py`: finalize-index and
  deterministic split/index row helpers.
- `diagnostics/dataset_scan.py`, `diagnostics/histograms.py`,
  `diagnostics/failure_report.py`: read-only dataset scanning and failure
  summaries.
- `testing/harness.py`: scratch RF assertions.

Still worth doing later, after release recovery:

- Keep `carla/runner.py` as the active collection orchestrator until a real
  extraction is needed; do not keep unused delegation-only shim files.
- Trim `render/rss_video.py` further so it only renders cached RSS/review
  artifacts. Formal generation already uses `rf/rss_compute.py`.

Completed validation for this refactor:

```text
config: configs/dynamic_radio/town10_junction_10s_4to7_refactor_2ep_gpu.yaml
root: datasets/refactor_2ep_gpu_20260503
accepted episodes: 2
episode-TX rows after finalize: 6
RSS shape: (3, 100, 128, 128)
GPU coverage: GPU 0 = 1 episode, GPU 1 = 1 episode
verify-release --expected-episodes 2: passed
```

## Constraints

- Do not change TrafficPlan schema, selection manifest semantics, bucket/matrix
  collection behavior, episode index schema, split policy, or release package
  layout in this refactor.
- Do not restore random collection as the formal path.
- Do not add `test_mode`, `debug_mode`, fake RSS, or silent fallback to formal
  generation.
- Trajectory-QA-failed episodes must not enter RF.
- Formal train/main remains all-TX.

## Validation Gates

Static gates after code/doc changes:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

Release gates after RF recovery:

```bash
python3 scripts/drd.py process-rf --config configs/dynamic_radio/dynamic_radiomap_town10_300.yaml --use-gpu --gpu-ids 0,1 --rf-workers 2
python3 scripts/drd.py finalize --config configs/dynamic_radio/dynamic_radiomap_town10_300.yaml
python3 scripts/drd.py verify-release --config configs/dynamic_radio/dynamic_radiomap_town10_300.yaml --expected-episodes 300
```



## MultiScene20 CARLA Stability Plan Update 2026-05-12 CST

Implemented direction for the next collection run: keep high CARLA restart
budget, add restart backoff, use trajectory-only CARLA `no_rendering_mode` by
default, and distribute 4 workers across GPU0/GPU1 with `--gpu-ids 0,1`. No hard
circuit breaker is desired; crashes should be summarized and slowed down, not
used to stop collection early.

Recommended next launch after metadata/scene decisions:

```bash
python3 scripts/drd.py collect-multi-scene \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --carla-workers 4 \
  --gpu-ids 0,1 \
  --rpc-ports 2100,2110,2120,2130 \
  --tm-ports 8100,8110,8120,8130 \
  --target-accepted-per-scene 150 \
  --max-carla-restarts-per-scene 1000 \
  --no-rendering-mode
```

Update 2026-05-14: `-nullrhi` was tested and is not usable in this packaged
CARLA build. The validated no-rendering path uses `-RenderOffScreen` for the
server, CARLA world `no_rendering_mode=true` for trajectory-only attempts,
explicit actor cleanup, same-town world reuse, and no-rendering-preserving sync
exit. The 2-worker recovery run reached 3000/3000 accepted trajectories.

## Town10 Static-Prior Correction Plan 2026-05-13 CST

The current `datasets/DynamicRadioMapRelease/Town10_v1` static prior should not
be treated as a clean no-vehicle baseline. Its runtime static cache metadata
lists active reference vehicles, so the next RF/release maintenance step should
regenerate static RSS cache with zero active vehicles, rerun affected
`rss_delta_from_static_db` artifacts, and rebuild the release before using
scene-level `tx_static_radio_dbm` for training. The code now guards future static
cache generation and release packaging against this failure mode.

## MultiScene20 Runtime Replacement Update 2026-05-10 CST

The active MultiScene20 collection now uses runtime replacement for clearly
pathological scenes. Four unfinished scenes with >=500 attempts and <5% observed
acceptance rate were removed from the active config and quarantined, then replaced
with validated candidates from the discovery catalog only:

```text
town01_opt_junction_0110 -> town05_opt_junction_0207
town05_opt_junction_0053 -> town05_opt_junction_0979
town04_opt_junction_0483 -> town04_opt_junction_0053
town05_opt_junction_1882 -> town10_junction_0664
```

All four replacements have reference exports, route libraries, 40-TX catalogs,
and 1200 accepted plans. CARLA-only collection has resumed with 4 workers on
GPU1 and a 1000 restart budget. Continue monitoring per-scene acceptance rates;
if another unfinished scene crosses the same pathological threshold, prefer a
candidate-id-only validated replacement over changing router, control, or QA
logic. Do not run RF/Sionna until CARLA collection is complete and the user
authorizes it.

## Multi-Scene Dataset Expansion Plan 2026-05-06 CST

A new multi-scene milestone is now scaffolded on top of the single-scene pipeline:

```text
scene discovery -> preview/contact sheet -> selected_scene_manifest validation
-> per-scene reference/scene_static/route_library/plan_catalog
-> RF static cache for 20 TX candidates
-> selected-5 TX assignment per accepted episode
-> selected-TX RF -> per-scene finalize -> global index merge
```

Near-term next steps:

1. The first CARLA-only multi-scene run was stopped at user request after
   producing 320 accepted trajectories. Before resuming, use the fixed
   `collect-multi-scene` code path; it no longer inherits the single-scene
   Town10 300-episode `collection.bucket_targets` and now targets 150 per
   scene as intended.
2. The two previously selected corridor scenes with empty accepted plan catalogs
   have been removed from the active 20-scene resolved config and their generated
   artifacts were deleted. The active replacement scene ids are now
   `town05_opt_junction_1722` and `town04_opt_junction_0483`. Both replacement
   scenes have been prepared and now have reference exports, 20 TX candidates,
   and 1200 accepted traffic plans.
3. If CARLA becomes unavailable during collection, use the multi-scene worker
   restart/summary outputs rather than continuing raw CARLA subprocesses. The
   restart path now terminates stale owned CARLA/listener processes, waits for
   RPC/RPC+1/RPC+2/TM ports to release, and keeps retrying the same scene until
   `--max-carla-restarts-per-scene` is exhausted. Initial 4-worker CARLA
   launches are now staggered with `DRD_CARLA_WORKER_START_STAGGER_S`; the
   current rerun uses 30s staggering.
4. Run `prepare-rf-cache` for validated scenes before selected-TX RF, but only
   after the user authorizes Sionna RT/static RSS.
5. Run selected-TX RF/finalize only after CARLA collection reaches the desired
   accepted trajectory count and the user authorizes Sionna RT.

Constraints for this milestone:

- Visual selection is only a diversity aid; program validation decides runnability.
- Do not introduce automatic scene classification, parallel scheduling, traffic-control changes, QA relaxation, or Sionna numeric changes.
- Scene-level CARLA parallelism is allowed for pre-Sionna collection; do not
  add cross-scene load balancing or router/control algorithm changes in this
  milestone.
- Static RSS for 20 TX candidates belongs to RF cache preparation, not scene preparation.
- Old single-scene all-TX configs must continue to work without `tx_assignment.json`.

## MultiScene20 CARLA Runtime Stabilization Update 2026-05-13 CST

Current active run is 2 workers, one per GPU, not 4 workers. The launcher remains
alive and uses the high restart budget, but CARLA server startup is now gated by
GPU health to avoid repeated UE4 `Signal 11` crashes while both GPUs are already
fully loaded by other jobs.

Current launcher command shape remains:

```bash
python3 scripts/drd.py collect-multi-scene \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --carla-workers 2 \
  --gpu-ids 0,1 \
  --rpc-ports 2100,2110 \
  --tm-ports 8100,8110 \
  --target-accepted-per-scene 150 \
  --max-carla-restarts-per-scene 1000 \
  --no-rendering-mode
```

If GPUs stay above the start threshold, collection will wait/back off instead of
starting CARLA and crashing. When GPU pressure drops, workers should start CARLA
again automatically. Continue to prefer scene replacement over router/control/QA
changes if `town04_opt_junction_1197` or `town04_opt_junction_0785` falls below
the pathological-scene threshold.
