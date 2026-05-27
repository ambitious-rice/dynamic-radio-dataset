# Dynamic Radio Dataset Pipeline
Last updated: 2026-05-19 CST

## Sionna RT Compatibility Layer 2026-05-26

The formal RSS computation path now uses
`scripts/dynamic_radio_dataset/sionna/rt_compat.py` to support both the legacy
Sionna 0.19 API and the current Sionna RT 2.x API.

Affected fresh-Sionna callers:

```text
scripts/dynamic_radio_dataset/rf/rss_compute.py
scripts/dynamic_radio_dataset/render/rss_video.py
```

Compatibility behavior:

```text
Sionna 0.19:
  scene.coverage_map(...)

Sionna RT 2.x:
  RadioMapSolver()(scene, center=..., orientation=..., size=...,
                   cell_size=..., samples_per_tx=...)
```

The output remains `rss_dbm` with shape `[T, resolution, resolution]` and the
same metadata/NPZ layout. Metadata now records:

```text
propagation.sionna_backend.package_version
propagation.sionna_backend.mitsuba_variant
propagation.sionna_backend.radio_map_api
```

Important Sionna 2.x loading detail: `load_scene()` defaults to merging shapes,
which collapses exported object names such as `static_buildings_proxy` and
`car_59`. The compatibility loader calls `load_scene(..., merge_shapes=False)`
when supported so the existing CARLA/Sionna export object names remain usable for
motion updates and vehicle inclusion/exclusion.

Verified environments:

```text
legacy:
  /share1/fzj/miniconda3/envs/sionna019/bin/python
  sionna 0.19.0, mitsuba 3.5.2, drjit 0.4.6
  API = scene.coverage_map

latest smoke:
  /tmp/sionna2test/bin/python
  sionna-rt 2.0.1, mitsuba 3.8.0, drjit 1.3.1
  API = RadioMapSolver
```

The `/tmp/sionna2test` environment is a working smoke-test environment, not a
durable project dependency. For production use, create a durable equivalent
outside `/tmp` and point RF subprocesses to that Python interpreter. The RF
runtime supports an environment override:

```bash
DRD_SIONNA_PYTHON=/path/to/new/sionna-rt/bin/python \
  python3 scripts/drd.py process-multi-scene-rf ...
```

If `DRD_SIONNA_PYTHON` is unset, existing configs/defaults still use the legacy
`/share1/fzj/miniconda3/envs/sionna019/bin/python` path.

## MultiScene20 Review Visualization Contract 2026-05-21

Canonical review/sample videos must use the faithful green absolute cached-RSS
style in `scripts/dynamic_radio_dataset/render/sample.py` via `render-sample` or
an equivalent wrapper. The style is intentionally fixed:

```text
display_mode = absolute
cmap = viridis
plot_style = clean
rx_region = valid_crop
view_region = valid
vehicle_overlay = dark
vmin_dbm = -108
vmax_dbm = -42
visual_fill = false
visual_smooth_sigma = 0.0
interpolation = nearest
tx_marker_visible = false
```

Important correction 2026-05-26: cached 128x128 RSS visualization must not
nearest-fill no-hit/low-RSS cells or smooth/interpolate the dBm field, because
that display path can create visible same-radius bright/dark artifacts that are
not present in fresh higher-quality Sionna RT visualizations. The current
canonical settings are the faithful values above.

This is the preferred human-review look for sampled RSS videos. Do not treat
legacy filled/smoothed clean-style output as canonical for MultiScene20. When
adding new visualization outputs for review, keep this faithful green style
consistent unless a doc explicitly says the goal is non-faithful smoothing/debug
output.

## Fusorosa Sealed-Underbody RF Export Contract 2026-05-19

For the formal MultiScene20 dynamic RF run, the Sionna exporter now treats
`vehicle.mitsubishi.fusorosa` as the sealed-underbody large vehicle. This is an
export-time mesh change only:

```text
CARLA source asset/blueprint: unchanged
CARLA runtime physics and trajectories: unchanged
Sionna object name and mesh file naming: unchanged
mesh file affected: the existing car_<actor_id>.ply for each fusorosa actor
added geometry: one oriented rectangular underbody slab using the actor bbox XY
                footprint, height 0.55 m, margin 0.0 m
manifest fields: vehicle.underbody_seal,
                 asset_pipeline.sealed_underbody_*,
                 large_vehicle_underbody_seal_count
```

This replaces prior unsealed fusorosa RF semantics. After the intervention, RF
outputs from accepted episodes containing fusorosa were removed and must be
recomputed; accepted episodes without fusorosa remain valid and their completed
RF outputs were preserved. Static RF cache, trajectories, trajectory QA, route
plans, and TX assignments are not invalidated by this change.

Resume command in use:

```bash
python3 scripts/drd.py process-multi-scene-rf \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --use-gpu --gpu-ids 0 --rf-workers 1
```

Do not add separate CLI/config knobs for the fusorosa seal unless the dataset
needs multiple large-vehicle geometry policies later.

## MultiScene20 Quality-Filtered Static RF State 2026-05-15

The active formal `scene_static/tx_catalog.json` files currently match the
reviewed `candidate_quality_filter` TX catalogs. Static RF cache has been
computed and validated for these formal catalogs:

