# Vehicle Blueprint RF Audit

Standalone research tool for screening CARLA `vehicle.*` blueprints for a 2D
occupancy radio-map dataset.

## Motivation

Some high-clearance or detailed large-vehicle meshes can create underbody or
mesh-specific propagation paths in Sionna RT. The current main learning input is
only 2D vehicle occupancy, with no chassis height or detailed body geometry.
Those vehicles can therefore create label variation that is not explainable from
the input. This audit treats vehicle selection as an **input-label consistency
constraint**: keep vehicles whose RF shadow can be reasonably approximated by a
2D occupancy solid blocker; exclude vehicles with obvious underbody bright spots
or unstable shadows.

This tool is diagnostic-only. It does not modify the formal DRD pipeline, does
not change CARLA collection, does not change Sionna numeric algorithms, and does
not post-process or repair RSS labels.

## What it does

The audit can:

1. Read candidate vehicle blueprints from JSON/JSONL/CSV, or optionally enumerate
   `vehicle.*` blueprints from a running CARLA server.
2. Apply explicit visual/geometry review so obviously high-clearance vehicles
   can be excluded without sending every blueprint through Sionna RT. Large
   vehicle names alone are not treated as hard exclusions.
3. Optionally read fixed-scene RF test outputs per blueprint and compute:
   - `shadow_strength`
   - `underbody_bright_spot_score`
   - `shadow_consistency_score`
   - `affected_area_ratio`
4. Write:
   - `vehicle_audit_report.csv`
   - `vehicle_whitelist.json`
   - `vehicle_exclusion_reasons.json`
   - `audit_manifest.json`
   - preview heatmap images under `previews/`

## Fixed RF test context

The default RF test-plan metadata uses the previous underbody bright-spot
investigation context:

```text
scene_id: town10_junction_0189
reference_episode_id: episode_000051
reference_tx_id: tx_00
reference_frame: 55
```

The script does not run Sionna by itself. It writes a test plan and audits RF map
outputs if they are provided. This keeps the tool independent from the main
pipeline and avoids changing numeric RF behavior.

Expected optional RF case layout:

```text
rf_root/
  vehicle.audi.a2/
    baseline.npz
    with_vehicle.npz
  vehicle.carlamotors.carlacola/
    difference.npz
```

Accepted NPZ keys include `rss_dbm`, `dynamic_rss_dbm`, `radio_map_dbm`,
`difference_db`, and `delta_db`. If 3D/4D arrays are given, the tool reduces them
to a representative 2D map for scoring. Optional masks can be named
`vehicle_mask.*`, `occupancy_mask.*`, or `shadow_mask.*`.

## Candidate files

JSON list of strings:

```json
[
  "vehicle.audi.a2",
  "vehicle.carlamotors.carlacola"
]
```

or JSON objects:

```json
{
  "blueprints": [
    {"blueprint_id": "vehicle.audi.a2"},
    {"blueprint_id": "vehicle.carlamotors.carlacola"}
  ]
}
```

CSV requires a `blueprint_id` column.

## Visual review file

Use this when exported vehicle views show high clearance or open underbody:

```json
{
  "vehicle.carlamotors.carlacola": {
    "visual_clearance": "high",
    "exclude": true,
    "reason": "obvious high chassis/open underbody in side view"
  }
}
```

## Usage

Audit candidate metadata only:

```bash
python3 scripts/drd_research/vehicle_rf_audit/vehicle_rf_audit.py \
  --blueprints vehicle_candidates.json \
  --view-review vehicle_view_review.json \
  --out-dir tmp/vehicle_rf_audit
```

Write an RF test plan using the fixed default context:

```bash
python3 scripts/drd_research/vehicle_rf_audit/vehicle_rf_audit.py \
  --blueprints vehicle_candidates.json \
  --write-rf-test-plan \
  --out-dir tmp/vehicle_rf_audit
```

Audit with completed RF before/after maps:

```bash
python3 scripts/drd_research/vehicle_rf_audit/vehicle_rf_audit.py \
  --blueprints vehicle_candidates.json \
  --rf-root tmp/vehicle_rf_cases \
  --out-dir tmp/vehicle_rf_audit
```

Optionally enumerate live CARLA blueprints:

```bash
python3 scripts/drd_research/vehicle_rf_audit/vehicle_rf_audit.py \
  --enumerate-carla \
  --carla-host 127.0.0.1 \
  --carla-port 2000 \
  --out-dir tmp/vehicle_rf_audit
```

## Decision policy

- Whitelist low-body/closed-body vehicles or RF cases with strong, consistent
  shadows and low underbody bright-spot score.
- Exclude vehicles with high `underbody_bright_spot_score`, weak shadow strength,
  low consistency, or manual visual high-clearance review.
- Large vehicles are not categorically excluded; they are excluded only when the
  visual/RF evidence indicates underbody leakage or unstable shadows.

## Optional RF command template

If you already have an external fixed-scene RF test runner, the audit tool can
invoke it per blueprint and then score the files it writes under `--rf-root`.
This is intentionally a template hook rather than a new Sionna implementation.
Available placeholders:

```text
{blueprint_id}          raw CARLA blueprint id
{blueprint_id_shell}    shell-quoted blueprint id
{case_dir}              output case directory for that blueprint
{case_dir_shell}        shell-quoted case directory
{scene_id}
{reference_episode_id}
{reference_tx_id}
{reference_frame}
```

Example:

```bash
python3 scripts/drd_research/vehicle_rf_audit/vehicle_rf_audit.py \
  --blueprints vehicle_candidates.json \
  --rf-root tmp/vehicle_rf_cases \
  --rf-command-template 'python3 my_fixed_rf_probe.py --blueprint {blueprint_id_shell} --out-dir {case_dir_shell}' \
  --skip-prior-excluded-rf \
  --out-dir tmp/vehicle_rf_audit
```

The external command is expected to write either `baseline.npz` +
`with_vehicle.npz`, or `difference.npz`, inside `{case_dir}`. Stdout/stderr are
captured in that case directory. This keeps Sionna execution explicit and
outside the production pipeline.

The generated `rf_test_plan.json` includes the default substitution pose from
`episode_000051` frame 55 (`car_40`, originally `vehicle.mitsubishi.fusorosa`),
including world transform and bounding-box extent, so an external probe can place
each candidate at the same fixed location.
