# QA And Failures

Last updated: 2026-05-03 CST

## QA Artifacts

Trajectory QA writes `trajectory_qa.json` before RF. RF/per-TX QA writes
`qa_report.json`, `episode_meta.json`, `rf_process_meta.json`, and RSS/traffic
artifacts during `process-rf`.

Only episodes with `trajectory_qc_pass=true` may enter RF. Only scene-QA and
per-TX-QA accepted rows enter `episode_index.jsonl`.

## Hard, Soft, Diagnostic

Hard trajectory blockers include:

- incomplete clips or actor tracks;
- missing required controlled vehicles;
- missing primary controlled vehicles;
- vehicle-vehicle or vehicle-building collisions;
- frame jumps;
- long all-stop episodes;
- invalid vehicle mix;
- required primary vehicles not moving enough.

Soft warning:

- lane-adherence near junction turns.

Diagnostics only:

- core visit count;
- TX corridor hit/crossing;
- TX clearance;
- route mix;
- label crossing count;
- legacy passed-target count.

RF/per-TX QA rejects insufficient dynamic RSS change. Dynamic RSS metrics use
valid non-building common-mask statistics and exclude NaN/Inf/floor-clipped
cells.

## Failure Discipline

Every formal failure path should preserve:

- `failure_code`;
- stage metadata;
- stdout/stderr/log paths when available;
- summary histograms/counts;
- enough path context to reproduce or inspect the failure.

Do not hide failures through fake RSS arrays, invalid reshaping, skipped TXs,
lowered thresholds, or silent CPU/GPU fallback.

## Failure Report

Generate a read-only failure report:

```bash
python3 scripts/drd.py failure-report --config configs/dynamic_radio/dynamic_radiomap_town10_300.yaml
```

The default output is:

```text
<dataset-root>/diagnostics/failure_report.json
```

The tool scans:

```text
collection_summary.json
attempts/attempt_*/attempt_meta.json
failed_attempts/*/attempt_meta.json
failed_attempts/*/failure.json
episodes/episode_*/trajectory_qa.json
episodes/episode_*/plan.json
episodes/episode_*/qa_report.json
episodes/episode_*/rf_process_meta.json
rf_failure_summary*.json
episode_index.jsonl
```

It does not start CARLA, run Sionna, mutate QA decisions, or change release
artifacts.

