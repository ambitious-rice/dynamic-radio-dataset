# Project Handoff

Last updated: 2026-05-03 CST

## Current State

Active implementation is the plan-first CARLA + Sionna RT dynamic radio-map
pipeline under `scripts/dynamic_radio_dataset/`. Use `python3 scripts/drd.py`
as the main CLI; root-level CARLA files, examples, generated `datasets/`, and
`tmp/archive_*` are not active implementation.

The formal path remains:

```text
RouteLibrary -> TrafficPlan bank -> preflight validation -> CARLA collection
-> trajectory QA -> Sionna/RSS RF processing -> per-TX QA -> finalize index/splits
```

`target_tx_first`, offline TX search, and flat-RSS allowances are debug or
harness affordances only. Formal train/main processing is all-TX, and
trajectory-QA-failed episodes must not enter RF processing.

## Current Code Shape

RF responsibilities have been split without changing the formal CLI or dataset
schema:

- `pipeline/stages.py`: stage orchestration, finalize, verify/prune release.
- `rf/processing.py`: trajectory-QA gate, RF completeness checks, GPU/worker
  episode scheduling, `rf_failure_summary.json`.
- `rf/runtime.py`: shared Sionna Python/runtime/cache environment.
- `rf/episode_job.py`: single-episode export/RSS/traffic-grid/QA artifact job.
- `pipeline/process_rf.py`: compatibility CLI for `prepare-scene`,
  `process-episode`, and `finalize-index`.
- `diagnostics/failure_report.py`: read-only failure diagnostics.
- `testing/harness.py`: optional scratch RF assertions.

## Formal Release State

Current formal release config:

```text
config: configs/dynamic_radio/dynamic_radiomap_town10_300.yaml
runner: scripts/run_DynamicRadioMap_Town10_300.sh
dataset root: datasets/DynamicRadioMap/Town10
target: 300 trajectory-QA-accepted episodes / 900 episode-TX rows
shape: (3, 100, 128, 128)
RF: cuda_ad_rgb, gpu_ids 0,1, rf_workers 2
```

Local status check on 2026-05-03:

```text
episodes/episode_* dirs: 300
collection_summary: target_reached=true, stopped_reason=selection_targets_reached
episode RSS outputs present: 262/300
episode_index.jsonl: absent
supervisor_status: failed at RF stage
rf_failure_summary.json: processed=262, failed=38
active run-supervised/drd shell process: none found
CARLA process: still running on port 2000 from the release run
```

Do not prune or delete `datasets/DynamicRadioMap/Town10`; it contains the
formal collection and partial RF outputs. Resume RF with care: the formal
`process-rf` path skips complete episodes and queues incomplete trajectory-QA
accepted episodes.

## Environment Notes

- CARLA/main env: `/share1/fzj/miniconda3/envs/carla0915/bin/python`.
- Sionna/RF env: `/share1/fzj/miniconda3/envs/sionna019/bin/python` with
  `PYTHONNOUSERSITE=1`.
- `networkx==3.1` is installed inside the isolated `carla0915` env; keep the
  runner preflight check because missing `networkx` caused prior route mismatch
  failures.
- GPU RF uses `CUDA_DEVICE_ORDER=PCI_BUS_ID` and isolated cache homes under
  `DRD_SIONNA_RUNTIME_BASE`, defaulting to `/dev/shm/fzj_drd_sionna_rf`.
- `/tmp` is on full root storage; use
  `TMUX_TMPDIR=/share1/fzj/tmux_tmp tmux ...` until root space is restored.
- Root `/` is full, but `/share1/fzj` and `/dev/shm` have usable space at the
  last check.

## Repository Tracking

Root Git tracking was initialized on 2026-05-03 on branch `main`. The root
`.gitignore` defaults to ignoring the CARLA release tree, generated datasets,
temporary outputs, videos, logs, caches, and nested upstream repositories, then
explicitly allows the active project layer: `.agent/`, `configs/dynamic_radio/`,
`docs/README.md`, `docs/QA_AND_FAILURES.md`, `docs/RUNTIME_ENVIRONMENT.md`,
`scripts/drd.py`, `scripts/run_DynamicRadioMap_Town10_300.sh`,
`scripts/setup_umodel_toolchain.sh`, and `scripts/dynamic_radio_dataset/`.

At initialization time CARLA was still running on port 2000 and a dual-GPU RF
job was running under `datasets/refactor_20ep_dual_gpu_20260503`; only Git
metadata and documentation files were touched.

## Verified Baselines

20-episode 10s validation remains the verified GPU baseline:

```text
config: configs/dynamic_radio/town10_junction_10s_4to7_validation.yaml
root: datasets/single_scene_10s_4to7_validation_20260430
trajectory accepted: 20/20
episode-TX rows after finalize: 60/60
shape: (3, 100, 128, 128)
GPU rerun: 20/20 processed, 10 episodes on GPU 0 and 10 on GPU 1
```

The previous medium 50-episode run remains valid as an 8s end-to-end baseline:

```text
root: datasets/single_scene_medium_50_20260429
trajectory accepted: 50
episode-TX rows: 150
sample renders: 15/15
```

Historical timeline details from the validation and GPU-fix work were archived
under `docs/archive/`.

## Useful Commands

```bash
python3 scripts/drd.py --help
python3 scripts/drd.py process-rf --config configs/dynamic_radio/dynamic_radiomap_town10_300.yaml --use-gpu --gpu-ids 0,1 --rf-workers 2
python3 scripts/drd.py failure-report --config configs/dynamic_radio/dynamic_radiomap_town10_300.yaml
python3 scripts/drd.py finalize --config configs/dynamic_radio/dynamic_radiomap_town10_300.yaml
python3 scripts/drd.py verify-release --config configs/dynamic_radio/dynamic_radiomap_town10_300.yaml --expected-episodes 300
```

Static verification after code changes:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

## Next Work

1. Diagnose the 38 RF failures in `datasets/DynamicRadioMap/Town10` with
   `failure-report` and targeted episode stderr/stdout reproduction.
2. Resume RF only after confirming the failure mode; completed RF episodes
   should be skipped by completeness checks.
3. Run `finalize` and `verify-release` after all 300 episodes have complete RF
   artifacts.
4. Prune the release root only after `verify-release` passes.
