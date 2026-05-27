# Training Release Packager

Standalone packager for creating a minimal training dataset from an already
valid DRD runtime dataset. It does **not** run CARLA, Sionna, RF processing, QA,
or the existing `drd.py` CLI, and it never modifies `--runtime-root`.

## Usage

```bash
python3 scripts/release_packager/package_training_release.py \
  --runtime-root datasets/DynamicRadioMap/Town10 \
  --out-root datasets/DynamicRadioMapRelease/Town10_v1
```

Options:

- `--mode copy|hardlink|symlink`, default `copy`.
- `--overwrite` replaces an existing output root. Runtime data is never deleted
  or moved.
- `--dry-run` validates and prints counts without materializing training files.

## Release layout

Static scene/TX radio artifacts are scene-level shared files, not per-episode
copies. Episodes only carry dynamic inputs and labels, and index rows reference
the scene-level static radio file.

```text
Town10_v1/
  dataset_meta.json
  episode_index.jsonl
  package_report.json
  scenes/
    <scene_id>/
      scene_meta.json
      tx_catalog.json
      tx_static_radio/
        tx_static_radio_maps.npz
        tx_static_radio_meta.json
      episodes/
        episode_000001/
          episode_meta.json
          tx_assignment.json          # selected-TX datasets only
          dynamic_input.npz
          rss_dynamic_dbm.npz
```

`tx_static_radio_maps.npz` contains the scene-level static radio map array
`tx_static_radio_dbm` with shape `(num_tx, H, W)`, plus static masks and TX
id/position arrays. For the current Town10 runtime this scene-level map is
derived from existing runtime artifacts as:

```text
rss_dynamic_dbm.npz:dynamic_rss_dbm - rss_delta_from_static_db.npz:delta_from_static_db
```

The packager validates that the derived map is frame-invariant for the source
episode before writing it. It does not run Sionna or synthesize fake radio maps.

## Current runtime layout adapter

If runtime `episode_index.jsonl` is absent, the packager uses QA-accepted,
RF-complete episode directories. Split files and split fields are intentionally
not written by this packager; train/val/test partitioning should be produced by
a separate tool later. Current `episodes/*/traffic_grid_uint8.npz` is
materialized as release `episodes/*/dynamic_input.npz`.

Only training-core files are materialized. Runtime/debug directories and files
such as `attempts/`, `failed_attempts/`, `diagnostics/`, `logs/`,
`plan_catalog/`, `route_library/`, `reference_scene/`, `sionna_export/`,
`rf_cache/`, `tx_*/`, `frames/actor_states.jsonl`, QA reports, videos, and
stdout/stderr logs are not packaged.