```text
expected_static_tx = 800
complete_static_tx = 800
bad_count = 0
validation: datasets/DynamicRadioMap/MultiScene20/indexes/static_rf_quality_filter_validation_latest.json
```

The validation includes `.npy` shape `(128,128)`, zero active vehicle objects, and
TX-position metadata matching the active formal catalog. Because the formal copy
was performed while replacement collection was active, accepted episode
`tx_assignment.json` files still need to be deleted/rebuilt by
`promote-tx-catalogs --source candidate_quality_filter` after collection finishes
and before dynamic RF.

## MultiScene20 TX Quality-Filter Sidecar Contract 2026-05-15

The `candidate_quality_filter` TX sidecar is a human-review candidate catalog
that preserves current TX IDs for reasonable placements and replaces only TXs
failing the stricter quality policy. Criteria used for the 2026-05-15 sidecar:

```text
max_distance_to_scene_center_m = 44.0
max_distance_to_route_centerline_m = 8.0
min_distance_to_route_centerline_m = 2.0
minimum clearance to saved CARLA drivable-lane exclusion = 0.5 m
inside valid_crop and outside building proxies
initial replacement pairwise spacing = 5.0 m, relaxed as needed to keep 40 TXs
```

The sidecar files are:

```text
scene_static/tx_catalog_candidate_quality_filter.json
scene_static/tx_placement_summary_candidate_quality_filter.json
```

Promotion uses the existing reviewed-sidecar path:

```bash
python3 scripts/drd.py promote-tx-catalogs \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --source candidate_quality_filter
```

Promotion should be done only after CARLA replacement collection is finished and
the visualization has been reviewed, because promotion deletes/rebuilds accepted
episode `tx_assignment.json` files.

## MultiScene20 No-Building Sionna Export Replacement Update 2026-05-15

If an active scene's forced Sionna reference export has
`static_geometry.building_count=0`, no `building_mesh`, and no
`meshes/static_buildings_proxy.ply`, the scene is considered invalid for the
formal RF dataset and must be replaced rather than processed with missing
buildings. The RF pipeline must not use `--allow-missing-buildings` for the
formal MultiScene20 run.

The 2026-05-15 verification found these old scenes invalid after re-export:

```text
town04_opt_junction_1368
town04_opt_junction_1061
town04_opt_junction_1176
```

The active resolved config replaces them with prepared Town04 scenes that have
nonzero exported building proxies:

```text
town04_opt_junction_1249 building_count=27
town04_opt_junction_0255 building_count=17
town04_opt_junction_0850 building_count=13
```

Only these replacement scenes need CARLA trajectory recollection. Existing good
active scenes and their validated static RF cache should be reused/skipped by
resume validation; old no-building scenes remain as provenance artifacts but are
not part of the active resolved config.

## MultiScene20 RF Cache / RF Resume Contract 2026-05-14

For MultiScene20 RF generation, the intended RF-only path after CARLA collection
and TX promotion is:

```bash
python3 scripts/drd.py prepare-rf-cache \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
python3 scripts/drd.py process-multi-scene-rf \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --use-gpu --gpu-ids <single_gpu_id> --rf-workers 1
python3 scripts/drd.py finalize-multi-scene \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
```

`prepare-rf-cache` computes all 40 candidate-TX static RSS maps per scene with
`--allow-zero-vehicles` and `--exclude-vehicles all`. A cached static TX is now
skipped only if `static_rss_dbm.npy` is readable with shape `(128, 128)` and its
metadata validates as zero-vehicle (`active_vehicle_objects=[]`). Static `.npy`
files are written atomically so an interruption should not leave a truncated file
that is later mistaken as complete.

`process-multi-scene-rf` is an RF-only multi-scene wrapper; it does not rerun
CARLA collection. It delegates to the existing per-scene `process_rf`, so dynamic
resume still uses `rf_episode_complete()` and skips only complete selected-5 RF
episodes. Per-episode `sionna_export` directories are reused only after checking
that `manifest.json`, `scene.xml`, and non-empty `motion.jsonl` are present and
readable; incomplete export leftovers are deleted and rebuilt.

To reserve one GPU for others, launch the RF pipeline with one visible GPU, e.g.
`CUDA_VISIBLE_DEVICES=1` and `--gpu-ids 1 --rf-workers 1`. Do not export global
`PYTHONNOUSERSITE=1` for the system `python3` orchestration process on this host;
Sionna subprocesses set it via `rf.runtime`.



## MultiScene20 TX Lane-Exclusion Sidecar Contract 2026-05-13

TX placement now supports an optional reviewed sidecar method:

```text
roadside_proxy_lane_exclusion_v1
```

This method keeps the existing route-centerline roadside proxy but additionally
builds a CARLA/OpenDRIVE drivable-lane exclusion sidecar from driving-lane
waypoints near each scene. Each lane waypoint is approximated as an inflated
oriented rectangle using `lane_sample_step_m` and
`lane_inflation_margin_m`. The implementation is scoped under
`scripts/dynamic_radio_dataset/tx/` and does not change routes, traffic plans,
CARLA collection, trajectory QA, Sionna, or RF numeric behavior.

Optional config fields:

```yaml
tx:
  placement:
    method: roadside_proxy_lane_exclusion_v1
    lane_exclusion:
      enabled: true
      lane_sample_step_m: 2.0
      lane_inflation_margin_m: 1.5
```

Review-mode regeneration is sidecar-only:

