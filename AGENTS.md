# Agent Operating Guide

This workspace is a CARLA 0.9.15 tree with one active project layered on top:
a CARLA + Sionna RT dynamic radio-map dataset pipeline.

Active project code lives in:

```text
scripts/dynamic_radio_dataset/
```

`scripts/drd.py` is the preferred thin CLI wrapper. Root-level CARLA files,
CARLA examples, generated `datasets/`, and `tmp/archive_*` are not the active
implementation.

## Search Scope

The repository root includes large CARLA assets, PythonAPI examples, generated
datasets, videos, and archives. For normal project work, search scoped paths
first:

```text
scripts/dynamic_radio_dataset/
configs/dynamic_radio/
.agent/
docs/README.md
```

Avoid broad root scans unless the task explicitly requires CARLA upstream files
or generated artifacts.

## Startup Routine

Every new agent must:

1. Read this `AGENTS.md`.
2. Read `.agent/HANDOFF.md`.
3. For multi-step planning or milestone changes, also read `.agent/PLANS.md`.
4. For dataset contract, traffic plan, QA, Sionna/RSS, TX, index, or CLI
   changes, also read `.agent/DATASET_PIPELINE.md`.
5. Before coding, summarize the current state from `.agent/HANDOFF.md`.

Do not read `docs/archive/` by default. It contains historical audits and old
reports for forensic lookup only.

Before ending a turn, update `.agent/HANDOFF.md`. Update `.agent/PLANS.md`
when milestones, risks, or decisions change. Update `.agent/DATASET_PIPELINE.md`
in the same change as any dataset format, QA, collection, TX, or CLI contract
change.

## Current CLI

Run from repository root:

```bash
cd /share1/fzj/carla
python3 scripts/drd.py --help
python3 scripts/drd.py prepare-scene --config configs/dynamic_radio/town10_junction_smoke.yaml
python3 scripts/drd.py build-route-library --config configs/dynamic_radio/town10_junction_smoke.yaml
python3 scripts/drd.py generate-plans --config configs/dynamic_radio/town10_junction_smoke.yaml --num-plans 20
python3 scripts/drd.py collect --config configs/dynamic_radio/town10_junction_smoke.yaml --max-attempts 3
python3 scripts/drd.py process-rf --config configs/dynamic_radio/town10_junction_smoke.yaml --max-episodes 1
python3 scripts/drd.py finalize --config configs/dynamic_radio/town10_junction_smoke.yaml
```

Module form is valid when `scripts/` is on `PYTHONPATH`:

```bash
PYTHONPATH=scripts python3 -m dynamic_radio_dataset --help
```

Use the Sionna environment for Sionna-specific commands:

```bash
PYTHONNOUSERSITE=1 /share1/fzj/miniconda3/envs/sionna019/bin/python ...
```

Start CARLA only when needed, and check port/process health first:

```bash
./CarlaUE4.sh -RenderOffScreen -nosound -quality-level=Low -carla-rpc-port=2000
```

## Active Pipeline

The formal path is plan-first:

```text
RouteLibrary
  -> TrafficPlan bank
  -> preflight validation
  -> CARLA collection
  -> trajectory QA
  -> Sionna/RSS processing
  -> per-TX QA
  -> episode-TX index
```

Current baseline config: one Town10HD_Opt junction, three fixed TXs, 8 seconds
at 10 fps, 80 frames, 128x128 RSS, 3-6 requested-total vehicles, all-TX RF.
This is a dataset config, not a structural assumption; keep `scene_id`,
`tx_catalog`, RouteLibrary metadata, and index rows scene-aware.

## Module Boundaries

- `carla/` must not import Sionna, Mitsuba, or DrJit.
- `sionna/` and RF/render modules must not generate routes or traffic plans.
- `plans/` must not call subprocess or start CARLA.
- `qa/` must not call subprocess.
- `routes/` operates on route feature data, not live CARLA runtime.
- `__init__.py` must not import CARLA, Sionna, Mitsuba, or DrJit.
- Formal train/main is all-TX. `target_tx_first` is debug/smoke only.
- Trajectory-QA-failed episodes must not enter Sionna/RSS processing.

## Engineering Rules

- Do not add new core logic to root `scripts/`; add it under
  `scripts/dynamic_radio_dataset/`.
- Do not hard-code route ids, plan ids, episode ids, attempt ids, TX-specific
  branches, or dataset paths in active production code.
- Use config, metadata, `TrafficPlan`, RouteLibrary features, `tx_catalog`, QA
  thresholds, and profiles for special behavior.
- Failures must surface explicit `failure_code`, log paths, stage metadata, and
  summary counts.
- Do not silently fallback after CARLA subprocess failure, route mismatch, plan
  schema mismatch, Sionna export failure, RSS shape mismatch, or RF QA failure.
- Do not lower QA thresholds, reshape bad arrays, skip failed TXs, or generate
  fake RSS to make smoke/pilot pass.
- Temporary workarounds must state reason, scope, and removal condition.

## Verification

Use these static checks after code or contract changes:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

If CARLA is unavailable, do not force collection. If CARLA is available for a
behavior change, run at most a smoke attempt unless the user explicitly
authorizes pilot or main collection.

## Done When

- Requested code/docs are updated.
- Relevant checks pass, or failures are explained.
- Important artifact paths are listed.
- `.agent/HANDOFF.md` is updated.
