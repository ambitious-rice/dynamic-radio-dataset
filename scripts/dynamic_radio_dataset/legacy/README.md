# Legacy Quarantine

This directory is not part of the active pipeline. Active dataset work must use:

```bash
python3 scripts/drd.py ...
```

or the equivalent `dynamic_radio_dataset` module CLI.

Deprecated paths include:

- `build_single_scene_radio_dataset.py collect-dataset`
- random `collect_one_episode()`
- random `choose_num_target_vehicles()`
- hard-coded demo route catalogs as production route selection logic
- TX-conditioned demo orchestrators
- old CARLA/RSS overlay renderers

The remaining files here are notes only. Historical runnable legacy entrypoints
were archived to:

```text
tmp/archive_legacy_20260428/dynamic_radio_dataset_legacy/
tmp/archive_legacy_20260429/legacy_generate_carla_traffic_plans.py
```

Do not import this directory from active code, do not call it through
subprocess, and do not fix active workflow problems by editing historical
legacy scripts. If active code needs behavior that once lived in a legacy
script, migrate the small function into the appropriate current module and keep
the active module boundaries intact.

Active replacements:

- route/traffic planning: `routes/route_library.py`, `plans/sampler.py`, `plans/validator.py`
- CARLA collection: `carla/runner.py`, `carla/collect.py`
- trajectory QA: `qa/trajectory_qa.py`
- Sionna/RSS processing: `pipeline/process_rf.py`
- RSS/review visualization: `render/rss_video.py`, `render/review_pack.py`
- final index/splits: `pipeline/stages.py`, `pipeline/process_rf.py`
