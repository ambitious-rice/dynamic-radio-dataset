# Project Plans

Last updated: 2026-05-03 CST

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

- `rf/processing.py`: formal process-rf API, trajectory-QA gate, completeness
  checks, GPU worker slots.
- `rf/runtime.py`: Sionna Python/env/cache setup shared by orchestration and
  manual episode jobs.
- `rf/episode_job.py`: single-episode export/RSS/QA artifact generation.
- `diagnostics/failure_report.py`: read-only dataset failure summaries.
- `testing/harness.py`: scratch RF assertions.

Still worth doing later, after release recovery:

- Move QA dataclasses/record helpers out of `radio_dataset_utils.py` without
  changing thresholds or JSON fields.
- Extract collection failure histogram helpers from `carla/runner.py` into a
  read-only diagnostics utility once current reporting is stable.
- Trim `render/rss_video.py` by separating formal cached rendering from review
  options.

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