```bash
python3 scripts/drd.py regenerate-tx-catalogs \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --placement-method roadside_proxy_lane_exclusion_v1 \
  --sidecar
```

Sidecar outputs per scene:

```text
scene_static/tx_catalog_candidate_lane_exclusion.json
scene_static/tx_placement_summary_candidate_lane_exclusion.json
scene_static/tx_drivable_exclusion.json
```

Review visualization outputs:

```text
datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization_lane_exclusion/
  tx_routes_contact_sheet.html
  <scene_id>_tx_routes.svg
  tx_visualization_summary.json
```

Sidecar regeneration does not overwrite formal `scene_static/tx_catalog.json`
and does not delete or rebuild any episode `tx_assignment.json`. If CARLA lane
geometry cannot be generated, the placement falls back to `roadside_proxy` and
records an explicit warning in `tx_placement_summary_*`; fallback is not silent.

Promotion is a separate human-gated operation:

```bash
python3 scripts/drd.py promote-tx-catalogs \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --source candidate_lane_exclusion
```

Promotion backs up the old formal catalog/summary, copies the reviewed candidate
catalog/summary to formal `tx_catalog.json` / `tx_placement_summary.json`, deletes
accepted-episode `tx_assignment.json`, then reruns the selected-5 TX assignment
policy. Promotion refuses to run while an active `collect-multi-scene` process is
detected unless explicitly overridden; do not override during live collection.

Current promoted result (2026-05-14): all 20 MultiScene20 formal
`scene_static/tx_catalog.json` files now use `roadside_proxy_lane_exclusion_v1`,
all scenes still have 40 candidates, every formal summary reports
`inside_drivable_lane_accepted = 0`, and all 3000 trajectory-QA-accepted episodes
have valid selected-5 `tx_assignment.json`. Static RSS/RF has not been run yet.

## MultiScene20 Metadata Repair / Replacement Contract Update

As of 2026-05-12, interrupted MultiScene20 CARLA collection can be repaired with:

```bash
python3 scripts/drd.py repair-multi-scene-metadata --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
```

The repair command is pre-Sionna only. It reconstructs missing/unreadable legacy
`validation_report.json` from recorded `actor_states.jsonl`, `scene_meta.json`,
`routes.json`, and `trajectory_qa.json`, and fills missing `tx_assignment.json`
using the configured balanced selected-TX sampler. It does not rerun CARLA, does
not relax trajectory QA, and does not create RF/RSS outputs.

Runtime replacement remains candidate-id-only. The 2026-05-12 intervention
quarantined three newly pathological unfinished scenes and replaced them with
validated simple-junction candidates:

```text
town02_opt_junction_0076 -> town01_opt_junction_0143
town05_opt_junction_0979 -> town05_opt_junction_0359
town04_opt_junction_0916 -> town04_opt_junction_1197
```

`collect-multi-scene` now uses a static unfinished-first, round-robin scene order
so fixed workers are not wasted on already-complete scenes. This is scheduling
only; it does not change traffic plans, route generation, QA, TX assignment, or RF
semantics.



## MultiScene20 Current TX/Collection Contract

For `configs/dynamic_radio/multi_scene_20x150_resolved.yaml` and
`configs/dynamic_radio/multi_scene_20x150.yaml`, the current MultiScene20
pre-Sionna contract is:

```text
TX candidates per scene: 40
selected TX per episode for RF: 5
TX placement method: roadside_proxy_lane_exclusion_v1
TX placement max distance to road: 10.0 m
TX placement lane exclusion: CARLA/OpenDRIVE driving-lane rectangles, sample 2.0 m, inflation 1.5 m
TX placement max distance to scene center: 48.0 m
CARLA collection target: 150 trajectory-QA-accepted episodes per scene
CARLA collection workers: 4 when launched for the current pilot run
collect-multi-scene default CARLA restart budget: 120 per scene
current high-budget pilot launch: 1000 CARLA restarts per scene
```

Changing TX candidate count does not require recollecting CARLA trajectories,
but it does require regenerating each scene `scene_static/tx_catalog.json` and
rebuilding every accepted episode `tx_assignment.json` before RF. Static RSS for
all 40 candidates still belongs to RF cache preparation, not scene preparation.
Each dynamic RF episode should process only the selected 5 TX from
`tx_assignment.json`.



## MultiScene20 CARLA Stability / No-Rendering Contract

As of 2026-05-14, multi-scene CARLA-only trajectory collection uses the
following validated no-rendering lifecycle contract without changing traffic, QA,
route, TX, Sionna, or RF semantics:

```text
collect-multi-scene default no_rendering_mode: true
collect-multi-scene no-rendering server launch: CarlaUE4.sh -RenderOffScreen
-nullrhi: disabled by default; this packaged CARLA build segfaults with NullRHI
CARLA world setting during attempts: no_rendering_mode=true
RGB/topdown preview workflows: rendering remains enabled via --rendering-mode
recommended CARLA workers after validation: 2 total across two GPUs
recommended GPU assignment: --gpu-ids 0,1, one worker per GPU
recommended high restart budget: --max-carla-restarts-per-scene 1000
restart backoff: 30s, 60s, 120s, 300s capped
CARLA GPU utilization gate: skipped by default for no-rendering collection
CARLA child process X11/VSCode display env cleanup: enabled by default
CARLA child process GPU visibility env cleanup: enabled by default
CARLA child process Vulkan ICD: NVIDIA ICD when available
CARLA child process core dumps: disabled by default
```

