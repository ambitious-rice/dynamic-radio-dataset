# Dynamic Radio Dataset Pipeline

Last updated: 2026-05-03 CST

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
- `routes/route_library.py`: route feature cache and route diagnostics.
- `plans/schemas.py`: TrafficPlan schema.
- `plans/sampler.py`, `plans/validator.py`, `plans/catalog.py`: plan bank
  sampling, preflight validation, and catalog IO.
- `plans/pilot_selector.py`, `plans/validation_selector.py`: diagnostic or
  deterministic selection helpers.
- `carla/collect.py`: CARLA clip collection CLI.
- `carla/runner.py`: plan-catalog attempt orchestration and resume behavior.
- `qa/trajectory_qa.py`: pre-Sionna trajectory QA.
- `qa/rss_diagnostics.py`: RSS diagnostics and duplicate-static checks.
- `pipeline/stages.py`: prepare/finalize/release stage orchestration.
- `pipeline/process_rf.py`: compatibility CLI for scene prep, per-episode RF,
  and finalize-index.
- `rf/processing.py`: formal RF scheduling, trajectory-QA gate, and RF
  completeness checks.
- `rf/runtime.py`: Sionna Python/env/cache setup.
- `rf/episode_job.py`: single-episode export/RSS/traffic-grid/QA artifact job.
- `pipeline/reports.py`: timing report and full-run extrapolation.
- `pipeline/supervisor.py`: unattended run supervisor.
- `sionna/export.py`: Sionna/Mitsuba export.
- `render/rss_video.py`, `render/sample.py`, `render/review_pack.py`: cached
  RSS rendering and diagnostics.
- `diagnostics/failure_report.py`: read-only failure analysis.
- `checks/check_contract.py`: engineering and data-contract checks.

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

