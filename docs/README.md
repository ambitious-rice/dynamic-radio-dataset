# Documentation Map

Last updated: 2026-05-03 CST

Use this file to avoid loading old project history by default.

## Active Docs

- `AGENTS.md`: operating rules, startup routine, current CLI, verification.
- `.agent/HANDOFF.md`: current handoff and next recommended work.
- `.agent/PLANS.md`: current milestone and pilot/main gates.
- `.agent/DATASET_PIPELINE.md`: dataset contract, active modules, QA policy,
  accepted products, and failure rules.
- `docs/RUNTIME_ENVIRONMENT.md`: CARLA/Sionna env split, GPU cache isolation,
  tmux/tmp notes, and CARLA process checks.
- `docs/QA_AND_FAILURES.md`: QA artifact policy, hard/soft/diagnostic
  meanings, and read-only failure-report usage.
- `README`: root project quick start.

## Archive

Historical reports and long audits are under:

```text
docs/archive/
```

Do not read archive files during normal startup. Use them only for targeted
questions such as:

- why a specific legacy script was archived;
- why a particular QA rule changed;
- what happened in the 2026-04-28 smoke/layered validation;
- historical CARLA stability evidence.
- detailed validation and GPU-fix timelines from late April and early May 2026.

Archived files are evidence, not current policy. Current policy lives in
`.agent/` and this README.

## Token-Cost Policy

- Prefer current-state docs over chronological records.
- Keep handoff concise; move detailed evidence to archive.
- Do not duplicate long command outputs in `.agent/`.
- If a doc describes old behavior, label it as historical or archive it.
- Search `scripts/dynamic_radio_dataset/`, `configs/dynamic_radio/`,
  `.agent/`, and this README before broad root scans; the root contains large
  CARLA assets and generated artifacts.
- If active code/config/CLI behavior changes, update `.agent/HANDOFF.md` and,
  when the contract changes, `.agent/DATASET_PIPELINE.md`.