Attempt lifecycle rules for no-rendering/no-video trajectory collection:

```text
same requested town already loaded -> reuse world; do not force reload for clean slate
town changed -> client.load_world(requested_town) only for the actual town change
attempt start -> clear_existing_vehicles / clear_existing_sensors handles residue
attempt cleanup -> if CARLA port accepts, always attempt DestroyActor cleanup
attempt cleanup -> if CARLA port is unavailable, write cleanup_skipped_server_unavailable and let supervisor restart
cleanup stage names -> cleanup_completed or cleanup_skipped_server_unavailable; no cleanup_skipped_no_rendering_mode
CarlaSyncContext exit in no-rendering -> synchronous_mode=false, fixed_delta_seconds=None, no_rendering_mode=true
```

Worker CARLA stdout/stderr logs include `DRD_CARLA_START` markers at every server
launch, and crash summaries report both cumulative signatures and
`signature_counts_since_last_start` to separate old Signal 11 history from current
crashes.

The 2-worker recovery run after this lifecycle fix reached `3000 / 3000` accepted
trajectories with `signature_counts_since_last_start` all zero for both workers.

## MultiScene20 Runtime Scene Replacement Policy

As of 2026-05-10, the active `multi_scene_20x150_resolved.yaml` config has a
manual runtime replacement record for pathological CARLA scenes. The current
intervention policy is:

```text
replacement threshold: unfinished scene, >=500 attempts, observed acceptance rate <5%
completed scenes are not replaced
replacement candidates must come from existing discovery candidate_id values
replacement scenes must pass program validation before entering the active config
no router/control/QA relaxation is used to rescue pathological scenes
```

Removed scenes are preserved under quarantine, not deleted. Active replacements
from runtime interventions are:

```text
town05_opt_junction_0207 replaces town01_opt_junction_0110
town04_opt_junction_0053 replaces town04_opt_junction_0483
town10_junction_0664     replaces town05_opt_junction_1882
town01_opt_junction_0143 replaces town02_opt_junction_0076
town05_opt_junction_0359 replaces town05_opt_junction_0979
town04_opt_junction_1197 replaces town04_opt_junction_0916
```

Replacement / repair reports:

```text
datasets/DynamicRadioMap/MultiScene20/indexes/high_failure_scene_replacement_report.json
datasets/DynamicRadioMap/MultiScene20/indexes/low_success_scene_replacement_report.json
datasets/DynamicRadioMap/MultiScene20/indexes/metadata_repair_latest.json
```

This is a CARLA collection/config decision only. It does not change Sionna RT,
RF numeric algorithms, trajectory QA semantics, TX assignment shape, or the
selected-5 dynamic RF contract.

## Baseline Contract

The active project is a plan-first CARLA + Sionna RT dynamic radio-map dataset
pipeline. Active code lives under `scripts/dynamic_radio_dataset/`; use
`python3 scripts/drd.py` as the primary CLI.

Current formal release baseline:

```text
config: configs/dynamic_radio/dynamic_radiomap_town10_300.yaml
root: datasets/DynamicRadioMap/Town10
town: Town10HD_Opt
scene id: town10_junction_0189
TX catalog: three fixed roadside TXs from scene_static/tx_catalog.json
episode length: 10s at 10fps
label frames: 100
RSS grid: 128 x 128
dynamic RSS shape: (3, 100, 128, 128)
vehicle matrix: requested-total 4/5/6/7 with target-large 0/1/2/3 quotas
RF policy: all TXs for every trajectory-QA-accepted episode
target: 300 accepted episodes / 900 episode-TX rows
```

This is a dataset config, not a structural assumption. Keep `scene_id`,
per-scene `tx_catalog`, route-library metadata, and scene-aware index rows.

## Formal Pipeline

```text
prepare-scene
  -> build-route-library
  -> generate-plans
  -> optional deterministic validation/release selection manifest
  -> preflight validation
  -> collect CARLA attempts from TrafficPlan
  -> trajectory QA
  -> process-rf through Sionna/RSS
  -> per-TX QA
  -> finalize index/splits
  -> verify-release
  -> prune-release only after verification succeeds
```

Formal RF processing must run only after trajectory QA passes.
`target_tx_first` is smoke/debug only; formal train/main is all-TX.

Useful commands:

```bash
python3 scripts/drd.py prepare-scene --config <config>
python3 scripts/drd.py build-route-library --config <config>
python3 scripts/drd.py generate-plans --config <config>
python3 scripts/drd.py select-validation-plans --config <config>
python3 scripts/drd.py collect --config <config> --target-accepted <N>
python3 scripts/drd.py process-rf --config <config>
python3 scripts/drd.py finalize --config <config>
python3 scripts/drd.py failure-report --config <config>
python3 scripts/drd.py verify-release --config <config> --expected-episodes <N>
python3 scripts/drd.py prune-release --config <config> --expected-episodes <N>
```

## Core Modules

- `configs.py`, `paths.py`: config loading and path resolution.
- `geometry/regions.py`, `geometry/collision.py`: pure region, distance,
  rectangle, building, and collision geometry.
