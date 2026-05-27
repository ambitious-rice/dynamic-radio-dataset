# Offline QA / Failcase Audit

Standalone audit scripts for an existing DynamicRadioMap dataset.

This directory is intentionally outside `scripts/dynamic_radio_dataset/`.
It does not participate in the formal CARLA/Sionna data generation pipeline and
does not modify collection, route-library, traffic-plan, trajectory-QA, RF,
finalize, index, split, or config logic.

## Usage

Run from the repository root:

```bash
python3 -m py_compile scripts/qa_audit/*.py
python3 scripts/qa_audit/audit_dataset.py --dataset-root datasets/DynamicRadioMap/Town10
```

Default output:

```text
datasets/DynamicRadioMap/Town10/diagnostics/qa_audit_<timestamp>/
```

The audit reads existing dataset files only and writes new diagnostics under
that output directory.

## Files Read

The script scans available files under:

```text
episodes/episode_*/
attempts/attempt_*/
failed_attempts/attempt_*/
collection_summary.json
rf_failure_summary*.json
```

Per accepted episode it tries to read:

```text
episode_meta.json
plan.json
trajectory_qa.json
qa_report.json
scene_meta.json
validation_report.json
frames/actor_states.jsonl
rf_process_meta*.json
```

Missing or partial records are logged to
`samples/missing_or_partial_records.json`; they do not abort the full audit.

## Outputs

```text
summary.md
summary.json
tables/
  failure_cases.csv
  accepted_episode_metrics.csv
  actor_behavior_metrics.csv
  route_failure_rates.csv
  bucket_failure_rates.csv
samples/
  suspicious_accepted_cases.json
  top_failure_routes.json
  missing_or_partial_records.json
```

## Metrics

Behavior metrics are lightweight offline metrics computed from
`frames/actor_states.jsonl`:

- speed mean / p95 / max
- signed acceleration-derived harsh brake / acceleration counts
- absolute acceleration mean / p95 / max
- jerk p95 / max
- stationary duration
- all-stop duration
- displacement-based route progress proxy
- min inter-vehicle center distance and bbox-radius gap

The suspicious score is only an audit ranking for human review. It is not a
formal QA gate.

Metrics that require reliable CARLA map/waypoint semantics are marked
`unavailable` rather than inferred:

- lane deviation
- offroad ratio
- true distance-along-route progress