- `raster/traffic_grid.py`: traffic-grid rasterization from Sionna motion.
- `routes/route_library.py`: route feature cache and route diagnostics.
- `plans/schemas.py`: TrafficPlan schema.
- `plans/sampler.py`, `plans/validator.py`, `plans/catalog.py`: plan bank
  sampling, preflight validation, and catalog IO.
- `plans/pilot_selector.py`, `plans/validation_selector.py`: diagnostic or
  deterministic selection helpers.
- `carla/collect.py`: CARLA clip collection CLI.
- `carla/runner.py`: plan-catalog attempt orchestration and resume behavior.
- `qa/trajectory_qa.py`: pre-Sionna trajectory QA.
- `qa/metrics.py`: pure trajectory/RSS metric calculations.
- `qa/rf_policy.py`: RF scene/per-TX QA policy and thresholds.
- `pipeline/stages.py`: prepare/finalize/release stage orchestration.
- `pipeline/process_rf.py`: compatibility CLI for scene prep, per-episode RF,
  and finalize-index.
- `rf/processing.py`: formal RF scheduling, trajectory-QA gate, and RF
  completeness checks.
- `rf/runtime.py`: Sionna Python/env/cache setup.
- `rf/rss_compute.py`: formal Sionna RSS numeric generation; writes
  `rss_maps.npz`, `frame_stats.jsonl`, and numeric RF metadata in
  `rss_heatmap_meta.json` only. It must not carry visualization-only CLI or
  config fields.
- `rf/artifacts.py`, `rf/scene_static.py`: RF artifact IO and scene-static/TX
  search helpers.
- `rf/episode_job.py`: single-episode export/RSS/traffic-grid/QA artifact job.
- `indexing/finalize.py`, `indexing/episode_index.py`: finalize index/splits
  and episode-TX index row helpers.
- `pipeline/reports.py`: timing report and full-run extrapolation.
- `pipeline/supervisor.py`: unattended run supervisor.
- `sionna/export.py`: Sionna/Mitsuba export.
- `render/rss_video.py`, `render/sample.py`, `render/review_pack.py`: cached
  RSS rendering and diagnostics. Fresh RSS compute in `render/rss_video.py` is
  compatibility-only and deprecated for formal RF.
- `diagnostics/dataset_scan.py`, `diagnostics/histograms.py`,
  `diagnostics/failure_report.py`: read-only failure analysis.
- `checks/check_contract.py`: engineering and data-contract checks.


### Cleanup Note 2026-05-12

Unused delegation-only CARLA boundary shims, unused QA/RSS diagnostic helpers,
the legacy compatibility re-export module, and the standalone dynamic-scene
renderer were removed from the active package. `carla/runner.py`,
`qa/trajectory_qa.py`, `qa/metrics.py`, `qa/rf_policy.py`, `rf/rss_compute.py`,
and the formal CLI paths remain the supported implementation.

## TrafficPlan Contract

`TrafficPlan.vehicle_count` is the requested total actual vehicle target.

```text
vehicles: route-controlled primary/auxiliary vehicles
background_tm: requested Traffic Manager ambient vehicles
```

Role semantics:

- `primary_controlled`: required route-controlled RF vehicles.
- `auxiliary_controlled`: optional route-controlled helper vehicles.
- `background_tm`: ambient Traffic Manager vehicles.

TrafficPlans may include `bucket` metadata:

```json
{
  "vehicle_count": 7,
  "target_large_vehicle_count": 3,
  "bucket_order": 3,
  "bucket_key": "vehicle_count_7"
}
```

Required controlled vehicles must spawn and match actor tracks. Optional
auxiliary and background TM shortfalls are recorded as metadata, not automatic
hard failures. Every spawned background vehicle must appear in
`frames/actor_states.jsonl` and flow through Sionna export and traffic-grid
rasterization. Quota-controlled plans use exact ordered
`background_tm.vehicle_types` lists.

## Accepted Episode Products

Accepted, RF-processed episodes contain:

```text
episode_meta.json
qa_report.json
rf_process_meta.json
trajectory_qa.json
scene_meta.json
validation_report.json
frames/actor_states.jsonl
sionna_export/
tx_*/rss_maps.npz
traffic_grid_uint8.npz
rss_dynamic_dbm.npz
rss_delta_from_static_db.npz
```

`rss_dynamic_dbm.npz` stores key `dynamic_rss_dbm` with shape
`(num_tx, num_label_frames, 128, 128)`. For the current release config this is
`(3, 100, 128, 128)`.

Per-TX `rss_heatmap_meta.json` is retained as the existing metadata filename,
but formal `rf/rss_compute.py` metadata is numeric RF metadata only. Cached
RSS visualization metadata may still include visualization fields when written
by `render/rss_video.py`; formal RF generation must not depend on those fields.

Role-aware counts should be preserved where available in `scene_meta.json`,
`trajectory_qa.json`, `qa_report.json`, `episode_meta.json`, and summaries:

```json
{
  "planned_required_controlled_count": 2,
  "planned_optional_controlled_count": 1,
  "requested_background_count": 3,
  "actual_required_controlled_count": 2,
  "actual_optional_controlled_count": 1,
  "actual_background_count": 3,
  "actual_total_vehicle_count": 6
}
```

## QA Policy

- Preflight rejects direct plan-realizability failures.
- Collection records subprocess success/failure and logs; it does not run
  Sionna.
- Required controlled spawn failure is a collection failure with explicit
  `failure_code`.
- Trajectory QA hard rejects collisions, frame jumps, incomplete clips,
  incomplete actor tracks, invalid vehicle mix, missing required controlled
  vehicles, missing primary controlled vehicles, and required-primary motion
  failures.
- Lane-adherence near junction turns is a soft warning.
- Core visit, TX corridor hit/crossing, TX clearance, route mix, label
  crossing count, and legacy passed-target count are diagnostics only.
- RF scene QA uses the configured `max_large_vehicle_count`.
- RF/per-TX QA rejects insufficient dynamic RSS change.
- Dynamic RSS metrics use valid non-building common-mask statistics and exclude
  NaN/Inf/floor-clipped cells.
- Only scene-QA and per-TX-QA accepted rows are indexed.

Do not lower QA thresholds, reshape bad arrays, skip failed TXs, or generate
fake RSS to pass validation.

## Failure Discipline

Failures must:

- write explicit `failure_code`;
- preserve stdout/stderr/log paths when available;
- record failed stage metadata;
- stop downstream stages that should not run;
- summarize failure distributions.

`failure-report` is read-only. It scans existing collection, trajectory QA, RF,
per-TX QA, and index artifacts and writes `diagnostics/failure_report.json` by
default. It must not trigger CARLA or Sionna and must not change QA decisions.

## Release Package

`verify-release` checks accepted episode count, episode-TX row count,
per-episode dynamic RSS shape, and configured GPU coverage. `prune-release`
reruns verification and only then removes runtime-only root entries. The final
release package root intentionally keeps only:

```text
episodes/
reference_scene/
scene_static/
```

## Multi-Scene / Selected-TX Extension 2026-05-06 CST

The pipeline now has a modular first-pass multi-scene extension. Single-scene configs remain valid; selected-TX behavior is enabled only when the resolved scene config sets `tx.selected_tx_per_episode` and accepted episodes have `tx_assignment.json`.

New CLI commands:

```bash
python3 scripts/drd.py discover-scenes --config configs/dynamic_radio/multi_scene_discovery_smoke.yaml
python3 scripts/drd.py render-scene-previews --config configs/dynamic_radio/multi_scene_discovery_smoke.yaml
python3 scripts/drd.py validate-selected-scenes --config selected_scene_manifest.yaml
python3 scripts/drd.py prepare-multi-scene --config configs/dynamic_radio/multi_scene_end2end_smoke.yaml
python3 scripts/drd.py prepare-rf-cache --config configs/dynamic_radio/multi_scene_end2end_smoke.yaml
python3 scripts/drd.py collect-multi-scene --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --carla-workers 4 --gpu-id 1
python3 scripts/drd.py run-multi-scene-supervised --config configs/dynamic_radio/multi_scene_end2end_smoke.yaml
python3 scripts/drd.py finalize-multi-scene --config configs/dynamic_radio/multi_scene_end2end_smoke.yaml
```

New artifacts:

```text
discovery/scene_candidates.jsonl
discovery/previews/*.png
discovery/previews/contact_sheet.png
scenes/<scene_id>/configs/resolved_single_scene_config.json
scenes/<scene_id>/configs/collection_runtime_worker_<N>.json
scenes/<scene_id>/scene_static/tx_catalog.json
scenes/<scene_id>/scene_static/tx_placement_summary.json
scenes/<scene_id>/scene_static/rf_static_cache_summary.json
episodes/episode_*/tx_assignment.json
indexes/multi_scene_collection_plan.json
indexes/multi_scene_collection_summary.json
indexes/global_episode_index.jsonl
indexes/global_splits.json
indexes/tx_summary.json
```

TX contract:

- `tx_catalog.json` may contain 20 candidate TXs for a scene.
- `prepare-multi-scene` creates candidate placement metadata only; it does not run static RSS.
- `prepare-rf-cache` computes static RSS cache for all candidates.
- `process-rf` processes only `tx_assignment.json.selected_tx_ids` when present.
- In selected mode, `rss_dynamic_dbm.npz` stores `(selected_tx_per_episode, num_label_frames, H, W)` and includes `tx_candidate_ids`.
- `episode_index.jsonl` and global index rows include `scene_id` and `tx_candidate_id`.

CARLA-only multi-scene collection contract:

- `collect-multi-scene` is a pre-Sionna stage. It runs CARLA trajectory
  collection and `tx_assignment.json` generation only; it must not run Sionna
  RT, static RSS cache, selected-TX RF, or finalize.
- Parallelism is scene-level. Each worker owns one CARLA RPC port and one
  Traffic Manager port, then processes its assigned scenes serially.
- Worker runtime configs may set per-scene jittered seeds to vary traffic while
  preserving the existing plan-catalog vehicle-count/large-vehicle ratios.
- `carla.collect_subprocess_timeout_s` is supported as a hard timeout for a
  single `dynamic_radio_dataset.carla.collect` subprocess. The multi-scene
  collector sets a conservative default of 600s to avoid indefinite hangs.
- Multi-scene scene configs must not inherit single-scene release
  `collection.bucket_targets`, `bucket_order`, or `selection_matrix`.
  `accepted_trajectories_per_scene` / per-scene `target_accepted` is the
  collection target; `check-contract` now verifies this isolation.
- If a CARLA worker server becomes unavailable, the multi-scene collector must
  terminate the stale owned CARLA process and wait briefly for the port to
  release before starting another server on the same RPC port.
- `--gpu-id` sets best-effort graphics/CUDA environment hints for owned CARLA
  processes (`CUDA_VISIBLE_DEVICES`, `NVIDIA_VISIBLE_DEVICES`, PRIME/Vulkan
  hints, and `-graphicsadapter=<id>`). Actual graphics placement is still
  controlled by the host NVIDIA/X/Unreal stack and must be verified with
  `nvidia-smi`.
- `--max-carla-restarts-per-scene` controls how many CARLA crash/restart cycles
  a scene may consume before being marked failed. A `carla_server_unavailable`
  collection result is not terminal by itself; the worker restarts CARLA and
  resumes the same scene until the target is reached or the restart budget is
  exhausted.
- CARLA startup/restart failures (for example UE4 exit code 139/1 before RPC is
  ready) consume the same per-scene restart budget instead of immediately ending
  the scene.
- Before restarting an owned worker server, the collector kills stale CARLA
  processes matching that worker RPC port and waits for RPC, RPC+1, RPC+2, and
  Traffic Manager ports to be clear. This is required because CARLA/UE4 can
  leave wrapper or child processes/ports around after Signal 11 crashes.
- If a stale CARLA child is still listening on those worker ports but no longer
  has the original wrapper command line, the collector may also terminate the
  CARLA listener PID for that dedicated worker port set.

Scene selection contract:

- `discover-scenes` only enumerates candidate descriptors from CARLA/Town.
- Preview PNGs/contact sheets are for human/Codex visual diversity review only.
- Selected manifests must reference existing `candidate_id` values from the candidate catalog; validation rejects invented ids.
- Failed selected scenes are recorded with `failure_code` and can be replaced from backup candidate ids.
- Selected scene preparation now treats a zero-accepted `plan_catalog` as a
  failed scene (`failure_code=plan_catalog_empty`). Reference/scene_static alone
  is not sufficient for collection readiness.
- Multi-scene reference prep treats a reference capture as reusable only when
  both `reference_scene/scene_meta.json` and
  `reference_scene/frames/actor_states.jsonl` exist; partial failed captures are
  rerun instead of being passed to Sionna export.
- Selected-scene validation can be configured with
  `validation_order: grouped_by_town`, `auto_manage_carla: true`, and
  `max_carla_restarts`. In that mode it performs a fast CARLA health check
  before each scene, uses the existing supervisor start/restart helpers, retries
  a candidate once after `carla_unavailable`, and records transient failures in
  `scene_validation_report.json`.

## Scene Signature Compatibility Note (2026-05-06 CST)

RF episode processing still requires a fixed physical scene match before using
prepared `scene_static/` and `reference_scene/` artifacts. The compatibility
check now treats route-discovery inventory fields in `scene_info` as metadata,
not physical-scene identity. It continues to require matching town, scene mode,
support-region center/size/yaw, valid-crop size/yaw, junction id, junction
center, and junction bounding-box extent. It no longer rejects an otherwise
same fixed junction solely because `num_valid_routes`, `num_direction_bins`,
`roads`, `lanes`, or `turn_types` differ between CARLA route discovery runs.

Reason: the remaining formal Town10 RF-incomplete episodes share the same
junction id, crop, center, and bbox as the prepared scene, but have a 16-route
route inventory (`left/right/straight`) instead of the 14-route inventory
(`right/straight`) recorded in the prepared scene signature. Those route
inventory fields affect routing diagnostics, not the static RSS crop/TX/building
contract used for RF generation.

## Standalone Training Release Packager Adapter (2026-05-06 CST)

`scripts/release_packager/package_training_release.py` remains independent from
`drd.py` and `scripts/dynamic_radio_dataset/`. It now has an explicit adapter
for the current Town10 runtime artifact layout:

- Runtime `episode_index.jsonl` and `splits.json` are optional for this
  standalone packager. If absent, it discovers QA-accepted RF-complete episodes
  from episode directories and marks generated release index rows as
  `split: "unsplit"`.
- Current `episodes/*/traffic_grid_uint8.npz` is materialized in the release as
  `episodes/*/dynamic_input.npz`.
- If per-episode `static_input.npz` is absent, release `static_input.npz` is
  generated from `scene_static/building_mask_uint8.npy`,
  `scene_static/loss_mask_uint8.npy`, and `scene_static/tx_catalog.json`.

This is a temporary training-release packaging adapter only. It does not change
CARLA collection, RF/Sionna, QA, finalize, or the runtime dataset writer.

## Local Sionna 0.19 no-RIS coverage-map guard (2026-05-06)

A local runtime hotfix is applied in the Sionna environment used by RF jobs:

```text
/share1/fzj/miniconda3/envs/sionna019/lib/python3.8/site-packages/sionna/rt/solver_cm.py
backup: solver_cm.py.bak_drd_no_ris_guard_20260506
```

Observed failure: `scene.coverage_map()` intermittently entered the RIS
reflection branch although exported DRD scenes have no RIS (`len(scene.ris)==0`),
then attempted `tf.gather(radii_curv, ...)` while `radii_curv is None`, raising:

```text
ValueError: Attempt to convert a value (None) with an unsupported type (<class 'NoneType'>) to a Tensor.
```

The local guard changes the RIS branch condition from:

```python
if tf.shape(ris_reflect_ind)[0] > 0:
```

to:

```python
if ris and tf.shape(ris_reflect_ind)[0] > 0:
```

This is intended to be behavior-preserving for no-RIS DRD scenes: normal LoS and
reflection paths remain enabled, but the RIS-only branch is not entered when RIS
handling is disabled/no RIS exists. This is an environment hotfix, not a dataset
schema change. Recreate it if the `sionna019` conda environment is rebuilt or
Sionna is upgraded, unless the upstream package already contains an equivalent
guard.

## Training Release Scene-Level Static Radio Update (2026-05-06 CST)

Standalone `scripts/release_packager/package_training_release.py` now writes
static TX radio as a scene-level shared artifact instead of per-episode static
inputs. Current release structure:

```text
<release_root>/
  dataset_meta.json
  episode_index.jsonl
  package_report.json
  scenes/<scene_id>/
    scene_meta.json
    tx_catalog.json
    tx_static_radio/
      tx_static_radio_maps.npz
      tx_static_radio_meta.json
    episodes/<episode_id>/
      episode_meta.json
      dynamic_input.npz
      rss_dynamic_dbm.npz
```

Index rows reference `tx_static_radio_path` and `tx_static_radio_meta_path`.
For current Town10 runtime artifacts, `tx_static_radio_maps.npz` is derived from
existing RF outputs (`dynamic_rss_dbm - delta_from_static_db`) and written once
per scene with main array `tx_static_radio_dbm` shape `(num_tx, H, W)`. This is
release packaging only and does not modify CARLA collection, QA, RF/Sionna, or
runtime dataset writing.

Important correction 2026-05-13: runtime static RSS cache must be generated with
zero active vehicle objects. A bad Town10 cache was found where
`scene_static/tx_*/rss_heatmap_meta.json` still listed active reference vehicles,
so the release `tx_static_radio_dbm` was mathematically consistent with
`dynamic_rss_dbm - delta_from_static_db` but semantically not a clean static
prior. `prepare-rf-cache` now excludes all vehicle objects for static cache
generation, and the release packager rejects runtime static caches whose metadata
contains non-empty `propagation.active_vehicle_objects`. Regenerate static cache
with `prepare-rf-cache --force`, then rerun RF/delta or rebuild the release
before using the current Town10 static prior for training.

## Training Release Split Ownership Update (2026-05-06 CST)

The standalone training release packager no longer writes dataset split content.
`episode_index.jsonl` contains sample identity and artifact paths only; it does
not include `split`. The packager also does not copy or generate `splits.json`
and does not record `split_counts` in `dataset_meta.json` or
`package_report.json`. Train/val/test partitioning is intentionally left to a
separate future split file/tool.

## MultiScene20 2-Worker CARLA Autorestart Collection Contract 2026-05-13

As of 2026-05-13, the active trajectory collection resume mode for
`configs/dynamic_radio/multi_scene_20x150_resolved.yaml` is 2 workers, not 4:

```text
workers: 2
GPU assignment: --gpu-ids 0,1
RPC ports: 2100,2110
Traffic Manager ports: 8100,8110
no_rendering_mode: enabled
restart budget per scene: 1000
outer autorestart loop: enabled by launcher under datasets/DynamicRadioMap/MultiScene20/logs/
```

CARLA infrastructure failures now stop the current `collect_from_plan_catalog`
run immediately so the multi-scene worker can clean the owned CARLA server and
restart instead of burning many attempts against a broken simulator. This is a
runtime stability behavior only. It does not change TrafficPlan sampling, vehicle
control, route generation, trajectory QA thresholds, TX assignment, or RF/Sionna
numeric behavior.

Infrastructure failure codes that trigger this multi-scene restart path include
CARLA server unavailable, collect subprocess timeout, post-attempt RPC/server
unavailable, CARLA RPC timeout, load-world timeout, CARLA segfault, and Traffic
Manager bind errors. Port cleanup now also targets stale `dynamic_radio_dataset.carla.collect`
subprocesses matching the worker RPC/TM ports, in addition to CARLA server
process groups.

## MultiScene20 CARLA Cleanup / GPU-Gated Startup Update 2026-05-13 CST

For trajectory-only multi-scene collection (`collect-multi-scene --no-rendering-mode`):

```text
final actor DestroyActor cleanup: skipped in no-rendering/no-video mode
clean-slate policy: each no-rendering attempt force-loads the requested town before spawning vehicles
validation_report.json: written immediately after recording completes, before cleanup/teardown
multi-scene collect subprocess timeout: capped at 180s for no-rendering mode
gpu_start_health_check: enabled by default for multi-scene CARLA workers
CARLA start thresholds: utilization <95%, temperature <88C
port release: treated as failure if CARLA/RPC/TM ports remain busy past timeout
```

Reason: repeated failures were isolated to concrete infrastructure stages, not
abstract CARLA instability:

- complete clips were aborting at `cleanup_started` when CARLA Python teardown RPC
  hit a C++ `TimeoutException` after the server became unavailable;
- one worker hung mid-recording at `recording_started` while `world.tick()` stopped
  advancing;
- during the latest run both GPUs were already at 100% utilization, causing CARLA
  UE4 startup `Signal 11` before RPC ports became ready.

These changes do not modify traffic plans, route generation, trajectory QA,
Sionna/RF, or TX assignment semantics.
