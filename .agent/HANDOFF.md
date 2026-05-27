# Project Handoff
Last updated: 2026-05-26 CST

## CARLA State GitHub Sync for RF Migration 2026-05-27 CST

User wants the target server to reuse CARLA-side simulation results and rerun
only the heavier Sionna/RF stages where possible. Added an explicit CARLA-state
migration tool instead of copying the whole `datasets/` tree:

```text
new:
  scripts/dynamic_radio_dataset/migration/__init__.py
  scripts/dynamic_radio_dataset/migration/carla_state.py

updated:
  scripts/dynamic_radio_dataset/cli.py
  docs/README_MIGRATION.md
```

New CLI commands:

```bash
PYTHONPATH=scripts python3 scripts/drd.py export-carla-state \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --output-dir carla_state/MultiScene20 \
  --archive carla_state/MultiScene20.tar.gz \
  --overwrite

PYTHONPATH=scripts python3 scripts/drd.py import-carla-state \
  --source carla_state/MultiScene20.tar.gz \
  --destination-root .
```

The export whitelist includes accepted episode trajectories/plans/trajectory
QA/TX assignments, `scene_static` TX catalogs and scene signatures, reference
scene metadata, and by default the reference Sionna export used as static
building proxy source. It excludes dynamic RSS, static RSS caches, per-TX
`rss_maps.npz`, episode Sionna exports, RF process metadata, videos, logs, and
other generated RF products.

Dry-run size check on the current MultiScene20 state:

```text
command:
  PYTHONPATH=scripts python3 scripts/drd.py export-carla-state \
    --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
    --output-dir tmp/carla_state_multiscene20_dryrun \
    --dry-run --overwrite

result:
  accepted episodes: 3000
  whitelisted files: 33542
  uncompressed size: 1706.155 MiB
  manifest: tmp/carla_state_multiscene20_dryrun/carla_state_manifest.json
```

Conclusion: the state is reusable but not small enough to mix into the normal
code branch. Use a separate GitHub data branch such as
`carla-state-multiscene20`; if the archive exceeds GitHub's 100 MB per-file
limit, split it with `split -b 90M` and reconstruct with `cat` on the target.
`docs/README_MIGRATION.md` contains the exact commands.

Validation passed after this change:

```text
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
python3 scripts/drd.py export-carla-state --help
python3 scripts/drd.py import-carla-state --help
```

## Code-Only Migration Guidance 2026-05-27 CST

User plans to migrate the current project code to another server and rerun
collection/simulation there. Dataset artifacts, temporary experiments, archives,
logs, and intermediate runtime records should not be migrated.

Local Git migration commit is ready:

```text
branch: dynamic-radio-migration-sionna2
commit: 2743738 Prepare dynamic radio migration with Sionna RT 2
```

GitHub SSH status on 2026-05-27:

```text
ssh -T -p 443 git@ssh.github.com
  authenticated as ambitious-rice

ssh -T git@github.com
  port 22 connection closed by remote host
```

Use `ssh://git@ssh.github.com:443/<owner>/<repo>.git` as the remote URL if port
22 remains blocked. Candidate repos checked
`ambitious-rice/carla`, `ambitious-rice/dynamic-radio-dataset`, and
`ambitious-rice/carla-sionna-dynamic-radio` did not exist. User still needs to
provide/create an empty GitHub repo URL before push can proceed.

Follow-up: user prefers GitHub-based migration over tar/rsync. Added migration
environment files and a target-server checklist:

```text
envs/dynamic-radio-orchestrator.yaml
envs/sionna-rt-2x.yaml
envs/README.md
docs/README_MIGRATION.md
```

`envs/sionna-rt-2x.yaml` installs `sionna-rt==2.0.1` from official PyPI through
pip because the system `python3 -m pip index versions sionna-rt` on this host
only saw `1.1.0`, while the miniconda pip/index saw the current `2.0.1`.

Recommended migration boundary:

```text
include:
  AGENTS.md
  .agent/
  docs/README.md
  scripts/drd.py
  scripts/dynamic_radio_dataset/
  configs/dynamic_radio/
  small repo metadata needed by scripts, if any

exclude:
  datasets/
  tmp/
  docs/archive/
  __pycache__/
  *.pyc
  large generated videos/images/logs
  CARLA runtime outputs and caches
```

Important: the current working tree contains many uncommitted and untracked
active project files. A plain `git clone` or copying only tracked files will miss
important code/config. Use a working-tree rsync/tar package, or commit the active
changes first and clone that branch.

Target server prerequisites:

```text
CARLA 0.9.15 compatible runtime
Python orchestration env with repo PYTHONPATH=scripts
Sionna RT env, preferably durable sionna-rt==2.0.1 for new RF runs
optional legacy Sionna 0.19 env only if exact old behavior must be reproduced
```

The RF runtime can be pointed at a new Sionna interpreter via:

```bash
DRD_SIONNA_PYTHON=/path/to/sionna2/bin/python python3 scripts/drd.py ...
```

After migration, run:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

## Sionna RT 2.x Compatibility Upgrade 2026-05-26 CST

User asked to upgrade Sionna RT to the latest system and rewrite the simulation
code for the latest API because Sionna 0.19 is old and slow.

Implemented a compatibility layer instead of hard-breaking the old path:

```text
new:
  scripts/dynamic_radio_dataset/sionna/rt_compat.py

updated:
  scripts/dynamic_radio_dataset/rf/rss_compute.py
  scripts/dynamic_radio_dataset/render/rss_video.py
  .agent/DATASET_PIPELINE.md
```

Behavior:

```text
Sionna 0.19 scenes with scene.coverage_map:
  continue using scene.coverage_map(...)

Sionna RT 2.x scenes without scene.coverage_map:
  use RadioMapSolver()(scene,
      center=...,
      orientation=...,
      size=...,
      cell_size=...,
      samples_per_tx=...,
      max_depth=...,
      los=...,
      specular_reflection=...,
      diffuse_reflection=...,
      diffraction=...,
      edge_diffraction=...)
```

Important Sionna 2.x API issue found and fixed:

```text
load_scene() defaults to merge_shapes=True in Sionna RT 2.x.
That collapses exported object names to no-name-* and breaks:
  static_buildings_proxy lookup
  car_<id> motion updates
  include/exclude vehicle filtering

rt_compat.load_scene_preserving_names() now calls:
  load_scene(scene_xml, merge_shapes=False)
when the argument exists.
```

Installed latest Sionna RT for smoke testing in a temporary environment:

```text
/tmp/sionna2test/bin/python
sionna-rt 2.0.1
mitsuba 3.8.0
drjit 1.3.1
```

The workspace/shared-disk venv attempt at
`tmp/venv_sionna_rt_latest` was abandoned because `venv`/`ensurepip` and removal
on the shared project filesystem were extremely slow. `/tmp/sionna2test` worked
normally. For production, create a durable non-`/tmp` clone of that environment
or install `sionna-rt==2.0.1` in a managed conda/venv location with faster local
IO.

The RF runtime now supports a Python override without editing all YAML configs:

```bash
DRD_SIONNA_PYTHON=/path/to/new/sionna-rt/bin/python \
  python3 scripts/drd.py process-multi-scene-rf ...
```

If unset, `scripts/dynamic_radio_dataset/rf/runtime.py` keeps the existing
default/config behavior and uses the legacy `sionna019` interpreter.

Smoke tests passed:

```text
legacy formal RF smoke:
PYTHONNOUSERSITE=1 PYTHONPATH=scripts \
  /share1/fzj/miniconda3/envs/sionna019/bin/python \
  -m dynamic_radio_dataset.rf.rss_compute ...

output:
  tmp/sionna_rt_compat_legacy_smoke_20260526/rss_maps.npz
  backend metadata: sionna 0.19.0, API=scene.coverage_map

legacy fresh render smoke:
PYTHONNOUSERSITE=1 PYTHONPATH=scripts \
  /share1/fzj/miniconda3/envs/sionna019/bin/python \
  -m dynamic_radio_dataset.render.rss_video ...

output:
  tmp/sionna_rt_compat_render_legacy_smoke_20260526/rss_maps.npz
  backend metadata: sionna 0.19.0, API=scene.coverage_map

latest formal RF smoke:
MI_DEFAULT_VARIANT=llvm_ad_rgb PYTHONPATH=scripts \
  /tmp/sionna2test/bin/python \
  -m dynamic_radio_dataset.rf.rss_compute ...

output:
  tmp/sionna_rt_compat_latest_smoke_20260526/rss_maps.npz
  backend metadata: sionna-rt 2.0.1, API=RadioMapSolver
```

Note: with Sionna RT 2.0.1/Mitsuba 3.8 on this host, requesting
`MI_DEFAULT_VARIANT=llvm_ad_rgb` resulted in Mitsuba reporting
`llvm_ad_mono_polarized` or `cuda_ad_mono_polarized`. This appears to be the new
variant naming/selection behavior, not a project-level failure.

Validation passed:

```text
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.render.rss_video --help
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.rf.rss_compute --help
```

## Fresh Sionna Legacy-Style Video Reproduction Check 2026-05-26 CST

User pointed to the reference smooth fresh-Sionna video:

```text
tmp/fresh_sionna_legacy_style_12s_rx080/rss_heatmap_video.mp4
```

The correct reproduction is:

```text
tmp/reproduce_reference_exact_fresh_sionna_4cars_20260526/rss_heatmap_video.mp4
tmp/reproduce_reference_exact_fresh_sionna_4cars_20260526/rss_heatmap_meta.json
tmp/reproduce_reference_exact_fresh_sionna_4cars_20260526/rss_maps.npz
```

It uses the archived source export:

```text
tmp/archive_datasets_20260502_221203/datasets/dynamic_radio_scene_demo/sionna_export
```

Metadata comparison against the reference confirmed the important settings
match:

```text
rx_grid.region = support_region
rx_grid.resolution = [128, 128]
rx_grid.height_m = 0.8
rx_grid.cell_size_m = [0.875, 0.875]
tx.position = [-60.0, 0.0, 3.0]
active_vehicle_objects = [car_59, car_60, car_63, car_65]
num_samples = 1200000
num_runs = 1
max_depth = 3
reflection = true
diffraction = false
edge_diffraction = false
scattering = false
frames.count = 120
frames.frame_indices = 7..126
visual_fill_threshold_dbm = -200.0
visual_smooth_sigma = 0.8
plot_style = clean
display_mode = absolute
vehicle_overlay = dark
view_region = support
tx_marker_visible = false
```

The old reference metadata did not have explicit `visual_fill_enabled` or
`interpolation` fields because those were implicit in the old renderer. The new
reproduction explicitly records:

```text
visual_fill_enabled = true
interpolation = bilinear
```

This matches the legacy continuous visual style that produced the reference
video.

Self-check artifacts:

```text
tmp/reproduce_reference_exact_fresh_sionna_4cars_20260526/compare_to_reference/reference_vs_reproduction_contact_sheet.png
tmp/reproduce_reference_exact_fresh_sionna_4cars_20260526/compare_to_reference/frame_compare_summary.json
tmp/reproduce_reference_exact_fresh_sionna_4cars_20260526/compare_to_reference/rss_array_compare_summary.json
```

Frame-level visual comparison on frames 0, 1, 10, 20, 40, 60, 80, 100, 119:

```text
mean RGB MAE = 2.42e-05
max sampled-frame RGB MAE = 5.03e-05
pixels with RGB channel diff > 20 = 0
```

RSS-array comparison:

```text
shape = [120, 128, 128]
total cells = 1,966,080
mean_abs_db = 0.000617
p99_abs_db = 7.63e-06
cells_gt_1db = 6
cells_gt_20db = 6
```

Interpretation: the reproduced video is effectively identical to the user's
reference video. The tiny RSS-array differences are limited to 6 cells, mostly
no-hit versus hit stochastic boundary cases, and are not visible in the rendered
video.

Follow-up clarification for the 96m x 96m formal region question:

```text
formal valid_crop: 96m x 96m at 128x128 -> 0.75m cell size
reference support_region: 112m x 112m at 128x128 -> 0.875m cell size
```

Sionna 0.19 `Scene.coverage_map()` documentation in the installed environment
defines each output cell as a rectangular-cell area average:

```text
g_ij = 1/|C| integral over C_ij |h(s)|^2 ds
```

The implementation estimates this integral by Monte Carlo ray shooting from the
TX and accumulating rays whose intersections with the coverage-map plane fall
inside each cell. Therefore the formal code is not manually placing one point RX
at each grid location. It already uses `cm_size` and `cm_cell_size` to ask Sionna
for a cell-based coverage map. Empty/no-hit cells still occur because the
Monte-Carlo estimator can send no successful path contribution into a given
0.75m x 0.75m cell, especially at larger distance, occluded areas, grazing
angles, or after reflection/diffraction constraints.

Important nuance: a 96m region is smaller than the 112m reference region, but
with the same 128x128 resolution each cell is also smaller:

```text
0.75^2 / 0.875^2 = 0.735
```

So per-cell area is about 26.5% smaller, which reduces expected ray hits per
cell for the same `num_samples`. The smaller crop helps if it removes far/weak
regions, but the smaller cell area does not automatically make no-hit effects
weaker.

Likely practical direction: test formal `valid_crop` with much higher sampling,
e.g. `num_samples=1200000` and `num_runs=5`, and evaluate hit fraction/radial
coverage/cost before changing the formal dataset contract. Interpolation after
collection should be treated as a derived visualization or derived imputation
channel with an explicit valid/no-hit mask, not as the only ground-truth RSS
label.

## 128x128 Cached RSS Visualization Artifact Diagnosis 2026-05-26 CST

User clarified that the problem is observed visually: fresh/direct Sionna RT
visualization at higher quality does not show same-radius light/dark variation,
but visualization from cached 128x128 RSS arrays shows it clearly.

Read-only diagnosis found the likely root in the cached visualization path, not
the formal RF array writer:

```text
formal numeric writer:
  scripts/dynamic_radio_dataset/rf/rss_compute.py
  -> scene.coverage_map(... cm_cell_size=[width/resolution, height/resolution])
  -> writes rss_maps.npz directly, no resize/postprocess

cached review renderer:
  scripts/dynamic_radio_dataset/render/sample.py
  -> dynamic_radio_dataset.render.rss_video --reuse-rss-dir
  -> plot_style=clean, visual_smooth_sigma=0.8, default bilinear interpolation

clean plot preprocessing:
  scripts/dynamic_radio_dataset/render/rss_video.py::prepare_plot_values
  source = finite & nonbuilding & RSS > -200 dBm
  all other nonbuilding cells are nearest-filled from source
  then dBm values are Gaussian-smoothed
```

This means cached 128x128 review videos can visually replace `-270 dBm`/no-hit
cells with nearby finite RSS and then smooth/interpolate the result. That is a
display-only transformation, but it can create or exaggerate speckled/rippled
same-radius brightness patterns in sparse 128 grids. Fresh Sionna RT high-quality
renders do not suffer as strongly because they are not rendering the already
sparse cached 128 grid through this fill/smooth path.

One existing MultiScene20 cached sample was checked:

```text
sample: town01_opt_junction_0143 / episode_000016 / tx_24 / frame 8
source RSS: datasets/DynamicRadioMap/MultiScene20/scenes/town01_opt_junction_0143/episodes/episode_000016/tx_24/rss_maps.npz
raw shape: (100, 128, 128)
nonbuilding cells <= -200 dBm in selected frame: 55.5%
cells used as visual nearest-fill sources (> -200 dBm): 36.0%
95th percentile abs display change from clean preprocessing: ~220 dB
max abs display change from clean preprocessing: ~227 dB
```

Temporary diagnostic artifacts:

```text
tmp/diagnose_128_cached_visual_artifact_20260526/summary.json
tmp/diagnose_128_cached_visual_artifact_20260526/raw_vs_clean_processed.png
```

Interpretation: the immediate issue is very likely the cached review
visualization policy (`visual_fill_threshold_dbm=-200`, nearest fill, Gaussian
smooth, bilinear interpolation) rather than a corrupt 128x128 dataset array. Next
step should be to add a diagnostic/no-fill review style for cached 128 data
(`nearest`, no fill, no smoothing, explicit floor/no-hit color) and compare it
against fresh high-resolution Sionna output before deciding whether any numeric
RF generation change is needed.

Follow-up implementation completed in the same turn after user confirmed the old
visualization can be broken/replaced because it is wrong:

```text
scripts/dynamic_radio_dataset/render/rss_video.py
  - clean/cached rendering no longer nearest-fills no-hit/low-RSS cells by default
  - default interpolation is now nearest, not bilinear
  - --visual-fill is now an explicit legacy opt-in
  - metadata records visual_fill_enabled and interpolation
  - faithful green cached style is tagged style_preset=faithful_cached_128

scripts/dynamic_radio_dataset/render/sample.py
  - canonical GREEN_ABSOLUTE_SAMPLE_STYLE now uses:
      visual_fill = false
      visual_smooth_sigma = 0.0
      interpolation = nearest
  - style_source = cached_rss_green_absolute_valid_crop_faithful_v3

.agent/DATASET_PIPELINE.md
  - review visualization contract updated to forbid nearest-fill/smoothing for
    canonical cached 128x128 review videos
```

Validation:

```text
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.render.rss_video --help
```

All passed.

Smoke render from cached RSS:

```text
tmp/faithful_cached_render_smoke_20260526/episode_000016_tx24_faithful.mp4
tmp/faithful_cached_render_smoke_20260526/rss_heatmap_meta.json
tmp/faithful_cached_render_smoke_20260526/frames/
```

Smoke metadata confirms:

```text
visual_fill_enabled = false
visual_smooth_sigma = 0.0
interpolation = nearest
style_preset = faithful_cached_128
```

Note: `render/rss_video.py --reuse-rss-dir` currently renders all cached frames;
`--end-frame` does not subset cached RSS frames. This is existing behavior and
was not changed in this visualization-artifact fix.

Additional follow-up for user's question about apparently denser samples near TX
and sparse samples far from TX:

Ran direct fresh Sionna RT visualization through
`scripts/dynamic_radio_dataset/render/rss_video.py` without `--reuse-rss-dir` for
the same scene/TX/frame as the cached diagnosis:

```text
scene: town01_opt_junction_0143
episode: episode_000016
tx: tx_24 at (344.2249755859375, 291.6780700683594, 1.5)
frame: 8
rx_region: valid_crop
rx_height: 1.0
max_depth: 3
```

Outputs:

```text
tmp/sionna_fresh_res_compare_20260526/fresh_128_frame8/
tmp/sionna_fresh_res_compare_20260526/fresh_512_frame8/
tmp/sionna_fresh_res_compare_20260526/fresh_512_1200k_frame8/
tmp/sionna_fresh_res_compare_20260526/resolution_sample_density_summary.json
tmp/sionna_fresh_res_compare_20260526/resolution_fresh_vs_cached_contact_sheet.png
```

Key result:

```text
cached 128, 250k samples: hit_gt_-200 fraction = 0.3932
fresh  128, 250k samples: hit_gt_-200 fraction = 0.3932
fresh  512, 250k samples: hit_gt_-200 fraction = 0.1104
fresh  512, 1.2M samples: hit_gt_-200 fraction = 0.2336
```

Radial hit fraction for cached/fresh 128 250k:

```text
0-10m: 0.977
10-20m: 0.837
20-30m: 0.808
30-40m: 0.683
40-50m: 0.312
50-70m: 0.135
70-100m: 0.068
```

Interpretation: the RX grid/cell centers are uniformly spaced, but Sionna
coverage-map RSS is estimated from launched/propagated ray paths. Effective
path hits per cell are not uniform over distance. Farther cells subtend smaller
solid angle from the TX and receive fewer successful path contributions, so they
more often remain at the no-hit floor (`-270 dBm`) unless `num_samples` is
increased substantially. Raising resolution from 128 to 512 increases cell count
16x; with the same 250k samples, per-cell hit density drops, so the high-res map
can look even sparser. Increasing 512 from 250k to 1.2M improves near/mid-range
coverage but still leaves far cells sparse.

Practical implication: faithful 128 visualization should show floor/no-hit cells
rather than fill them. For smoother human review without lying about no-hit
cells, prefer either a high-sample fresh diagnostic render or a separate
confidence/hit-mask-aware visualization, not nearest-fill of the RSS values.

Scattering/diffraction follow-up 2026-05-26 CST:

User asked whether enabling "diffuse reflection" / scattering could mitigate the
coverage-map sparsity. Tested the same scene/TX/frame via direct fresh Sionna RT.

GPU attempts:

```text
128/512 cuda_ad_rgb with --scattering were attempted in parallel.
128 failed in Dr.Jit/OptiX pipeline creation.
512 failed during TensorFlow cuBLAS initialization, likely GPU resource/init
contention.
```

CPU fallback results:

```text
baseline 128 250k:
  reflection=true, diffraction=false, scattering=false, edge_diffraction=false
  hit_gt_-200 fraction = 0.3932
  mean nonbuilding RSS = -183.9 dBm
  finite cells = 5893

scattering only 128 250k CPU:
  reflection=true, diffraction=false, scattering=true, edge_diffraction=false
  hit_gt_-200 fraction = 0.3932
  mean nonbuilding RSS = -183.9 dBm
  finite cells = 5893

scattering + diffraction + edge_diffraction 128 250k CPU:
  reflection=true, diffraction=true, scattering=true, edge_diffraction=true
  hit_gt_-200 fraction = 0.4285
  mean nonbuilding RSS = -177.6 dBm
  finite cells = 6422
```

Radial hit fraction baseline -> scattering-only -> scattering+diffraction+edge:

```text
0-10m:   0.977 -> 0.977 -> 0.980
10-20m:  0.837 -> 0.837 -> 0.903
20-30m:  0.808 -> 0.808 -> 0.901
30-40m:  0.683 -> 0.683 -> 0.717
40-50m:  0.312 -> 0.312 -> 0.364
50-70m:  0.135 -> 0.135 -> 0.155
70-100m: 0.068 -> 0.068 -> 0.072
```

Artifacts:

```text
tmp/sionna_scattering_compare_20260526/fresh_128_250k_scattering_cpu_frame8/
tmp/sionna_scattering_compare_20260526/fresh_128_250k_scattering_diffraction_cpu_frame8/
tmp/sionna_scattering_compare_20260526/scattering_diffraction_summary.json
tmp/sionna_scattering_compare_20260526/scattering_diffraction_contact_sheet.png
```

Interpretation: enabling `--scattering` alone did not mitigate the sparse/no-hit
coverage-map issue for this sample. Adding diffraction and edge diffraction gives
a small improvement, mostly near/mid range, but far-range no-hit sparsity remains
largely unresolved. This is not enough to treat the dataset label issue as fixed.
The more robust path remains explicit valid/no-hit masks and/or much higher
sample budgets / alternative coverage estimation.

Num-runs follow-up 2026-05-26 CST:

User asked to set `num_runs=5` and retry. Ran CPU `llvm_ad_rgb` direct fresh
Sionna RT for the same sample (`town01_opt_junction_0143/episode_000016/tx_24`,
frame 8), resolution 128, num_samples 250000, max_depth 3.

Outputs:

```text
tmp/sionna_num_runs_compare_20260526/baseline_128_250k_runs5_cpu_frame8/
tmp/sionna_num_runs_compare_20260526/scattering_128_250k_runs5_cpu_frame8/
tmp/sionna_num_runs_compare_20260526/scattering_diffraction_edge_128_250k_runs5_cpu_frame8/
tmp/sionna_num_runs_compare_20260526/num_runs5_summary.json
tmp/sionna_num_runs_compare_20260526/num_runs5_contact_sheet.png
```

Key result:

```text
baseline runs=1:
  hit_gt_-200 fraction = 0.3932
  mean nonbuilding RSS = -183.9 dBm
  finite cells = 5893

baseline runs=5:
  hit_gt_-200 fraction = 0.6181
  mean nonbuilding RSS = -136.3 dBm
  finite cells = 9263

scattering runs=5:
  hit_gt_-200 fraction = 0.6183
  mean nonbuilding RSS = -136.2 dBm
  finite cells = 9266

scattering + diffraction + edge_diffraction runs=5:
  hit_gt_-200 fraction = 0.6723
  mean nonbuilding RSS = -126.7 dBm
  finite cells = 10076
```

Radial hit fraction:

```text
             baseline r1  baseline r5  scattering r5  scat+diff+edge r5
0-10m        0.977        0.977        0.977          0.980
10-20m       0.837        0.855        0.860          0.971
20-30m       0.808        0.837        0.843          0.988
30-40m       0.683        0.918        0.918          0.955
40-50m       0.312        0.694        0.694          0.731
50-70m       0.135        0.464        0.462          0.508
70-100m      0.068        0.296        0.294          0.313
```

Interpretation update: `num_runs=5` is much more effective than scattering-only
for reducing no-hit/floor sparsity. Scattering-only still gives essentially no
additional benefit over baseline runs=5. Diffraction/edge diffraction adds a
secondary improvement, especially 10-50 m and modestly at far range. If changing
formal RF settings is considered, prioritize validating `num_runs > 1` cost and
quality first; do not rely on scattering-only as the fix.

## Code Cleanup Pass 2026-05-26 CST

User asked to reduce code and delete definitely unused/temp material. Completed
a conservative cleanup pass without changing dataset contracts or formal CLI
behavior.

Removed generated/temp files:

```text
all __pycache__/ and *.pyc under:
  scripts/dynamic_radio_dataset/
  scripts/drd_research/
  scripts/qa_audit/
  scripts/release_packager/

configs/dynamic_radio/multi_scene_20x150_replacements_20260515.yaml.bak_tx_quality_20260515_1200
configs/dynamic_radio/multi_scene_20x150_resolved.yaml.bak_tx_quality_20260515_1200
```

Removed dead code/imports:

```text
scripts/dynamic_radio_dataset/pipeline/process_rf.py
  removed deprecated collect-dataset subcommand shell, its dedicated parser args,
  and DEFAULT_VEHICLE_ALLOWLIST. The command only raised RuntimeError and formal
  collection already uses scripts/drd.py collect.

scripts/dynamic_radio_dataset/render/rss_video.py
  removed unused xy_to_region_local helper.

scripts/dynamic_radio_dataset/tx/assignment.py
  removed unused selected_tx_ids_for_episode helper.

scripts/dynamic_radio_dataset/geometry/regions.py
  removed unused region_local_xy helper.

scripts/dynamic_radio_dataset/rf/episode_job.py
  removed unused load_tx_catalog helper.

Removed unused imports from:
  carla/collect.py
  sionna/verify.py
  multi_scene/runner.py
  indexing/global_finalize.py
  plans/pilot_selector.py
  routes/route_library.py
  tx/catalog_tools.py
```

I intentionally did not delete `scripts/drd_research/`, `scripts/qa_audit/`, or
`scripts/release_packager/` because the handoff records concrete historical
usage and commands for those standalone tools. I also did not remove the fresh
Sionna compute path in `render/rss_video.py`; docs say it is worth trimming
later, but it is still a standalone command capability and needs an explicit
deprecation/removal decision if we want to break that surface.

Validation after cleanup:

```text
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py") scripts/release_packager/package_training_release.py scripts/qa_audit/audit_dataset.py scripts/qa_audit/schemas.py scripts/qa_audit/utils.py scripts/drd_research/vehicle_rf_audit/vehicle_rf_audit.py
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.pipeline.process_rf --help
python3 scripts/release_packager/package_training_release.py --help

all passed
```

Note: the worktree was already dirty before this cleanup, including large
pre-existing refactor changes, deleted legacy files, and untracked multi-scene
modules/configs. Do not interpret the full `git status` as solely from this
cleanup pass.

## Sionna RT Material Orientation 2026-05-26 CST

User asked how simulation materials are determined for different objects. The
current formal export does not inherit fine-grained CARLA asset materials. Object
radio materials are assigned in `scripts/dynamic_radio_dataset/sionna/export.py`
during CARLA -> Sionna export and written into `scene.xml`.

Default export material arguments:

```text
--vehicle-material  mat-itu_metal
--ground-material   mat-itu_concrete
--building-material mat-itu_concrete
```

`scene.xml` currently defines only:

```text
mat-itu_metal:
  Mitsuba diffuse BSDF
  reflectance = 0.000000 0.001646 0.290089

mat-itu_concrete:
  twosided principled BSDF
  base_color = 0.8 0.8 0.8
  metallic = 0.0
  roughness = 0.25
  specular = 0.5
```

Shape bindings:

```text
road_support_region.ply    -> mat-itu_concrete
static_buildings_proxy.ply -> mat-itu_concrete
car_<actor_id>.ply         -> mat-itu_metal
```

Vehicle geometry can be bbox, catalog mesh, or bbox fallback, but all vehicle
types share `mat-itu_metal` by default. The fusorosa sealed-underbody change is
geometry-only and does not change material. Buildings are currently one concrete
proxy material; there is no glass/wall/roof/tree/pole material split in the
formal dataset export.

## Packaging/Finalization Orientation 2026-05-25 CST

Read-only orientation pass completed for the final dataset processing and
packaging path. No code or dataset artifacts were changed in this pass.

Current MultiScene20 state observed from the runtime tree:

```text
root: datasets/DynamicRadioMap/MultiScene20
active config: configs/dynamic_radio/multi_scene_20x150_resolved.yaml
scene dirs: 20
episodes per scene: 150
qa_report/rf triplets present: 150/150 in each scene
sample dynamic shape: rss_dynamic_dbm = (5, 100, 128, 128)
sample traffic shape: traffic_grid_uint8 = (100, 128, 128)
global index currently present: no indexes/global_episode_index.jsonl
global splits currently present: no indexes/global_splits.json
active DRD/CARLA/Sionna process at check: none
```

Important finalize/package code paths:

```text
scripts/dynamic_radio_dataset/multi_scene/runner.py::finalize_multi_scene
  -> per-scene pipeline.stages.finalize(...)
  -> indexing.global_finalize.finalize_global_index(...)

scripts/dynamic_radio_dataset/pipeline/stages.py::finalize
  -> indexing.finalize.finalize_index(...)
  -> copies per-scene episode_index.jsonl/splits.json into indexes/
  -> writes bucket_summary.json and timing report

scripts/dynamic_radio_dataset/indexing/global_finalize.py
  -> merges per-scene episode_index.jsonl into indexes/global_episode_index.jsonl
  -> writes indexes/global_splits.json, tx_summary.json, global_index_summary.json
```

Next expected non-CARLA gate for MultiScene20 is:

```bash
python3 scripts/drd.py finalize-multi-scene --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
```

The existing standalone release packager is single-scene-oriented:

```text
scripts/release_packager/package_training_release.py
```

It expects a runtime root with top-level `episodes/` and `scene_static/`, derives
one scene-level static-radio bundle from `dynamic_rss_dbm - delta_from_static_db`,
and writes:

```text
dataset_meta.json
episode_index.jsonl
package_report.json
scenes/<scene_id>/...
```

Do not run it directly on `datasets/DynamicRadioMap/MultiScene20` as the final
multi-scene package path. MultiScene20 stores data under
`scenes/<scene_id>/episodes/`, uses 40 candidate TXs per scene, and stores
selected 5 TXs per episode. A correct MultiScene20 training packager must be
scene-aware and handle per-episode selected-TX mappings; static radio should come
from the validated zero-vehicle static cache for the selected TX ids or from a
carefully validated scene-level cache, not from a single first episode's selected
TX slice as if it represented the whole 40-TX catalog.

## MultiScene20 RF Completion / Repair 2026-05-25 11:08 CST

User asked whether collection is complete and requested repair for any missing
data. The main RF wrapper had finished the final scene but exited nonzero because
three historical partial episodes remained. No RF worker is running now.

Main wrapper final error before repair:

```text
RuntimeError: Multi-scene RF failed for 3 scene(s)
summary: datasets/DynamicRadioMap/MultiScene20/indexes/multi_scene_rf_process_summary.json
```

Missing/partial state before repair:

```text
complete episodes: 2997 / 3000
partial episodes: 3
empty episodes: 0
rss_maps.npz files: 14993 / 15000

town04_opt_junction_0148/episode_000029: missing tx_13
town04_opt_junction_0785/episode_000107: missing tx_12, tx_23, tx_21
town05_opt_junction_2086/episode_000138: missing tx_06, tx_11, tx_38
```

Repair action:

```text
Reran dynamic_radio_dataset.pipeline.process_rf process-episode for the three
episodes above using the formal Sionna env and GPU0 only
(`CUDA_VISIBLE_DEVICES=0`, `--use-gpu`, `mitsuba_variant=cuda_ad_rgb`).
```

Post-repair lightweight full-dataset verification:

```text
complete episodes: 3000 / 3000
partial episodes: 0
empty episodes: 0
rss_maps.npz files: 15000 / 15000
missing episode artifacts among qa_report.json, episode_meta.json,
traffic_grid_uint8.npz, rss_dynamic_dbm.npz, rss_delta_from_static_db.npz,
rf_process_meta.json: 0
```

An extra deep read/shape validation over all compressed arrays was started but
stopped because it was very slow after the lightweight check had already
confirmed the repaired artifact set. No RF or validation process was left
running.

## MultiScene20 RF Progress Snapshot 2026-05-24 18:31 CST

User asked for current collection status. CARLA trajectory collection remains
complete; dynamic RF recomputation is still running on GPU0 with one worker and
is now in the final active scene.

Runtime:

```text
launcher pid: 997156
main pid: 997265
active episode: town10_junction_0664/episode_000014
active tx at check: tx_01
log: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.log
```

Artifact-existence progress across active 20 scenes:

```text
accepted trajectories: 3000 / 3000
dynamic RF complete episodes: 2861 / 3000
dynamic RF partial episodes: 4
dynamic RF incomplete/no RF episodes: 135
overall RF completion by complete episode: 95.4%
rss_maps.npz files present: 14317 / 15000 expected selected-TX maps
```

Partial episodes at check time:

```text
town04_opt_junction_0148/episode_000029: 4/5 TX done
town04_opt_junction_0785/episode_000107: 2/5 TX done
town05_opt_junction_2086/episode_000138: 2/5 TX done
town10_junction_0664/episode_000014: active partial, 4/5 TX done at count time
```

Scene complete counts:

```text
19 scenes are at 150/150 except:
town04_opt_junction_0148: 149/150, 1 partial
town04_opt_junction_0785: 149/150, 1 partial
town05_opt_junction_2086: 149/150, 1 partial
town10_junction_0664: 14/150 complete, 1 active partial, 135 empty
```

At recent throughput, the remaining `town10_junction_0664` work is roughly
5-7 hours, plus a small cleanup/finalization/index-validation tail. The three
older partial episodes may need explicit retry/cleanup after the main RF pass if
the wrapper does not revisit them automatically.

## MultiScene20 RF Progress Snapshot 2026-05-22 16:42 CST

User asked for current collection status. CARLA trajectory collection remains
complete; dynamic RF recomputation is still running on GPU0 with one worker.

Runtime:

```text
launcher pid: 997156
main pid: 997265
active episode: town10_junction_0895/episode_000033
active tx at check: tx_31
log: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.log
```

Artifact-existence progress across active 20 scenes:

```text
accepted trajectories: 3000 / 3000
dynamic RF complete episodes: 1685 / 3000
dynamic RF partial episodes: 3
dynamic RF incomplete/no RF episodes: 1312
overall RF completion by complete episode: 56.2%
rss_maps.npz files present: 8432 / 15000 expected selected-TX maps
```

Partial episodes at check time:

```text
town04_opt_junction_0148/episode_000029: 4/5 TX done
town05_opt_junction_2086/episode_000138: 2/5 TX done
town10_junction_0895/episode_000033: active partial
```

Scene complete counts:

```text
town05_opt_junction_0207: 150/150
town01_opt_junction_0143: 150/150
town05_opt_junction_1722: 150/150
town04_opt_junction_0148: 149/150, 1 partial
town04_opt_junction_1249: 150/150
town04_opt_junction_0255: 150/150
town05_opt_junction_0359: 150/150
town05_opt_junction_2086: 149/150, 1 partial
town10_junction_0189: 150/150
town10_junction_0532: 150/150
town10_junction_0719: 150/150
town10_junction_0895: 37/150, active
remaining eight scenes: 0/150
```

Recent throughput since the 2026-05-21 15:20 snapshot is about 23 complete
episodes/hour. At that rate, the remaining RF work is roughly 2.4 days, plus a
small finalization/index-validation tail, assuming no major interruption.

## MultiScene20 Video Sampling 2026-05-21

User asked to visualize existing generated data as videos. Completed a read-only
sample render using cached RSS outputs.

Canonical review videos should use the green absolute style from
`scripts/dynamic_radio_dataset/render/sample.py` (`GREEN_ABSOLUTE_SAMPLE_STYLE`)
instead of the generic clean-style `render/rss_video.py` defaults.

Output:

```text
render root: datasets/DynamicRadioMap/MultiScene20/renders/scene_samples_20260521
manifest:    datasets/DynamicRadioMap/MultiScene20/renders/scene_samples_20260521/sample_manifest.json
video count: 24
scene count: 12 rendered, 8 skipped for now
sampling:    2 episodes per available scene, 1 random tx per episode, seed 20260521
```

The first pass used the generic clean style, then the render was rerun in the
canonical green absolute style from `render/sample.py`. The final output now
matches the review contract and the temporary `_tmp` directory has been cleaned
up.

Skipped scenes had no available `rss_maps.npz` yet:

```text
town04_opt_junction_0053
town04_opt_junction_0785
town04_opt_junction_0850
town04_opt_junction_1197
town04_opt_junction_1593
town05_opt_junction_0396
town05_opt_junction_0562
town10_junction_0664
```

## MultiScene20 RF Progress Snapshot 2026-05-21 15:20 CST

User asked for current collection status. CARLA trajectory collection remains
complete; dynamic RF recomputation is still running on GPU0.

Runtime:

```text
launcher pid: 997156
main pid: 997265
active episode: town05_opt_junction_0359/episode_000135
active tx at check: tx_18
log: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.log
```

Artifact-existence progress across active 20 scenes:

```text
accepted trajectories: 3000 / 3000
dynamic RF complete: 1101 / 3000
dynamic RF incomplete: 1899 / 3000
overall RF completion: 36.7%
failed rf_process_meta count: 1
partials:
  town04_opt_junction_0148/episode_000029: 4/5 TX done, failed meta
  town05_opt_junction_0359/episode_000135: 3/5 TX done at count time
```

Scene complete counts:

```text
town05_opt_junction_0207: 150/150
town01_opt_junction_0143: 150/150
town05_opt_junction_1722: 150/150
town04_opt_junction_0148: 149/150, 1 failed partial
town04_opt_junction_1249: 150/150
town04_opt_junction_0255: 150/150
town05_opt_junction_0359: 137/150, active
town05_opt_junction_2086: 19/150
town10_junction_0189: 13/150
town10_junction_0719: 4/150
town10_junction_0532: 25/150
town10_junction_0895: 4/150
remaining eight scenes: 0/150
```

At the current observed throughput, remaining dynamic RF is roughly 3.5-4 days
plus a small finalization/index-validation tail, assuming no major interruptions.

## MultiScene20 RF Progress / Dynamic-Static RSS Signal 2026-05-20 15:24 CST

User asked for current data collection status and whether the dynamic RSS change
relative to static RSS is large enough to be research-useful.

Current status:

```text
CARLA trajectories: 3000 / 3000 accepted
dynamic RF complete: 579 / 3000
dynamic RF incomplete: 2421 / 3000
active episode: town04_opt_junction_0148/episode_000027
active partial at check: 4/5 TX done, tx_37 running
launcher pid: 997156
main pid: 997265
log: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.log
```

Scene complete counts:

```text
town05_opt_junction_0207 150/150
town01_opt_junction_0143 150/150
town05_opt_junction_1722 150/150
town04_opt_junction_0148 36/150
town04_opt_junction_1249 19/150
town04_opt_junction_0255 4/150
town05_opt_junction_0359 5/150
town05_opt_junction_2086 19/150
town10_junction_0189 13/150
town10_junction_0719 4/150
town10_junction_0532 25/150
town10_junction_0895 4/150
remaining eight scenes 0/150
```

Dynamic-vs-static RSS statistics were computed read-only over 578 complete
episodes. Metric mask:

```text
delta_db = dynamic_rss_dbm - static_rss_dbm
free cells = traffic_grid_uint8 == 0, excluding buildings and vehicle cells
common above-floor cells = free cells where both static and dynamic RSS > -200 dBm
```

Overall completed set:

```text
episodes analyzed: 578
with fusorosa: 452
without fusorosa: 126
raw free cells with delta > 0 dB: 5.03% mean per episode
raw free cells with delta < 0 dB: 6.14% mean per episode
|delta| > 1/3/6/10 dB over free cells: 7.88% / 6.62% / 5.80% / 5.37%
new coverage, static <= -200 and dynamic > -200: 0.92%
lost coverage, static > -200 and dynamic <= -200: 4.07%
common above-floor fraction of free cells: 43.64%
common above-floor |delta| p95/p99 episode mean: 1.94 dB / 9.13 dB
common above-floor |delta| > 3/6/10 dB: 3.81% / 1.89% / 0.90% of common cells
positive common-cell delta mean: +1.88 dB
negative common-cell |delta| mean: 4.02 dB
```

Fusorosa episodes show stronger dynamics:

```text
with fusorosa, |delta| > 3/6/10 dB over free cells: 7.44% / 6.49% / 6.02%
with fusorosa, common above-floor |delta| p95/p99: 2.38 dB / 10.04 dB
without fusorosa, |delta| > 3/6/10 dB over free cells: 3.68% / 3.30% / 3.05%
without fusorosa, common above-floor |delta| p95/p99: 0.36 dB / 5.87 dB
```

By fusorosa count:

```text
0 fusorosa: |delta| > 6 dB free 3.30%, common p95 0.36 dB
1 fusorosa: |delta| > 6 dB free 5.90%, common p95 1.95 dB
2 fusorosa: |delta| > 6 dB free 6.94%, common p95 2.70 dB
3 fusorosa: |delta| > 6 dB free 7.90%, common p95 3.42 dB
```

Interpretation for modeling: the change is sparse but not negligible. The full
map is still dominated by unchanged cells, so plain full-grid MSE on dynamic RSS
alone may underweight the useful signal. Use dynamic-minus-static targets,
traffic-aware masks, and/or weighted losses for `|delta| > 3/6 dB`, new/lost
coverage cells, and near-vehicle shadow regions. With that framing the collected
data has enough research signal; treating all pixels equally would make it look
more weakly dynamic than it is.

RSS value distribution follow-up, sampled read-only over 619 complete episodes
with 30k free-cell samples per episode:

```text
dynamic free RSS range: -270.0 .. -18.36 dBm
static free RSS range: -270.0 .. -20.25 dBm
dynamic free mean including -270 floor: -173.1 dBm
static free mean including -270 floor: -166.3 dBm
dynamic free median including floor: -270.0 dBm
static free median including floor: -270.0 dBm
dynamic free cells > -200 dBm: 44.46%
static free cells > -200 dBm: 47.60%
```

For above-floor free cells only (`RSS > -200 dBm`):

```text
dynamic mean/p50/p90/p95/p99: -52.07 / -52.95 / -44.59 / -42.09 / -35.09 dBm
static mean/p50/p90/p95/p99: -52.19 / -53.51 / -44.88 / -42.53 / -35.43 dBm
```

Interpretation: raw means/medians are dominated by the `-270 dBm` no-coverage
floor; usable non-floor RSS values are mostly in the `-60` to `-40 dBm` band,
with strong-near-TX tails around `-35 dBm` and maxima around `-20 dBm`.

## MultiScene20 RF Progress Snapshot 2026-05-19 19:34 CST

User asked for current collection/progress status after the fusorosa sealed-
underbody restart. CARLA trajectory collection remains complete; current active
stage is GPU0-only dynamic RF recomputation.

Runtime:

```text
launcher pid: 997156
main pid: 997265
active episode: town05_opt_junction_0207/episode_000036
active tx at final check: tx_15
log: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.log
```

Artifact-existence progress snapshot across the active 20 scenes:

```text
accepted trajectories: 3000 / 3000
dynamic RF complete: 162 / 3000
dynamic RF incomplete: 2838 / 3000
episodes with fusorosa: 2793
completed fusorosa episodes after sealed export: 36 / 2793
completed no-fusorosa episodes preserved: 126 / 207
active partial episode at final check: town05_opt_junction_0207/episode_000036, tx_15 in progress
failed rf_process_meta count after restart: 0
```

Scene-level complete counts at the snapshot:

```text
town05_opt_junction_0207: 40/150 complete, episode_000036 active
town01_opt_junction_0143: 9/150 complete
town05_opt_junction_1722: 6/150 complete
town04_opt_junction_0148: 14/150 complete
town04_opt_junction_1249: 19/150 complete
town04_opt_junction_0255: 4/150 complete
town05_opt_junction_0359: 5/150 complete
town05_opt_junction_2086: 19/150 complete
town10_junction_0189: 13/150 complete
town10_junction_0719: 4/150 complete
town10_junction_0532: 25/150 complete
town10_junction_0895: 4/150 complete
remaining eight scenes: 0/150 complete
```

Estimated remaining wall time at the current GPU0 rate:

```text
current scene finish: about 1-2 hours
full RF recomputation + finalize: about 6-8 days
```

## Fusorosa Underbody Seal Production Update 2026-05-19 CST

User requested the simpler production path: keep the existing CARLA traffic and
asset workflow, but make future Sionna exports use a sealed version of the only
large vehicle model currently used in the dataset,
`vehicle.mitsubishi.fusorosa`.

Implemented in `scripts/dynamic_radio_dataset/sionna/export.py`:

```text
affected vehicle_type: vehicle.mitsubishi.fusorosa
scope: Sionna/Mitsuba export mesh only; CARLA runtime, physics, trajectories, and
       source CARLA assets are unchanged
method: preserve the catalog static mesh and append one oriented bbox-footprint
        underbody slab into the same car_<actor_id>.ply
slab height: 0.55 m
slab footprint margin: 0.0 m
per-vehicle manifest field: underbody_seal
episode manifest summary: large_vehicle_underbody_seal_count
```

The diagnostic export/RF test showed this closes most of the high-clearance
leakage while preserving the detailed upper mesh. First formal recompute after
the change verified the production exporter on:

```text
scene/episode: town05_opt_junction_0207/episode_000000
vehicles: 7
fusorosa vehicles sealed: 2
manifest: datasets/DynamicRadioMap/MultiScene20/scenes/town05_opt_junction_0207/episodes/episode_000000/sionna_export/manifest.json
```

Cleanup before restart:

```text
accepted episodes: 3000
episodes with fusorosa: 2793
episodes without fusorosa: 207
remaining RF/Sionna artifacts under fusorosa episodes after cleanup: 0
no-fusorosa completed RF artifacts preserved: 126
```

Only RF/Sionna artifacts were deleted for fusorosa episodes:
`sionna_export`, `tx_*`, `qa_report.json`, `episode_meta.json`,
`traffic_grid_uint8.npz`, `rss_dynamic_dbm.npz`,
`rss_delta_from_static_db.npz`, and `rf_process_meta.json`. Trajectories,
frames, trajectory QA, TX assignments, plans, validation reports, and static RF
cache were kept.

Checks passed after the production code change:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

Dynamic RF was relaunched GPU0-only:

```text
launcher pid: 997156
main pid: 997265
command: python3 scripts/drd.py process-multi-scene-rf --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --use-gpu --gpu-ids 0 --rf-workers 1
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.pid
log: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.log
```

At last check, `episode_000000` completed selected-5 RF with the sealed export:
`tx_10`, `tx_23`, `tx_27`, `tx_35`, and `tx_02` all wrote RSS maps and the
episode logged `scene_qc_pass=True`. The run had advanced to `episode_000001`,
whose export reported `Large vehicle underbody seals: 1`. GPU0 was the visible
RF GPU for this run; GPU1 remained occupied by unrelated jobs.

## Fusorosa Underbody Seal Visualization 2026-05-19 CST

User asked to visualize the sealed-underbody large-vehicle geometry and explain
the sealing logic. Generated temporary SVG/HTML visualization only; no production
dataset/code change.

Outputs:

```text
tmp/episode_000051_tx00_underbody_seal_20260519/visualization/
  fusorosa_underbody_seal_views.html
  fusorosa_underbody_seal_views.svg
  fusorosa_underbody_closeup.svg
  sealed_underbody_rf_heatmap_comparison.html
  sealed_underbody_rf_heatmap_comparison.svg
  sealed_underbody_rf_heatmap_comparison_summary.json
  visualization_summary.json
```

Sealing logic used for the diagnostic mesh:

```text
source mesh: datasets/DynamicRadioMap/Town10/episodes/episode_000051/sionna_export/meshes/car_40.ply
sealed mesh: tmp/episode_000051_tx00_underbody_seal_20260519/sealed_bottom/sionna_export/meshes/car_40.ply
method: preserve original fusorosa mesh, append one low rectangular prism/slab under the existing footprint
added vertices/faces: 8 / 12
slab z range in PLY/world coordinates: 0.015406 -> 0.565406 m
slab footprint: x -50.414434..-47.177156, y -6.960016..2.893261
purpose: close the high-clearance underbody RF path while keeping upper/body mesh detail
```

Visualization style: black outlines are the original fusorosa mesh, red filled
faces are the added underbody sealing slab, and blue dashed outlines show the
slab extent. Environment lacks matplotlib/PIL/rsvg/inkscape, so no PNG was
generated; SVG/HTML are directly browser-viewable and zoomable.

Also generated RF simulation heatmap comparison for
`Town10/episode_000051/tx_00/frame55`:

```text
panel 1: stored original detailed fusorosa mesh RSS
panel 2: sealed-underbody fusorosa mesh CPU RSS
panel 3: full bbox proxy CPU RSS reference
panel 4: sealed - original RSS delta
```

Behind-bus shadow strip metric from the comparison:

```text
original bright cells > -70 dBm: 120/172
sealed-underbody bright cells > -70 dBm: 7/172
full bbox bright cells > -70 dBm: 0/172
```

## Dynamic Minus Static Positive-Area Sample 2026-05-19 CST

User asked for a future statistic: compute dynamic RSS minus static RSS, remove
vehicle positions, then estimate how much area changes positively and how large
the relative change is. A full 100-episode run was completed using the previous
random 100-episode sample. No production data/code was changed.

Temporary outputs:

```text
tmp/dynamic_minus_static_positive_area_sample100_20260519/
  summary.json
  rows.csv

tmp/dynamic_minus_static_positive_area_pilot_20260519/
  summary.json
  summary_above_floor.json
  rows.csv
```

Recommended metric definition:

```text
delta_db = dynamic_rss_dbm - static_rss_dbm
free mask = traffic_grid_uint8 == 0
  excludes building cells (1) and vehicle cells (2), per frame
positive area = delta_db > 0 dB under the free mask
```

Important caveat: raw `delta_db > 0` includes static floor transitions, e.g.
static `-270 dBm` becoming finite dynamic signal. That is a valid "new coverage /
reflection" effect, but it makes positive magnitude tails huge and not suitable
as an ordinary relative-change size. In the 100-episode sample:

```text
raw positive fraction of free cells: 4.97% episode mean
raw positive delta median: 1.45 dB
raw positive delta p95: ~216.5 dB (dominated by floor-to-signal transitions)
```

For the more interpretable above-floor version, require both static and dynamic
RSS to be above `-200 dBm`:

```text
common above-floor cells / free cells: 45.70%
positive common cells / all free cells: 4.01%
positive common cells / common above-floor cells: 8.78%
positive common delta mean: 1.90 dB
positive common delta median: 0.78 dB
positive common delta p95: 7.86 dB
positive common delta p99: 13.08 dB
```

By total vehicle count in the above-floor 100-episode sample:

```text
4 vehicles: n=36, positive/free 3.19%, positive/common 6.85%, mean positive +1.75 dB
5 vehicles: n=21, positive/free 4.08%, positive/common 9.15%, mean positive +1.85 dB
6 vehicles: n=22, positive/free 4.50%, positive/common 10.03%, mean positive +2.05 dB
7 vehicles: n=21, positive/free 4.88%, positive/common 10.55%, mean positive +2.05 dB
```

For final reporting, present both raw floor-inclusive and above-floor metrics.
Use above-floor p50/p95 dB as the main "relative size"; avoid mean linear ratio
because rare high-dB tail cells make it unstable.

## RSS Variation Sample / Fusorosa Underbody Test 2026-05-19 CST

User asked for a random 100-sample RSS variation estimate and a simple test of
whether replacing/sealing high-clearance large-vehicle geometry improves RF
shadowing. No production code or formal dataset files were changed. Temporary
outputs:

```text
tmp/rss_variation_sample_100_20260519/
  summary.json
  sample_rows.csv

tmp/episode_000051_tx00_underbody_seal_20260519/
  sealed_bottom/sionna_export/
  sealed_bottom/tx00_frame55_cpu/
  sealed_bottom/tx00_frame55_cpu_run.log
  analysis/sealed_bottom_region_summary.json
```

Random sample details:

```text
population: 1714 completed MultiScene20 dynamic-RF episodes
sample seed: 20260519
sample: 100 distinct episodes, 500 episode-TX rows
vehicle histogram: 4=36, 5=21, 6=22, 7=21
large-vehicle histogram: 0=9, 1=48, 2=42, 3=1
```

Episode-level metrics are finite-cell-weighted over the 5 selected TX. Overall:

```text
temporal RSS range p50 mean: 3.00 dB
temporal RSS range p95 mean: 17.36 dB
temporal RSS range p99 mean: 28.88 dB
fraction of common cells with temporal range >6 dB: 29.6%
abs(dynamic-static) p95 mean: 1.59 dB
shadow-side min dynamic-static median: about -78.6 dB
reflection-side max dynamic-static median: about +48.3 dB
```

By total vehicle count:

```text
4 vehicles: n=36, temporal p95 mean 16.24 dB, abs delta p95 mean 0.98 dB, >6 dB cells 26.5%
5 vehicles: n=21, temporal p95 mean 17.50 dB, abs delta p95 mean 1.60 dB, >6 dB cells 30.5%
6 vehicles: n=22, temporal p95 mean 18.60 dB, abs delta p95 mean 2.04 dB, >6 dB cells 31.3%
7 vehicles: n=21, temporal p95 mean 17.85 dB, abs delta p95 mean 2.17 dB, >6 dB cells 32.2%
```

By actual large-vehicle count:

```text
0 large: n=9, temporal p95 mean 16.74 dB, abs delta p95 mean 0.43 dB, >6 dB cells 22.0%
1 large: n=48, temporal p95 mean 16.70 dB, abs delta p95 mean 1.35 dB, >6 dB cells 29.1%
2 large: n=42, temporal p95 mean 18.28 dB, abs delta p95 mean 2.09 dB, >6 dB cells 31.6%
3 large: n=1, not enough for stable aggregate
```

Large-vehicle geometry diagnostic reused the known `Town10/episode_000051/tx_00`
frame-55 fusorosa bright-shadow case and added one new CPU run. The new test
copied the export to tmp and appended a low underbody slab to `car_40.ply`
without changing the rest of the bus mesh:

```text
added slab z range: 0.015406 -> 0.565406 m
added vertices/faces: 8 / 12
CPU RF command: llvm_ad_rgb, frame 55 only, 128x128, 250000 samples, no GPU
```

Region result using a behind-bus shadow strip:

```text
stored original mesh:       mean -120.0 dBm, p95 -55.3 dBm, cells > -70 dBm = 120/172
full bbox metal proxy:      mean -270.0 dBm, p95 -270.0 dBm, cells > -70 dBm = 0/172
sealed-underbody mesh:      mean -259.4 dBm, p95 -188.5 dBm, cells > -70 dBm = 7/172
```

Interpretation: the fusorosa bright patch is strongly tied to detailed/high-
clearance mesh geometry. A full bbox proxy gives the cleanest shadow. Simply
sealing the underbody already removes most of the leakage in the downstream
shadow strip while preserving the rest of the detailed mesh. The bus footprint
itself remains reflective/bright for the sealed variant because the original
upper/body mesh is still present; that is less concerning if vehicle-occupied
cells are masked/labelled separately, but it matters if RSS targets include
inside-vehicle cells.

## Dynamic RF Status Check 2026-05-19 11:09 CST

User asked where the dataset collection pipeline currently stands. Read-only
check found the GPU0-only dynamic RF launcher is still active:

```text
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.pid
launcher pid: 2466955
main pid: 2466980
command: python3 scripts/drd.py process-multi-scene-rf --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --use-gpu --gpu-ids 0 --rf-workers 1
current sample: town10_junction_0895 / episode_000073, rss_compute tx_36 on GPU0
log: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.log
```

Pipeline position: route/plan/preflight/CARLA trajectory collection/trajectory
QA/TX assignment/static RF cache are already complete. Dynamic Sionna/RSS RF is
the active stage. Per-TX QA, multi-scene finalization, and episode-TX index
finalization have not been reached for the full dataset.

Fast artifact count (existence/status based, not full NPZ validation):

```text
accepted trajectories = 3000 / 3000
static RF cache = 800 / 800 validated
dynamic RF complete artifacts = 1714 / 3000 (57.13%)
rf_process_meta status counts = processed 1714, failed 9
partial_not_complete_count = 1 (town10_junction_0895/episode_000073 running)
```

Complete/failed by active scene:

```text
town05_opt_junction_0207: 142/150 complete, 8 failed
town01_opt_junction_0143: 150/150 complete
town05_opt_junction_1722: 150/150 complete
town04_opt_junction_0148: 150/150 complete
town04_opt_junction_1249: 150/150 complete
town04_opt_junction_0255: 150/150 complete
town05_opt_junction_0359: 150/150 complete
town05_opt_junction_2086: 150/150 complete
town10_junction_0189: 150/150 complete
town10_junction_0719: 149/150 complete, 1 failed
town10_junction_0532: 150/150 complete
town10_junction_0895: 73/150 complete, episode_000073 running/partial
remaining scenes not started for dynamic RF:
  town04_opt_junction_0053, town04_opt_junction_1197,
  town04_opt_junction_0785, town04_opt_junction_1593,
  town04_opt_junction_0850, town05_opt_junction_0562,
  town05_opt_junction_0396, town10_junction_0664
```

Failed episodes now total 9:

```text
town05_opt_junction_0207/episode_000033
town05_opt_junction_0207/episode_000034
town05_opt_junction_0207/episode_000035
town05_opt_junction_0207/episode_000036
town05_opt_junction_0207/episode_000037
town05_opt_junction_0207/episode_000039
town05_opt_junction_0207/episode_000041
town05_opt_junction_0207/episode_000042
town10_junction_0719/episode_000055
```

The new later failure is `town10_junction_0719/episode_000055`, where
`tx_26` hit a Dr.Jit/OptiX `optixPipelineCreate` failure and the subprocess died
with `SIGABRT`; the run continued afterward and is now processing later scenes.
GPU0 showed the expected large Sionna allocation during `town10_junction_0895`
RF (~47.6 GB at the check); GPU1 had unrelated non-DRD jobs.

Progress since 2026-05-18 15:27 was 1386 -> 1714 complete (+328 in about 19.7 h,
~16-17 episodes/hour). Remaining first-pass work is about 1276 episodes plus the
currently running partial episode. At the recent rate, rough first-pass ETA is
about 3 days, followed by a retry pass for failed/incomplete episodes and then
finalization. Because failed episodes exist, expect the current pass to exit with
a multi-scene RF failure before finalization; rerun the same GPU0-only command
after it exits to retry. Complete episodes should be skipped. Do not edit active
runtime code while this run is active.

## Dynamic RF Status Check 2026-05-18 15:27 CST

User asked for current runtime status. Read-only check found the GPU0-only
dynamic RF launcher is still active and progressing:

```text
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.pid
launcher pid: 2466955
main pid: 2466980
command: python3 scripts/drd.py process-multi-scene-rf --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --use-gpu --gpu-ids 0 --rf-workers 1
current sample: town10_junction_0719 / episode_000043, rss_compute on GPU0
log: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.log
```

GPU status at the check: GPU0 was occupied by Sionna (~47.6 GB used while
`rss_compute` runs). GPU1 had unrelated jobs (~20 GB, 100% util), no DRD/Sionna
process.

Fast artifact count (existence-based, not full NPZ validation):

```text
expected accepted episodes = 3000
complete RF artifacts = 1386 / 3000 (46.20%)
failed rf_process_meta = 8
partial_not_complete_count = 2

complete by scene so far:
  town05_opt_junction_0207: 142/150 complete, 8 failed
  town01_opt_junction_0143: 150/150 complete
  town05_opt_junction_1722: 150/150 complete
  town04_opt_junction_0148: 150/150 complete
  town04_opt_junction_1249: 150/150 complete
  town04_opt_junction_0255: 150/150 complete
  town05_opt_junction_0359: 150/150 complete
  town05_opt_junction_2086: 150/150 complete
  town10_junction_0189: 150/150 complete
  town10_junction_0719: 44/150 complete, current scene in progress
```

The same 8 early failures remain in `town05_opt_junction_0207`:
`episode_000033`, `000034`, `000035`, `000036`, `000037`, `000039`, `000041`,
`000042`. Log counts remain `traceback_count=8`, `oom_count=1`; no new failure
was observed. Recent episodes in `town10_junction_0719` are completing steadily.

Since 2026-05-18 11:29, progress increased 1324 -> 1386 complete (+62 in ~4 h,
~15-16 episodes/hour; the NFS/IO delays noted earlier likely reduced rate).
Remaining incomplete count is about 1614 episodes. At 16-21 episodes/hour, rough
ETA is about 3.2-4.2 days plus a short retry/finalize pass. Do not edit active
runtime code while this run is active.

## Dynamic RF NFS/IO Wait Check 2026-05-18 11:51 CST

User asked why IO waits are long and whether storage is full. Read-only check:

```text
/share1 mount type: nfs4, hard mount, server 192.168.2.236:/share1
df -h /share1: 3.6T total, 2.8T used, 713G available, 80% used
df -i /share1: 244M inodes, 7.9M used, 236M free, 4% used
```

So storage is not full and inode exhaustion is not the issue. The low-VRAM / long
wait intervals were NFS RPC waits: the active `rss_compute` subprocess showed
`State: D (disk sleep)` and `wchan=rpc_wait_bit_killable`. Whole-dataset `du -sh`
was slow enough to hit the command timeout, which is consistent with NFS metadata
latency under load. NFS mount stats showed non-trivial latency, especially WRITE
(avg total execute ~329 ms/op), READ (~31 ms/op), COMMIT (~119 ms/op), and many
metadata ops.

The run continued despite the wait. Example in `town10_junction_0189/episode_000132`:

```text
tx_08 completed 11:28:55
tx_28 completed 11:29:44
tx_32 completed 11:36:55
tx_17 completed 11:43:20
tx_13 completed 11:49:26
then episode_000132 processed OK and episode_000133 started
```

Conclusion for user: this is not due to full storage. It is intermittent NFS/shared
storage latency or server/network contention during scene/mesh/NPZ read/write and
metadata operations. It is slower than earlier typical episodes, but it is making
progress. Do not kill unless no log progress for >20-30 minutes or new errors
appear. No code changes were made.

## Dynamic RF Low-GPU-Memory Observation 2026-05-18 11:43 CST

User asked why GPU memory dropped close to zero. Read-only check found the
GPU0-only dynamic RF launcher was still alive. At the low-memory sample,
`rss_compute` for `town10_junction_0189/episode_000132/tx_17` existed but had no
large CUDA allocation and was in kernel D sleep with `wchan=rpc_wait_bit_killable`:

```text
GPU0 memory: ~1.2-1.5 GB (only small unrelated contexts, no Sionna ~47 GB allocation)
rss_compute state: D/disk sleep, wchan=rpc_wait_bit_killable
output dir tx_17: initially empty
```

This indicates a CPU/filesystem/RPC wait or between-TX transition, not a GPU
ray-tracing phase. The run recovered without intervention: a later check showed
log progress through frames and successful output for `tx_17`:

```text
[OK] Wrote RSS maps: .../town10_junction_0189/episodes/episode_000132/tx_17/rss_maps.npz
[OK] Wrote metadata: .../tx_17/rss_heatmap_meta.json
```

Explanation to user: because each selected TX is a separate `rss_compute`
subprocess, VRAM drops when a TX subprocess exits or while the next subprocess is
loading/parsing scene files or waiting on filesystem I/O before CUDA/Sionna
allocates. Near-zero VRAM is expected in these gaps; it should rise again during
coverage-map computation. Only worry if log/progress stays unchanged for a long
time (e.g. >20-30 min) or errors appear. No code changes were made.

## Dynamic RF Status Check 2026-05-18 11:29 CST

User asked for current runtime status. Read-only check found the GPU0-only
dynamic RF launcher is still active:

```text
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.pid
launcher pid: 2466955
main pid: 2466980
command: python3 scripts/drd.py process-multi-scene-rf --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --use-gpu --gpu-ids 0 --rf-workers 1
current sample: town10_junction_0189 / episode_000132, rss_compute on GPU0
log: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.log
```

GPU status at the check: GPU0 was occupied by Sionna (~47.5 GB used while
`rss_compute` runs). GPU1 had unrelated jobs (`ollama`/python, about 13 GB) but
no DRD/Sionna process.

Fast artifact count (existence-based, not full NPZ validation):

```text
expected accepted episodes = 3000
complete RF artifacts = 1324 / 3000 (44.13%)
failed rf_process_meta = 8
partial_not_complete_count = 3

complete by scene so far:
  town05_opt_junction_0207: 142/150 complete, 8 failed
  town01_opt_junction_0143: 150/150 complete
  town05_opt_junction_1722: 150/150 complete
  town04_opt_junction_0148: 150/150 complete
  town04_opt_junction_1249: 150/150 complete
  town04_opt_junction_0255: 150/150 complete
  town05_opt_junction_0359: 150/150 complete
  town05_opt_junction_2086: 150/150 complete
  town10_junction_0189: 132/150 complete, episode_000132 running/partial
```

The same 8 early failures remain in `town05_opt_junction_0207`:
`episode_000033`, `000034`, `000035`, `000036`, `000037`, `000039`, `000041`,
`000042`. Grepping the log still shows only those early Traceback/OptiX/OOM
errors; recent episodes continue to complete.

Progress since the 2026-05-17 17:35 check was 943 -> 1324 complete, about 381
episodes over ~18 hours (~21 episodes/hour). Remaining incomplete count is about
1676 episodes; at 19-21 episodes/hour, ETA is roughly 3.3-3.7 days plus a short
retry/finalize pass. Because the first scene has failed episodes, the current
full pass is still expected to eventually exit before finalization with a
multi-scene RF failure; rerun the same GPU0-only script/command afterward to
retry failed/incomplete episodes. Complete episodes will be skipped. Do not edit
active runtime code while this run is active.

## Dynamic RF Status Check 2026-05-17 17:35 CST

User asked for current runtime status. Read-only check found the GPU0-only
dynamic RF launcher is still active and continuing normally:

```text
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.pid
launcher pid: 2466955
main pid: 2466980
command: python3 scripts/drd.py process-multi-scene-rf --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --use-gpu --gpu-ids 0 --rf-workers 1
current sample: town05_opt_junction_0359 / episode_000051, rss_compute on GPU0
log: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.log
```

GPU status at the check: GPU0 was occupied by Sionna (~47.4 GB used, expected
near-full allocation while `rss_compute` runs); GPU1 was idle for DRD and showed
only 26 MiB used. No DRD/Sionna process was on GPU1.

Fast artifact count (existence-based, not full NPZ validation):

```text
expected accepted episodes = 3000
complete RF artifacts = 943 / 3000 (31.43%)
failed rf_process_meta = 8
partial_not_complete_count = 3

complete by scene so far:
  town05_opt_junction_0207: 142/150 complete, 8 failed
  town01_opt_junction_0143: 150/150 complete
  town05_opt_junction_1722: 150/150 complete
  town04_opt_junction_0148: 150/150 complete
  town04_opt_junction_1249: 150/150 complete
  town04_opt_junction_0255: 150/150 complete
  town05_opt_junction_0359: 51/150 complete, episode_000051 running/partial
```

The same 8 early failures remain in `town05_opt_junction_0207`:
`episode_000033`, `000034`, `000035`, `000036`, `000037`, `000039`, `000041`,
`000042`. Log grep still shows only those early Traceback/OptiX/OOM errors; no
new error class was observed in the current tail. Recent episodes in
`town05_opt_junction_0359` are completing steadily.

At the current single-GPU rate (~19 episodes/hour), remaining work is roughly
2057 incomplete episodes / 19 per hour = about 108 hours (~4.5 days), plus a
short retry/finalize pass. Because the first scene has failed episodes, the
current full pass is expected to eventually exit before finalization with a
multi-scene RF failure; rerun the same GPU0-only script/command afterward to
retry the failed/incomplete episodes. Complete episodes will be skipped. Do not
edit active runtime code while this run is active.

## Dynamic RF Status Check 2026-05-16 13:18 CST

User asked for current runtime status. Read-only check found the GPU0-only
dynamic RF launcher is still active:

```text
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.pid
launcher pid: 2466955
main command: python3 scripts/drd.py process-multi-scene-rf --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --use-gpu --gpu-ids 0 --rf-workers 1
current scene/episode sample: town05_opt_junction_1722 / episode_000104
```

GPU verification: current `rss_compute` is on GPU0 only; GPU1 has no DRD/Sionna
processes, only unrelated user jobs. GPU0 shows the expected sawtooth/near-full
Sionna allocation while `rss_compute` runs.

Fast artifact count (existence-based, not full NPZ load) showed:

```text
expected accepted episodes = 3000
complete RF artifacts = 396 / 3000 (13.20%)
failed rf_process_meta = 8
per scene:
  town05_opt_junction_0207: 142/150 complete, 8 failed
  town01_opt_junction_0143: 150/150 complete
  town05_opt_junction_1722: 104/150 complete, current episode_000104 running/partial
```

The 8 failed episodes are all early in `town05_opt_junction_0207`:
`episode_000033`, `000034`, `000035`, `000036`, `000037`, `000039`, `000041`,
`000042`. Logs show transient Dr.Jit/OptiX pipeline creation failures and one
CUDA out-of-memory around those failures. The run recovered and continued; no
new failures were seen in the current scene sample. Because `process_multi_scene_rf`
will mark scenes with failed episodes as failed at the end, the current launcher
may stop before finalization after it finishes all scenes. A safe follow-up is to
rerun the same GPU0-only command after this pass exits; complete episodes will be
skipped and the 8 incomplete episodes should be retried. Do not edit runtime code
while this run is active.

Approximate rate since GPU0-only launch: ~383 processed episodes in ~20.5 h,
about 18-19 episodes/hour (~3.2 min/episode). At single-GPU speed, remaining work
is roughly 5.5-6 days, plus a short retry/finalize pass if needed.

## Dynamic RF Switched to GPU0-only 2026-05-15 16:45 CST

User requested stopping the DRD/Sionna job on GPU1 and continuing simulation only
on GPU0. The previous 2-GPU launcher process group was terminated cleanly:

```text
old launcher: bash datasets/DynamicRadioMap/MultiScene20/logs/run_dynamic_rf_quality_filter_2gpu_20260515_161152.sh
old command:  python3 scripts/drd.py process-multi-scene-rf --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --use-gpu --gpu-ids 0,1 --rf-workers 2
```

The stale 2-GPU pidfile was removed. A new detached GPU0-only launcher is active:

```text
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.pid
launcher: datasets/DynamicRadioMap/MultiScene20/logs/run_dynamic_rf_quality_filter_gpu0_latest.sh
log:     datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_gpu0_latest.log
command: CUDA_VISIBLE_DEVICES=0 PYTHONPATH=scripts python3 scripts/drd.py process-multi-scene-rf --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --use-gpu --gpu-ids 0 --rf-workers 1
```

Verification after relaunch:

```text
DRD process tree: one process-multi-scene-rf, one process-episode, one rss_compute
rss_compute CUDA_VISIBLE_DEVICES=0
GPU0: DRD/Sionna active (~44 GB app memory plus unrelated ~3.4 GB python)
GPU1: no DRD/Sionna process; only unrelated python jobs remained
dynamic_complete = 13 / 3000, failed_meta = 0
```

The GPU0-only script keeps the same finalization and 3000-episode completeness
validation steps as the previous 2-GPU script. Already completed episodes are
skipped by `rf_episode_complete`; interrupted incomplete episodes are resumed /
rerun as needed.

## Sionna Per-GPU Peak Difference Note 2026-05-15 16:40 CST

User asked why peak per-process VRAM differs by about 1 GB between the two GPUs.
Read-only sample: GPU0 total 47.6 GB used with unrelated python 3.46 GB and DRD
Sionna app ~44.0 GB; GPU1 total 47.58 GB used with unrelated python jobs
3.67 GB + 0.94 GB and DRD Sionna app ~42.9 GB. Total board usage was almost
the same; the DRD per-process number was lower on GPU1 mostly because GPU1 had
more unrelated memory already occupied. Additional small differences are expected
because GPU0 is RTX A6000 and GPU1 is RTX 5880 Ada, workers process different
episodes/TX at a given instant, and TensorFlow/Mitsuba/DrJit allocators/cache can
grow to different pool sizes depending on current free/fragmented memory. This
does not imply reduced RF fidelity; config still uses the same resolution,
num_samples, and max_depth. No code changes were made.

## Sionna VRAM Sawtooth Explanation 2026-05-15 16:37 CST

User noticed Sionna GPU memory sometimes high and sometimes low, appearing as if
it adapts to currently available memory. Read-only sampling showed this is mainly
process lifecycle, not adaptive placement: GPU1 was about 4.6 GB while only
unrelated Python jobs were present, then jumped to about 47 GB within seconds
when a new `dynamic_radio_dataset.rf.rss_compute` subprocess started. GPU0 stayed
high because its worker already had an active `rss_compute`. The dynamic RF
orchestration has long-lived `process-episode` parents, but launches a separate
Sionna `rss_compute` subprocess per selected TX; when that subprocess exits,
Mitsuba/DrJit/TensorFlow allocations are released, then the next TX subprocess
loads the scene and allocates again. Low-utilization or low-memory intervals also
occur during CPU-side Sionna export, mesh/XML parsing, QA, and compressed NPZ
writes. No code changes were made.

## Sionna RT GPU Memory Analysis 2026-05-15 16:33 CST

User asked whether near-full GPU memory during dynamic Sionna RT is normal and
whether it can be reduced. Read-only check found the 2-GPU dynamic RF launcher
still running with two `process-episode` workers and one active `rss_compute`
subprocess per GPU. Observed progress at the check was `dynamic_complete = 8 /
3000`, `failed_meta = 0`. GPU memory was about 47.5 GB used on each 49 GB card,
with unrelated non-DRD Python jobs also occupying about 3.4 GB on GPU0 and about
4.6 GB total on GPU1. Current RF settings are inherited from
`configs/dynamic_radio/dynamic_radiomap_town10_300.yaml`: `resolution=128`,
`num_samples=250000`, `max_depth=3`, `mitsuba_variant=cuda_ad_rgb`,
`rf_workers=2`. No code changes were made because dynamic RF is live.

Conclusion for the user: this near-full VRAM pattern is expected for
Sionna/Mitsuba/DrJit/TensorFlow GPU coverage-map computation under these
settings and can include allocator/cache reservation. Low instantaneous GPU
utilization while memory remains high is also expected during CPU-side export,
ASCII PLY/XML parsing, JIT/cache setup, and compressed NPZ writes. Memory can be
reduced mainly by using fewer workers/GPUs, lowering RF fidelity
(`num_samples`, `max_depth`, or resolution), or later testing TensorFlow memory
growth / allocator settings after the live run. Do not edit active runtime code
during the live run.

## Code Cleanup Request During Live Dynamic RF 2026-05-15 16:22 CST

User asked to clean stale previous-stage code now that the pipeline is in Sionna
RT dynamic RF. A safety check found dynamic RF is actively running and spawning
new Sionna subprocesses that import code from `scripts/dynamic_radio_dataset/`
from disk for each episode/TX. Current live processes include:

```text
python3 scripts/drd.py process-multi-scene-rf --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --use-gpu --gpu-ids 0,1 --rf-workers 2
/share1/fzj/miniconda3/envs/sionna019/bin/python -m dynamic_radio_dataset.pipeline.process_rf process-episode ...
/share1/fzj/miniconda3/envs/sionna019/bin/python -m dynamic_radio_dataset.rf.rss_compute ...
```

Because new subprocesses import the working tree during the live run, editing or
deleting active modules now could make later episodes use different code than
earlier episodes, harming reproducibility or causing mid-run failures. Therefore
no active runtime code was modified during this check.

Read-only audit performed:

```text
Deleted/stale modules already absent from the worktree:
  scripts/dynamic_radio_dataset/qa/rss_diagnostics.py
  scripts/dynamic_radio_dataset/radio_dataset_utils.py
  scripts/dynamic_radio_dataset/render/dynamic_scene.py
No active code references were found for those modules, except historical docs /
check_contract deny-list entries.
Current dynamic RF progress sample: 4 / 3000 complete, failed_meta=0.
```

Recommended safe cleanup window: pause/stop the dynamic RF launcher (resume is
supported) or wait until completion, then run a focused cleanup with static gates:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

Do not perform broad cleanup of `pipeline/process_rf.py`, `rf/rss_compute.py`,
`sionna/export.py`, `rf/processing.py`, `rf/episode_job.py`, `raster/`,
`geometry/`, `qa/`, `tx/`, or shared utilities while dynamic RF is running.

## MultiScene20 Dynamic RF Launched on 2 GPUs 2026-05-15 16:17 CST

User requested final cleanup and then starting dynamic Sionna RT/RF on two GPUs.
Pre-launch status: no active CARLA/collect/static RF/dynamic RF process; active
dataset had 20 scenes, 3000 accepted trajectories, 800/800 validated static RF
maps, and no completed dynamic RF episodes. GPU1 and GPU0 had small unrelated
Python processes, but the user explicitly requested 2-GPU dynamic RF.

Final organization before launch:

```bash
python3 scripts/drd.py promote-tx-catalogs \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --source candidate_quality_filter
python3 scripts/drd.py repair-multi-scene-metadata \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
```

The promote command hit the shell timeout while rebuilding assignments for the
last scene, but formal catalogs were already the quality-filtered catalogs and
`repair-multi-scene-metadata` completed the remaining assignment repair. Final
pre-dynamic verification passed and was saved to:

```text
datasets/DynamicRadioMap/MultiScene20/indexes/pre_dynamic_rf_validation_latest.json
```

Verification values:

```text
accepted_total = 3000
missing_assignment = 0
invalid_assignment = 0
selected_tx_refs = 15000
static_ok = 800
static_bad_count = 0
rf_complete_before = 0
```

Dynamic RF launched in background with two workers / two GPUs:

```bash
CUDA_VISIBLE_DEVICES=0,1 PYTHONPATH=scripts \
python3 scripts/drd.py process-multi-scene-rf \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --use-gpu --gpu-ids 0,1 --rf-workers 2
```

Runtime handles:

```text
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_2gpu_latest.pid
log:     datasets/DynamicRadioMap/MultiScene20/logs/dynamic_rf_quality_filter_2gpu_latest.log
script:  datasets/DynamicRadioMap/MultiScene20/logs/run_dynamic_rf_quality_filter_2gpu_latest.sh
```

The script will run `process-multi-scene-rf`, then `finalize-multi-scene`, then a
3000-episode completeness validation saved to
`indexes/dynamic_rf_quality_filter_completion_latest.json`. It is resumable by
rerunning the latest script; complete episodes are skipped by `rf_episode_complete`.

Initial monitor ~4.5 minutes after launch:

```text
launcher PID still running
complete dynamic episodes: 1 / 3000
failed rf_process_meta: 0
first processed episode: town05_opt_junction_0207/episode_000001
both GPUs occupied by Sionna subprocesses plus small unrelated Python processes
```

If the run fails due to GPU memory contention on GPU1, inspect the log and rerun
with one GPU or after the unrelated GPU1 process exits.

## MultiScene20 Status Check 2026-05-15 15:05 CST

User asked for current runtime status. Read-only check found replacement CARLA
collection has completed and no active CARLA / collect / static RF / dynamic RF
processes from this pipeline are running. The latest replacement collection
summary is `status=ok` for `configs/dynamic_radio/multi_scene_20x150_replacements_20260515.yaml` with `final_accepted=450` across 3 scenes.

Current active dataset counts from `multi_scene_20x150_resolved.yaml`:

```text
active_scenes = 20
accepted_total = 3000 / 3000
missing_tx_assignment = 0
invalid_tx_assignment = 0
static_rf_cache = 800 / 800, bad_count = 0
```

Replacement scene final counts:

```text
town04_opt_junction_1249: 150/150 accepted, 783 failed attempts
town04_opt_junction_0255: 150/150 accepted, 4 failed attempts
town04_opt_junction_0850: 150/150 accepted, 6 failed attempts
```

GPU/process status at check:

```text
No active collect-multi-scene, CarlaUE4, prepare-rf-cache, rss_compute, or process-multi-scene-rf process from this pipeline.
GPU0: idle except small display memory.
GPU1: occupied by an unrelated `python scripts/run_train.py` process using about 2 GB; not a DRD process.
```

Next recommended step before dynamic RF: run the post-collection formal promotion
/ assignment rebuild gate for `candidate_quality_filter`, then
`repair-multi-scene-metadata`, then start dynamic RF if user authorizes. Static RF
does not need rerun unless TX/reference geometry changes.

## MultiScene20 Static RF Cache Complete for Quality-Filtered TX 2026-05-15 12:30 CST

User approved the quality-filtered TX placements and asked to clean stale data,
then run the static RF stage on one GPU before dynamic RF. This is complete.

Cleanup performed before static RF:

```text
Moved 6 non-active/stale scene dirs out of scenes/:
  town04_opt_junction_0335
  town04_opt_junction_1008
  town04_opt_junction_1061
  town04_opt_junction_1176
  town04_opt_junction_1368
  town04_opt_junction_1452
quarantine dir:
  datasets/DynamicRadioMap/MultiScene20/quarantine/stale_scene_dirs_pre_static_rf_20260515_120150/
```

The reviewed `candidate_quality_filter` catalogs were copied to formal
`scene_static/tx_catalog.json` / `tx_placement_summary.json` for all 20 active
scenes, with backups under per-scene `scene_static/tx_catalog_backups/`. This was
done without deleting episode `tx_assignment.json` because replacement CARLA
collection was still active. Dynamic RF must still wait for collection completion
and a promotion/assignment rebuild pass.

Copy/cleanup report:

```text
datasets/DynamicRadioMap/MultiScene20/indexes/tx_quality_filter_formal_copy_for_static_rf_latest.json
```

Old static cache artifacts for the 73 replaced TX IDs were removed before running
RF so no old-position cache could be reused by mistake. Existing valid static
cache for unchanged TXs was retained.

Static RF was launched on physical GPU 0 only while CARLA collection continued on
GPU 1:

```text
script: datasets/DynamicRadioMap/MultiScene20/logs/run_static_rf_quality_filter_gpu0_latest.sh
log:    datasets/DynamicRadioMap/MultiScene20/logs/static_rf_quality_filter_gpu0_latest.log
pid:    completed; previous pidfile datasets/DynamicRadioMap/MultiScene20/logs/static_rf_quality_filter_gpu0_latest.pid
```

Static RF result:

```text
processed_new_or_replaced_static_tx = 184
skipped_valid_existing_static_tx = 616
failed_tx_count = 0
invalid_existing_tx_count = 0
complete_static_tx = 800 / 800
validation_bad_count = 0
```

Validation report:

```text
datasets/DynamicRadioMap/MultiScene20/indexes/static_rf_quality_filter_validation_latest.json
```

The validation checked all 800 `static_rss_dbm.npy` arrays have shape `(128,128)`,
zero active vehicle objects, and metadata TX coordinates matching the current
formal quality-filtered `tx_catalog.json`.

Current replacement CARLA collection snapshot at static RF completion:

```text
town04_opt_junction_0255: 150/150 accepted
town04_opt_junction_0850: 150/150 accepted
town04_opt_junction_1249: 14/150 accepted, still collecting on GPU 1
```

Next required gate before dynamic RF: wait for `town04_opt_junction_1249` to reach
150 accepted trajectories, then run `promote-tx-catalogs --source
candidate_quality_filter` (even though formal catalogs already match) to delete
and rebuild all accepted-episode selected-5 `tx_assignment.json` files, followed
by `repair-multi-scene-metadata` and a 3000/3000 active-scene verification.

## MultiScene20 TX Quality-Filter Sidecar Generated 2026-05-15 12:00 CST

User approved a partial TX repair policy: keep reasonable existing TXs, replace
only poor-quality TXs, require all TXs to be within a tighter scene-center radius,
and allow TXs to be closer together if needed to keep 40 candidates per scene.
No formal `tx_catalog.json` or episode `tx_assignment.json` was overwritten while
replacement CARLA collection is active.

Config policy was updated in both the full and replacement-only configs, with
backups saved as `*.bak_tx_quality_20260515_1200`:

```yaml
tx.placement.max_distance_to_road_m: 8.0
tx.placement.min_pairwise_tx_distance_m: 5.0
tx.placement.max_distance_to_scene_center_m: 44.0
tx.placement.lane_exclusion.lane_inflation_margin_m: 2.0
```

A sidecar catalog set was generated for all 20 active scenes using source label
`candidate_quality_filter`. It preserves old TX IDs and keeps existing TXs unless
any of these criteria fail:

```text
distance_to_scene_center_m <= 44.0
distance_to_route_centerline_m <= 8.0
distance_to_route_centerline_m >= 2.0
drivable lane exclusion clearance >= 0.5 m
inside valid_crop
outside buildings
```

Artifacts:

```text
report latest: datasets/DynamicRadioMap/MultiScene20/indexes/tx_quality_filtered_partial_replacement_latest.json
report stamped: datasets/DynamicRadioMap/MultiScene20/indexes/tx_quality_filtered_partial_replacement_20260515_115328.json
visualization: datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization_quality_filter_20260515/tx_routes_contact_sheet.html
per-scene sidecars: scene_static/tx_catalog_candidate_quality_filter.json
per-scene summaries: scene_static/tx_placement_summary_candidate_quality_filter.json
```

Verification of the sidecar set:

```text
total_tx = 800
total_replaced_count = 73
quality violations after replacement = 0
inside_drivable_lane_accepted = 0 for every scene
min_drivable_lane_clearance_m = 0.512
max_distance_to_scene_center_m = 43.900
max_distance_to_road_m = 7.974
min_valid_crop_edge_margin_m = 5.0
min_pairwise_tx_distance_m after preserving old good TXs = 3.0
```

Replacement counts for the three new no-building-replacement scenes:

```text
town04_opt_junction_1249: replaced 7, relaxed replacement spacing to 3.0 m
town04_opt_junction_0255: replaced 0
town04_opt_junction_0850: replaced 2, relaxed replacement spacing to 4.0 m
```

Validation passed after config update:

```bash
PYTHONPATH=scripts python3 scripts/drd.py validate-multi-scene --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
PYTHONPATH=scripts python3 scripts/drd.py validate-multi-scene --config configs/dynamic_radio/multi_scene_20x150_replacements_20260515.yaml
```

Next gate: after replacement CARLA collection finishes, inspect the quality-filter
visualization. If accepted, promote with:

```bash
python3 scripts/drd.py promote-tx-catalogs \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --source candidate_quality_filter
python3 scripts/drd.py repair-multi-scene-metadata \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
```

Do not run RF on the old formal catalogs if the user wants this TX cleanup; use
the promoted `candidate_quality_filter` catalogs first.

## MultiScene20 TX Region / Road-Proximity Diagnostic 2026-05-15 11:45 CST

User questioned whether some replacement-scene TX points in the SVG are visually
inside/among the yellow road-lane areas and whether any TXs are outside the data
collection/model region. A read-only diagnostic was run; no formal catalog or
assignment was modified while replacement CARLA collection is active.

Diagnostic report:

```text
datasets/DynamicRadioMap/MultiScene20/indexes/tx_position_region_diagnostic_20260515_1130.json
```

Interpretation used by the diagnostic: `valid_crop` is the strict 96 m x 96 m
RSS/label/model grid region; `support_region` is the 192 m x 192 m visualization
/support crop. Current SVG projection uses the support region and does not draw
the valid_crop border, which can make edge TXs look farther outside than they
are.

Findings over the active 20-scene formal catalogs (800 TX total):

```text
outside_valid_crop_count = 0
outside_support_region_count = 0
inside_saved_drivable_lane_exclusion_count = 0
min_valid_crop_edge_margin_m = 1.5
max_distance_to_center_m = 47.74
max_distance_to_route_centerline_m = 9.94
near_lane_clearance_lt_0.5m_count = 22
would_be_rejected_if_lane_exclusion_inflated_by_1.0m = 78
would_be_rejected_if_lane_exclusion_inflated_by_2.0m = 216
```

So the current TXs are inside the strict valid crop, but the user's visual
concern is real: the current lane-exclusion filter rejects only points inside the
CARLA driving-lane rectangles. Points just outside those rectangles, or in gaps /
medians / shoulders between lane rectangles, can still appear visually inside
the yellow road complex. Replacement-scene examples flagged by a +1 m lane
clearance test include:

```text
town04_opt_junction_1249: tx_35, tx_39, tx_37, tx_36 plus tx_20/34/29/12 near lane
town04_opt_junction_0255: tx_25-37 band along one roadside, plus tx_01 near edge
town04_opt_junction_0850: tx_20, tx_25-37/39 band along one roadside
```

A quick replacement-only feasibility probe (read-only, using saved lane geometry)
shows stricter placement is possible but trades off TX spacing in the simple
straight replacement scenes. Example settings
`max_center=44m`, `valid_crop_margin=4m`, `max_road=8m`, and +1 m lane clearance
still produced 40 candidates for all three replacement scenes, but `0255` needed
about 1.5 m selected spacing and `0850` about 2.0 m. More aggressive +1.5 m lane
clearance failed to find 40 for `0850` without even tighter clustering.

Recommended fix before RF, if the user wants cleaner TXs: regenerate TX catalogs
sidecar-only after collection with an explicit valid-crop margin and drivable-lane
clearance / larger lane inflation, plus reduced `max_distance_to_road_m` (e.g. 8
m). Then inspect refreshed visualizations before promotion. This does not require
recollecting trajectories, only rebuilding selected-5 `tx_assignment.json` after
promotion.

## MultiScene20 No-Building Scene Replacement Collection Running 2026-05-15 11:10 CST

User asked to re-export the Sionna `reference_scene/sionna_export` for the 3
Town04 scenes that failed static RF with missing buildings, confirm whether they
truly have building geometry, and replace/recollect if they do not. This has now
been verified and the replacement-only trajectory collection has been launched.

Re-export verification log:

```text
datasets/DynamicRadioMap/MultiScene20/logs/reexport_failed_town04_sionna_20260515_104333.log
```

After forced re-export with the latest static-scene export code, the original
failed scenes still have no exported building proxy:

```text
town04_opt_junction_1368 building_count=0 mesh=None ply_exists=false
town04_opt_junction_1061 building_count=0 mesh=None ply_exists=false
town04_opt_junction_1176 building_count=0 mesh=None ply_exists=false
```

They remain on disk for provenance but must not be used for RF. Their old exports
were backed up as `reference_scene/sionna_export_bak_no_buildings_20260515_104333`.

Active config replacements in
`configs/dynamic_radio/multi_scene_20x150_resolved.yaml` are now:

```text
town04_opt_junction_1368 -> town04_opt_junction_1249  building_count=27
town04_opt_junction_1061 -> town04_opt_junction_0255  building_count=17
town04_opt_junction_1176 -> town04_opt_junction_0850  building_count=13
```

Replacement-only config:

```text
configs/dynamic_radio/multi_scene_20x150_replacements_20260515.yaml
```

Prepared replacement scenes have valid Sionna building proxies, 40 formal TXs
using `roadside_proxy_lane_exclusion_v1`, and
`inside_drivable_lane_accepted=0` in their TX placement summaries. Validation
passed for both the full resolved config and the replacement-only config.

Replacement-only CARLA trajectory collection was launched in the background with
one CARLA worker on physical GPU 1, leaving GPU 0 free:

```bash
python3 scripts/drd.py collect-multi-scene \
  --config configs/dynamic_radio/multi_scene_20x150_replacements_20260515.yaml \
  --carla-workers 1 \
  --gpu-ids 1 \
  --rpc-ports 2100 \
  --tm-ports 8100 \
  --target-accepted-per-scene 150 \
  --max-carla-restarts-per-scene 1000 \
  --no-rendering-mode
```

Runtime handles:

```text
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/collect_replacement_set2_1gpu_latest.pid
latest log symlink: datasets/DynamicRadioMap/MultiScene20/logs/collect_replacement_set2_1gpu_latest.log
command: datasets/DynamicRadioMap/MultiScene20/logs/collect_replacement_set2_1gpu_latest.command.sh
CARLA worker logs: datasets/DynamicRadioMap/MultiScene20/supervisor_logs/carla_parallel/worker_0/
ports: RPC 2100, TM 8100
```

Replacement TX visualization was refreshed on request at 2026-05-15 11:21 CST
without touching collection or TX assignments:

```text
datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization_replacements_20260515/tx_routes_contact_sheet.html
datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization_replacements_20260515/town04_opt_junction_1249_tx_routes.svg
datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization_replacements_20260515/town04_opt_junction_0255_tx_routes.svg
datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization_replacements_20260515/town04_opt_junction_0850_tx_routes.svg
```

Snapshot shortly after launch (2026-05-15 11:12 CST):

```text
town04_opt_junction_0255 accepted=25/150 failed_attempts=3 (vehicle_vehicle_collision)
town04_opt_junction_0850 accepted=0/150 failed_attempts=0
town04_opt_junction_1249 accepted=0/150 failed_attempts=0
```


Pre-replacement collection summary/plan backups made after launch to preserve the
previous full-run summary before the replacement-only run overwrites latest
indexes:

```text
datasets/DynamicRadioMap/MultiScene20/indexes/multi_scene_collection_summary_pre_no_building_replacement_20260515_1111.json
datasets/DynamicRadioMap/MultiScene20/indexes/multi_scene_collection_plan_pre_no_building_replacement_20260515_1111.json
```

The main nohup log may stay empty while Python output is buffered; use accepted
counts and the worker CARLA logs for live progress. After all three replacement
scenes reach 150 accepted trajectories, run:

```bash
python3 scripts/drd.py repair-multi-scene-metadata \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
```

Then verify the active 20-scene config has 3000 accepted episodes and valid
selected-5 `tx_assignment.json` files before resuming RF. Resume RF with the
full resolved config only; do not use `--allow-missing-buildings` and do not run
RF on the old no-building scenes.

## MultiScene20 RF Run Stopped at Static Check 2026-05-15 10:30 CST

Status check requested by the user on 2026-05-15 10:30 CST found the background
RF pipeline is no longer running. The PID from
`datasets/DynamicRadioMap/MultiScene20/logs/rf_pipeline_gpu1_latest.pid` is stale
and both GPUs are idle except desktop processes. Dynamic RF has not started.

Current artifact state:

```text
static cache complete/valid: 680 / 800 TX maps
dynamic RF complete: 0 / 3000 accepted episodes
validated existing static maps: 680 checked, 0 bad; shape=(128,128), active_vehicle_objects=[]
```

Failed/missing static scenes, each missing all 40 TX static maps:

```text
town04_opt_junction_1368
town04_opt_junction_1061
town04_opt_junction_1176
```

Failure cause: their `reference_scene/sionna_export/manifest.json` has
`static_geometry.building_count = 0`, no `buildings`, and no
`meshes/static_buildings_proxy.ply`. `rss_compute` therefore failed fast for every
TX with `RuntimeError: No exported building geometry found`. This looks like a
stale/bad reference export from the earlier static-scene generation issue, not a
GPU failure. The long script then stopped at its static completeness check
(`expected_static_tx=800`, `present_static_tx=680`, `bad_count=240`) before
launching dynamic RF.

Relevant logs/summaries:

```text
main log: datasets/DynamicRadioMap/MultiScene20/logs/rf_pipeline_gpu1_latest.log
failed summaries:
  datasets/DynamicRadioMap/MultiScene20/scenes/town04_opt_junction_1368/scene_static/rf_static_cache_summary.json
  datasets/DynamicRadioMap/MultiScene20/scenes/town04_opt_junction_1061/scene_static/rf_static_cache_summary.json
  datasets/DynamicRadioMap/MultiScene20/scenes/town04_opt_junction_1176/scene_static/rf_static_cache_summary.json
```

Next recommended action: repair/regenerate the reference Sionna export building
geometry for the 3 failed scenes using the user's latest static-scene export fix,
then rerun the same RF pipeline script. The 680 valid static TX maps should be
skipped by validation; only the 120 missing static maps should be generated before
dynamic RF starts.

## MultiScene20 RF Runtime Estimate 2026-05-14 21:45 CST

Current single-GPU RF runtime estimate after user asked about completion time:

```text
status sample time: ~2026-05-14 21:40 CST
static progress: 155 / 800 TX maps
static observed rate: about 8.8-9.0 TX maps/min
static ETA: roughly 70-75 min remaining, expected around 2026-05-14 22:50-23:00 CST
dynamic progress at sample: 0 / 3000 episodes
```

Dynamic estimate uses prior Town10 RF metadata as the closest measured baseline:
`3 TX x 100 frames` episodes had median ~117 s and mean ~189 s per episode.
MultiScene20 dynamic episodes are `5 TX x 100 frames`; scaling by 5/3 gives
roughly 195-315 s per episode on one worker / one GPU. For 3000 episodes this is
about 162-263 hours after dynamic starts, i.e. approximately 6.8-10.9 days.
Expected completion window if no failures/interruption: around 2026-05-21 evening
to 2026-05-25 late CST; use 2026-05-26 as a conservative buffer. Re-estimate
after the first 10-20 MultiScene20 dynamic episodes finish for a more accurate
scene-specific rate.

## MultiScene20 RF GPU Utilization Check 2026-05-14 21:30 CST

User asked whether GPU memory being nearly full while GPU utilization often reads
0% is normal. Live check while `prepare-rf-cache` was running showed this is
expected for the current static-cache stage:

```text
RF launcher still active: datasets/DynamicRadioMap/MultiScene20/logs/rf_pipeline_gpu1_latest.pid
active stage: prepare-rf-cache
static progress at check: 62 / 800 TX maps
current scene progress: town05_opt_junction_0207 40/40, town01_opt_junction_0143 22/40
GPU confinement: only physical GPU 1 used; GPU 0 remained free except desktop processes
```

A 30s `nvidia-smi` sample showed repeated short-lived Sionna subprocesses with
changing PIDs. During each subprocess, GPU1 memory rose to about 47 GB, often
with instantaneous GPU utilization 0-19%, then dropped to ~25 MiB between TXs,
with occasional 50-100% utilization samples. This matches TensorFlow / Mitsuba /
DrJit allocator behavior plus CPU/IO/JIT/ASCII-PLY scene loading between short
coverage-map kernels. The log continued writing `[OK] Wrote RSS maps` lines, so
this was not a hang.

Operator guidance: treat low instantaneous utilization as normal while progress
continues. Investigate only if no new `[OK] Wrote RSS maps` line and no static
count increase for ~10-20 minutes, or if the log reports errors.

## MultiScene20 RF Pipeline Started 2026-05-14 21:23 CST

RF flow was rechecked after the user's static-scene fix concern. Current verified
state before launch:

```text
CARLA/collect active processes: none observed
formal TX placement_method: roadside_proxy_lane_exclusion_v1 for 20/20 scenes
static cache before launch: 0 / 800 static_rss_dbm.npy
dynamic RF complete before launch: 0 / 3000 accepted episodes
tx_assignment.json: 3000 valid selected-5 assignments
chosen RF GPU: physical GPU 1 only (CUDA_VISIBLE_DEVICES=1)
```

Code/runtime checks performed before launch:

```text
Sionna import on CUDA_VISIBLE_DEVICES=1: passed; TensorFlow saw one GPU; Mitsuba cuda_ad_rgb set; sionna.rt import ok
zero-vehicle static one-TX smoke: passed; active_vehicle_objects=[]; allow_zero_vehicles=true; shape=(1,128,128)
dynamic one-frame smoke with exported vehicles: passed; 4 active vehicle objects; shape=(1,128,128)
static checks: py_compile all dynamic_radio_dataset modules passed; check_contract passed; drd.py help passed
```

Small RF robustness changes made before the long run:

```text
scripts/dynamic_radio_dataset/rf/static_cache.py
  - existing static cache is now skipped only after .npy shape/readability and zero-vehicle metadata validation
  - static .npy writes are atomic to avoid treating a crash-truncated file as complete
  - static Sionna subprocess respects inherited CUDA_VISIBLE_DEVICES; if absent, it uses the first configured sionna.gpu_ids instead of exposing all GPUs

scripts/dynamic_radio_dataset/rf/episode_job.py
  - per-episode sionna_export is now considered reusable only if manifest.json, scene.xml, and motion.jsonl are present/readable/non-empty; incomplete crash leftovers are removed and re-exported

scripts/dynamic_radio_dataset/multi_scene/runner.py and cli.py
  - added process-multi-scene-rf for RF-only multi-scene resume without rerunning CARLA collection
```

Long RF pipeline was launched in the background:

```bash
nohup env GPU_ID=1 bash datasets/DynamicRadioMap/MultiScene20/logs/run_rf_pipeline_gpu1_20260514_212311.sh \
  > datasets/DynamicRadioMap/MultiScene20/logs/rf_pipeline_gpu1_20260514_212311.log 2>&1 &
```

Runtime handles:

```text
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/rf_pipeline_gpu1_latest.pid
latest log symlink: datasets/DynamicRadioMap/MultiScene20/logs/rf_pipeline_gpu1_latest.log
launcher script symlink: datasets/DynamicRadioMap/MultiScene20/logs/run_rf_pipeline_gpu1_latest.sh
current stage at launch handoff: prepare-rf-cache
```

The script runs, in order:

```text
1. prepare-rf-cache for all 20 scenes x 40 TX, zero-vehicle static maps
2. verify all 800 static maps have shape 128x128 and active_vehicle_objects=[]
3. process-multi-scene-rf --use-gpu --gpu-ids 1 --rf-workers 1 for selected-5 dynamic RF
4. finalize-multi-scene
5. verify 3000/3000 accepted episodes are RF-complete
```

Resume guidance if interrupted/rebooted: rerun
`GPU_ID=1 bash datasets/DynamicRadioMap/MultiScene20/logs/run_rf_pipeline_gpu1_latest.sh`
(or the timestamped script). Static cache skips only validated complete TXs;
dynamic RF skips only episodes passing `rf_episode_complete`; incomplete episode
exports are rebuilt. Do not set global `PYTHONNOUSERSITE=1` for the system
`python3` driver because this host's system Python needs its normal site path for
`numpy`; Sionna subprocesses set `PYTHONNOUSERSITE=1` internally.





## MultiScene20 TX Promotion / Metadata Repair Complete 2026-05-14 21:06 CST

Completed the deferred post-collection TX merge after confirming no active
CARLA/collect processes and closed RPC/TM ports. This resolves the previously
interrupted TX promotion state.

Commands run:

```bash
python3 scripts/drd.py promote-tx-catalogs \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --source candidate_lane_exclusion
python3 scripts/drd.py repair-multi-scene-metadata --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
```

Results:

```text
promoted_scene_count: 20
formal tx_catalog placement_method: roadside_proxy_lane_exclusion_v1 for 20/20 scenes
formal tx candidates per scene: 40
inside_drivable_lane_accepted: 0 for every formal summary
accepted episodes: 3000
valid selected-5 tx_assignment.json: 3000
missing/invalid tx_assignment.json: 0
per-scene selected TX usage range: 18-19 uses per tx, 750 selected-TX refs per scene
metadata repair totals: validation_report_missing_after=0, tx_assignment_missing_after=0
```

Artifacts:

```text
datasets/DynamicRadioMap/MultiScene20/indexes/tx_catalog_promotion_20260514_205429.json
datasets/DynamicRadioMap/MultiScene20/indexes/tx_catalog_promotion_latest.json
datasets/DynamicRadioMap/MultiScene20/indexes/metadata_repair_latest.json
datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization/tx_routes_contact_sheet.html
datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization/tx_visualization_summary.json
```

Formal TX visualization was refreshed from the promoted formal catalogs. The old
lane-exclusion review directory still exists under
`indexes/tx_visualization_lane_exclusion/`, but the default
`indexes/tx_visualization/` now also reflects the promoted lane-exclusion TXs.

Validation passed after promotion/repair:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

No Sionna/RF/static cache was run. Next stage, only after user authorization, is
to prepare zero-vehicle static RSS cache for all 40 candidates per scene and then
run selected-5 dynamic RF.

## MultiScene20 CARLA Lifecycle Fix Completed / Collection Target Reached 2026-05-14 CST

Implemented the CARLA attempt-boundary lifecycle fix requested by the user and
ran the 2-worker recovery collection (one worker per GPU) to completion. Current
state after monitoring:

```text
active CARLA/collect processes: none
active RPC/TM ports 2100/2110/8100/8110: none listening
latest collection status: ok
accepted_total: 3000 / 3000
completed_scenes: 20 / 20
workers: 2
gpu_ids: [0, 1]
latest successful summary: datasets/DynamicRadioMap/MultiScene20/indexes/multi_scene_collection_summary.json
latest log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_latest.log
full recovery log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_2workers_lifecyclefix_resume_20260513_234107.log
```

Code changes for the root-cause fix:

```text
scripts/dynamic_radio_dataset/carla/collect.py
  - no-rendering/no-video attempts no longer skip final actor destruction
  - cleanup now writes cleanup_completed or cleanup_skipped_server_unavailable
  - removed same-town force_clean_slate reload; same town now logs Reusing loaded town
  - CarlaSyncContext exits no-rendering collection with synchronous_mode=false,
    fixed_delta_seconds=None, no_rendering_mode=true instead of restoring an
    older rendering-enabled setting

scripts/dynamic_radio_dataset/multi_scene/carla_server.py
  - appends DRD_CARLA_START markers to worker CARLA stdout/stderr on every
    server launch
  - crash-signature summary now includes counts since the latest start marker,
    so old Signal 11 history is not mistaken for current crashes
```

Validation / runtime evidence:

```text
static checks passed:
  PYTHONPATH=scripts python3 -m py_compile scripts/dynamic_radio_dataset/carla/collect.py scripts/dynamic_radio_dataset/multi_scene/carla_server.py scripts/dynamic_radio_dataset/multi_scene/carla_parallel.py
  PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
  PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
  python3 scripts/drd.py --help

runtime checks:
  latest recent attempts show collection_stage=cleanup_completed
  latest recent attempt stdout shows Reusing loaded town
  latest recent attempt stdout does not show reason=force_clean_slate
  latest recent attempt stdout does not show Skipping final actor destruction
  worker 0 signature_counts_since_last_start: all zero
  worker 1 signature_counts_since_last_start: all zero

trajectory integrity read-only check 2026-05-14:
  scene_count=20
  episode_dirs_total=3000
  accepted_eps_by_qa=3000
  per-scene accepted=150 for all scenes
  episode IDs are contiguous episode_000000..episode_000149 in every scene
  required trajectory files present/readable for accepted episodes
  actor_states.jsonl frame_count=100 for accepted episodes, matching config
    episode_duration_s=10 and fps=10
```

The successful long recovery run started at `2026-05-13 23:41 CST` and exited at
`2026-05-14 06:33 CST` with `final_accepted=3000`. A later user-requested
2-worker resume was started at `2026-05-14 15:45 CST`; because the target had
already been reached, it did not launch CARLA servers and exited successfully at
`2026-05-14 15:54 CST` with `initial_accepted=3000`, `final_accepted=3000`, and
zero new crash signatures since the latest CARLA start markers.

Important next step before Sionna/RF: the earlier interrupted TX promotion left
formal TX catalogs/assignments in a mixed state. Now that CARLA collection is no
longer active, do not run RF until the intended TX catalog state is resolved. If
the lane-exclusion TX review is accepted, run:

```bash
python3 scripts/drd.py promote-tx-catalogs \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --source candidate_lane_exclusion
python3 scripts/drd.py repair-multi-scene-metadata --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
```

Current read-only TX/assignment check after collection:

```text
formal tx_catalog.json placement_method counts:
  roadside_proxy_lane_exclusion_v1: 19
  roadside_proxy: 1 (town10_junction_0664)
accepted trajectory-QA-passing episodes: 3000
valid selected-5 tx_assignment.json: 2999
missing tx_assignment.json: 0
invalid tx_assignment.json: 1
  datasets/DynamicRadioMap/MultiScene20/scenes/town05_opt_junction_0562/episodes/episode_000098/tx_assignment.json (JSONDecodeError)
```

Then validate all 20 formal catalogs and all 3000 accepted episodes have valid
selected-5 `tx_assignment.json` before RF.



## MultiScene20 Replacement CARLA Collection Status 2026-05-15 11:35 CST

User restarted CARLA collection for three replacement Town04 scenes with:

```text
config: configs/dynamic_radio/multi_scene_20x150_replacements_20260515.yaml
command: collect-multi-scene --carla-workers 1 --gpu-ids 1 --rpc-ports 2100 --tm-ports 8100 --target-accepted-per-scene 150 --no-rendering-mode
active pid: 1961680
active CARLA: pid 1961792, port 2100, graphicsadapter=1
```

Observed status at 11:35 CST:

```text
town04_opt_junction_0255: 142 accepted / 146 attempts, currently collecting; target 150
town04_opt_junction_0850: 0 accepted / 0 attempts, queued
town04_opt_junction_1249: 0 accepted / 0 attempts, queued
latest CARLA start marker 2026-05-15T11:07:14 has 0 Signal 11 / 0 segfault / 0 RenderThread timeout after marker
```

GPU interpretation:

- Only one CARLA worker was launched, so only one CARLA server should use GPU memory.
- `dynamic_radio_dataset.carla.collect` Python subprocess is CPU/RPC-side and normally does not allocate GPU memory.
- `nvidia-smi` shows CARLA pid 1961792 using about 2.6 GiB on GPU1; the tiny GPU0 entry is graphics bookkeeping/Xorg-level context, not the active CARLA workload.
- The empty `collect_replacement_set2_1gpu_20260515_110713.log` is not itself a failure; current progress is visible from scene attempts and process tree.

## MultiScene20 TX Promotion Interrupted / Collection Active 2026-05-13 19:14 CST

User restarted CARLA collection while TX catalog promotion was being attempted,
and the promotion command was also interrupted. Current state is intentionally
recorded so the next agent does not assume TX promotion is complete.

Read-only checks after the interruption:

```text
active collection: yes
launcher pidfile: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_latest.pid -> 1337283
current command: collect-multi-scene, 2 workers, rpc 2100/2110, tm 8100/8110
current CARLA server observed: RPC 2110 listening
accepted_total: 1885 / 3000
completed_scenes: 10 / 20
```

Do not run `promote-tx-catalogs`, `repair-multi-scene-metadata`, or any command
that deletes/rebuilds `tx_assignment.json` while this collection remains active.
Trajectory collection itself is still usable because CARLA trajectory collection
does not depend on TX coordinates for actor motion, but the dataset is not RF-ready
until metadata/TX state is repaired.

Partial TX state caused by the interrupted promotion:

```text
formal tx_catalog.json placement_method counts:
  roadside_proxy_lane_exclusion_v1: 19 scenes
  roadside_proxy: 1 scene (town10_junction_0664)
accepted trajectory-like episodes: 1885
tx_assignment.json files present: 1693
missing tx_assignment.json: 192
invalid tx_assignment.json: 1
  datasets/DynamicRadioMap/MultiScene20/scenes/town05_opt_junction_0562/episodes/episode_000098/tx_assignment.json
scenes with assignment mismatch:
  town05_opt_junction_0396: accepted 150, assignments 9
  town05_opt_junction_0562: accepted 150, assignments 99
```

After collection is completed or explicitly stopped, repair in this order:

```bash
# confirm no collect/CARLA process and ports 2100/2110/8100/8110 are idle
python3 scripts/drd.py promote-tx-catalogs \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --source candidate_lane_exclusion
python3 scripts/drd.py repair-multi-scene-metadata --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
```

Then verify all 20 formal TX catalogs use `roadside_proxy_lane_exclusion_v1`, all
trajectory-QA accepted episodes have valid selected-5 `tx_assignment.json`, and
only then proceed to Sionna/RF.


## MultiScene20 CARLA Relaunch / Crash Analysis 2026-05-13 19:40 CST

User observed repeated CARLA starts/exits. Current state after inspection:

```text
active launcher: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_latest.pid
latest log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_latest.log
command: collect-multi-scene, 2 workers, RPC 2100/2110, TM 8100/8110, --no-rendering-mode
accepted_total: 1885 / 3000
completed_scenes: 10 / 20
```

Important diagnosis:

- GPU utilization/temperature is no longer used to block CARLA startup for no-rendering collection.
- `-nullrhi` was tested directly and crashes immediately in this packaged CARLA build (`Signal 11`). Do not use NullRHI here.
- Current launcher uses `-RenderOffScreen` plus `VK_ICD_FILENAMES=/etc/vulkan/icd.d/nvidia_icd.json`, while the CARLA world still enables `no_rendering_mode` for trajectory-only collection.
- CARLA server exits/restarts are still happening around attempt boundaries, but the supervisor continues and new attempts are being created. This is not caused by the old GPU-health gate.
- The newest attempts observed were under `town04_opt_junction_1197` and `town04_opt_junction_0785`; many are trajectory-QA failures due `vehicle_vehicle_collision`, so accepted count may not increase quickly even while CARLA is running attempts.

Code changes made during this analysis:

```text
scripts/dynamic_radio_dataset/multi_scene/carla_server.py
  - no longer defaults no_rendering_mode to CARLA -nullrhi
  - sanitizes DISPLAY/X11 and GPU steering env for CARLA subprocesses
  - sets NVIDIA Vulkan ICD when available

scripts/dynamic_radio_dataset/multi_scene/carla_parallel.py
  - no-rendering collection no longer uses GPU health gate by default
  - no-rendering collection no longer defaults to NullRHI
```

Validation passed after code changes:

```bash
PYTHONPATH=scripts python3 -m py_compile scripts/dynamic_radio_dataset/multi_scene/carla_server.py scripts/dynamic_radio_dataset/multi_scene/carla_parallel.py
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

Next operator guidance:

- Do not interpret `nvidia-smi` 100% GPU utilization alone as a reason to stop collection.
- If accepted count stalls, inspect recent attempt `trajectory_qa.json` first; current stall is largely collision QA failures in some scenes, not only CARLA startup failure.
- If system/admin complaints recur, reduce to one worker or replace high-collision scenes; do not re-enable NullRHI.

## Town10_v1 Release Static Radio Hotfix 2026-05-13 18:52 CST

User asked to update only
`datasets/DynamicRadioMapRelease/Town10_v1/scenes/town10_junction_0189` for model
debugging. Completed without touching runtime `datasets/DynamicRadioMap/Town10`,
dynamic episode RSS files, or other datasets.

Actions:

```text
ran zero-vehicle Sionna static sidecar for tx_01 and tx_02
reused existing zero-vehicle sidecar for tx_00
replaced release scene-level static file:
  datasets/DynamicRadioMapRelease/Town10_v1/scenes/town10_junction_0189/tx_static_radio/tx_static_radio_maps.npz
replaced release metadata:
  datasets/DynamicRadioMapRelease/Town10_v1/scenes/town10_junction_0189/tx_static_radio/tx_static_radio_meta.json
old release static npz/meta were deleted/replaced per user request; no backup copy was kept
```

New metadata:

```text
source: zero_vehicle_sionna_rt_sidecar
active_vehicle_objects: []
shape: [3, 128, 128]
tx_ids: [tx_00, tx_01, tx_02]
```

Validation:

```text
release static max_abs_diff_vs_sidecar:
  tx_00: 0.0
  tx_01: 0.0
  tx_02: 0.0
episode_000080 frame 95 has minimum vehicle cells in that episode: 26
MAE(dynamic - updated static), valid nonbuilding:
  tx_00: 4.33 dB, p95_abs=0.09 dB
  tx_01: 5.73 dB, p95_abs=0.20 dB
  tx_02: 2.79 dB, p95_abs=0.00 dB
```

Updated review images:

```text
runs/static_prior_visual_check_clean/tx00_UPDATED_release_static_vs_dynamic_ep000080_frame095_full_range_minus270_minus20.png
runs/static_prior_visual_check_clean/tx00_UPDATED_release_static_vs_dynamic_ep000080_frame095_display_range_minus115_minus35.png
```

## Town10 Clean Static RSS Sidecar 2026-05-13 CST

User asked to rerun Sionna RT for the erroneous Town10 static-prior case without
touching dynamic scene artifacts. Ran a sidecar-only static RSS computation for
`tx_00` using the existing Town10 reference export and explicitly excluding all
vehicle objects:

```text
export: datasets/DynamicRadioMap/Town10/reference_scene/sionna_export
tx: tx_00 at (-32.76010513305664, 7.430440902709961, 1.5)
output: runs/static_prior_visual_check_clean/tx_00_zero_vehicle_static/
Sionna variant: cuda_ad_rgb on CUDA_VISIBLE_DEVICES=1
active_vehicle_objects: []
```

Generated review images:

```text
runs/static_prior_visual_check_clean/tx00_clean_static_zero_vehicle_full_range_minus270_minus20.png
runs/static_prior_visual_check_clean/tx00_clean_static_zero_vehicle_display_range_minus115_minus35.png
runs/static_prior_visual_check_clean/tx00_old_static_vs_clean_static_vs_dynamic_ep000080_frame095_full_range_minus270_minus20.png
runs/static_prior_visual_check_clean/tx00_old_static_vs_clean_static_vs_dynamic_ep000080_frame095_display_range_minus115_minus35.png
runs/static_prior_visual_check_clean/manifest.json
```

Key valid-nonbuilding stats for `episode_000080`, frame 95, `tx_00`:

```text
old stale static mean: -208.83 dBm
clean zero-vehicle static mean: -124.84 dBm
dynamic frame mean: -128.41 dBm
MAE(dynamic - old static): 88.93 dB
MAE(dynamic - clean static): 4.33 dB
```

This confirms the prior visual mismatch was caused by stale/incorrect static
cache, not dynamic RSS or visualization interpolation. No runtime
`scene_static/tx_00/static_rss_dbm.npy`, dynamic episode files, release files, or
RF cache were overwritten.

## Town10 Release Static Prior Issue 2026-05-13 CST

Investigated the reported side-by-side static-prior vs dynamic RSS mismatch for
`datasets/DynamicRadioMapRelease/Town10_v1`, episode `episode_000080`, frame 95,
TX `tx_00`.

Findings:

```text
release tx_static_radio_dbm exactly matches runtime:
  datasets/DynamicRadioMap/Town10/scene_static/tx_00/static_rss_dbm.npy
release static source:
  derived_from_runtime_dynamic_minus_delta, source_episode_id=episode_000000
dynamic episode_000080 release RSS exactly matches runtime RSS
rss_delta_from_static_db equals dynamic_rss_dbm - static_rss_dbm
```

So the packager did not scramble arrays. The semantic bug is older: the runtime
static RSS cache itself was not a clean zero-vehicle static prior. The static
cache metadata shows:

```text
datasets/DynamicRadioMap/Town10/scene_static/tx_00/rss_heatmap_meta.json
  source_export_dir: datasets/single_scene_tx_demo_v1/reference_scene/sionna_export
  active_vehicle_objects: [car_47, car_48, car_49, car_50]
```

That stale/static cache was generated on 2026-04-23 from an old reference export
with active reference vehicles. As a result, later dynamic RSS/delta artifacts are
internally self-consistent but the release `tx_static_radio_dbm` is not a clean
no-vehicle static prior. This explains the large visual/numeric gap such as
static valid mean around -209 dBm vs dynamic frame mean around -128 dBm.

Code guard/fix applied:

```text
scripts/dynamic_radio_dataset/rf/rss_compute.py
  - supports exclude-vehicles=all or * to select zero vehicles when allow_zero_vehicles is set
scripts/dynamic_radio_dataset/rf/static_cache.py
  - prepare-rf-cache now passes --exclude-vehicles all for static cache generation
  - static cache generation fails if metadata reports active vehicle objects
scripts/release_packager/package_training_release.py
  - release packaging now rejects runtime static caches with non-empty active_vehicle_objects
```

Validation:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py") scripts/release_packager/package_training_release.py
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

Direct guard check now fails the current stale Town10 runtime as expected:

```text
static RSS cache for tx_00 contains active vehicle objects ['car_47', 'car_48', 'car_49', 'car_50'];
regenerate static cache with prepare-rf-cache --force before packaging
```

No Sionna/RF regeneration was run during this investigation. To make
`Town10_v1` usable as a clean static-prior release, regenerate the runtime static
cache with `prepare-rf-cache --force`, then rerun affected per-episode RF/delta
or rebuild a new release after deciding whether to keep the old RF artifacts.

## TX Lane-Exclusion Sidecar Implementation 2026-05-13 17:52 CST

Implemented the TX roadside correction as a sidecar-first, human-review workflow.
No formal `tx_catalog.json` files were promoted and no episode
`tx_assignment.json` files were deleted or rebuilt.

Code changes:

```text
scripts/dynamic_radio_dataset/tx/lane_exclusion.py
  - new CARLA/OpenDRIVE driving-lane exclusion builder
  - approximates nearby driving-lane waypoints as inflated oriented rectangles
scripts/dynamic_radio_dataset/tx/placement.py
  - supports requested method roadside_proxy_lane_exclusion_v1
  - preserves old roadside_proxy as default/fallback
  - writes explicit fallback warnings if lane geometry cannot be generated
scripts/dynamic_radio_dataset/tx/catalog_tools.py
  - sidecar regeneration, promotion helper, process guard, SVG/contact-sheet rendering
scripts/dynamic_radio_dataset/cli.py
  - added regenerate-tx-catalogs and promote-tx-catalogs
```

Full sidecar dry-run was run:

```bash
python3 scripts/drd.py regenerate-tx-catalogs \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --placement-method roadside_proxy_lane_exclusion_v1 \
  --sidecar
```

Result:

```text
scene_count: 20
candidate_count: 40 for every scene
inside_drivable_lane_accepted: 0 for every scene
selected_tx_per_episode assignments on disk: still 5, not modified
formal catalogs: still placement_method=roadside_proxy until promotion
```

Artifacts for review:

```text
datasets/DynamicRadioMap/MultiScene20/indexes/tx_catalog_regeneration_candidate_lane_exclusion_latest.json
datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization_lane_exclusion/tx_routes_contact_sheet.html
datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization_lane_exclusion/town05_opt_junction_2086_tx_routes.svg
datasets/DynamicRadioMap/MultiScene20/scenes/<scene_id>/scene_static/tx_catalog_candidate_lane_exclusion.json
datasets/DynamicRadioMap/MultiScene20/scenes/<scene_id>/scene_static/tx_placement_summary_candidate_lane_exclusion.json
datasets/DynamicRadioMap/MultiScene20/scenes/<scene_id>/scene_static/tx_drivable_exclusion.json
```

Important notes:

- Promotion was intentionally not run; human SVG review should happen first.
- `promote-tx-catalogs --source candidate_lane_exclusion` backs up old formal
  catalogs and rebuilds selected-5 assignments, and refuses to run while an
  active `collect-multi-scene` process is detected unless explicitly overridden.
- A process/port check during this turn found no active `collect-multi-scene`,
  `dynamic_radio_dataset.carla.collect`, or CARLA process and RPC/TM ports were
  closed. The previous latest pidfile was stale.
- Scenes `town04_opt_junction_1061` and `town04_opt_junction_1176` needed a
  second pairwise-spacing relaxation to 2.0m; `town05_opt_junction_0359`,
  `town10_junction_0895`, and `town04_opt_junction_1593` relaxed to 3.0m. These
  warnings are visible in the candidate summaries and contact sheet.

Validation passed:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

## MultiScene20 CARLA Root-Cause Fix 2026-05-13 17:35 CST

Implemented code fix and stopped the stale old launcher; did not restart
collection. Root cause of the stalled 2-worker run was not missing trajectory
code or Sionna. The current
trajectory-only command used `--no-rendering-mode`, but the CARLA server launcher
still used `CarlaUE4.sh -RenderOffScreen`, so each server start still depended on
the UE4 RenderThread/GPU path. This explains both symptoms:

```text
GPU too busy/hot for CARLA start ...
Signal 11 / RenderThread timeout in carla_stderr.log
```

Code changes:

```text
scripts/dynamic_radio_dataset/multi_scene/carla_parallel.py
  - multi-scene no-rendering workers now default supervisor.use_nullrhi_for_no_rendering=true
  - GPU start health gate defaults off when the worker is expected to use -nullrhi

scripts/dynamic_radio_dataset/multi_scene/carla_server.py
  - _use_nullrhi now treats carla.no_rendering_mode=true as the default reason to launch -nullrhi
    unless supervisor.use_nullrhi/use_nullrhi_for_no_rendering explicitly overrides it
```

Validation passed:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
python3 scripts/drd.py collect-multi-scene --help
```

A no-start Python check confirmed the runtime config resolves to:

```text
no_rendering_mode=True
use_nullrhi_for_no_rendering=True
gpu_start_health_check=False
resolved_use_nullrhi=True
```

The stale launcher process group was terminated after the code fix:

```text
terminated process group: 807097
stopped old orchestrator: 807537
post-cleanup CARLA/collect processes: none found
post-cleanup ports 2100/2110/8100/8110: closed
```

Before resuming collection, relaunch with the same 2-worker command if desired.
The new worker runtime config should now use `-nullrhi` for trajectory-only
collection. Do not start another collection until the user explicitly asks.

## MultiScene20 CARLA Status Check 2026-05-13 17:19 CST

Read-only check only; did not restart or kill anything. The current
2-worker collection command is still alive, but it is not effectively collecting
at the check time:

```text
orchestrator pid: 807537
launcher pid: 807097
command: python3 scripts/drd.py collect-multi-scene ... --carla-workers 2 --gpu-ids 0,1 --rpc-ports 2100,2110 --tm-ports 8100,8110 --no-rendering-mode
active CARLA RPC/TM ports: none; 2100/2110/8100/8110 are closed
CARLA process state: one defunct CarlaUE4.sh child under worker 807597
effective collection throughput: zero at check time
```

Current saved trajectory count is unchanged from the latest repair summary:

```text
accepted_total: 1885 / 3000
completed_scenes: 10 / 20
unfinished:
  town01_opt_junction_0143 41/150
  town04_opt_junction_0148 25/150
  town04_opt_junction_0785 11/150
  town04_opt_junction_1197 21/150
  town04_opt_junction_1368 51/150
  town05_opt_junction_0359 83/150
  town05_opt_junction_1722 40/150
  town05_opt_junction_2086 56/150
  town10_junction_0189 31/150
  town10_junction_0719 26/150
```

Latest collection log is
`datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_latest.log`
and is a symlink to
`collect_multi_scene_2workers_autorestart_20260513_165147.log`. It last showed
GPU start gating warnings such as "GPU too busy/hot for CARLA start". At the
check, `nvidia-smi` showed high memory usage on both GPUs but 0% instantaneous
utilization; the active GPU memory users are other Python/Xorg/gnome-shell
processes, not a running CARLA server. Next action should be diagnosis/cleanup
of the stuck worker/zombie CARLA start path, not blindly launching another
collection instance.

## MultiScene20 Live Progress 2026-05-12 21:11 CST

Read-only progress check. Collection process remains alive at pid 849375 but
effective active CARLA servers fluctuate; at the check only port 2120 was
listening, while other workers appeared to be in restart/backoff or had recently
crashed. Current dataset status:

```text
accepted_total: 1871 / 3000
remaining_total: 1129
completed_scenes: 10 / 20
failed_attempt_total: 14728
missing_validation: 9
missing_tx_assignment: 167
```

Unfinished scenes:

```text
town01_opt_junction_0143 41/150 fail=339 rate=10.79% newest=20:50:07
town05_opt_junction_1722 40/150 fail=373 rate=9.69% newest=21:08:07
town04_opt_junction_0148 25/150 fail=49 rate=33.78%
town04_opt_junction_1368 51/150 fail=1 rate=98.08%
town05_opt_junction_0359 83/150 fail=247 rate=25.15% newest=20:56:15
town05_opt_junction_2086 56/150 fail=73 rate=43.41%
town10_junction_0189 17/150 fail=7 rate=70.83%
town10_junction_0719 26/150 fail=8 rate=76.47%
town04_opt_junction_1197 21/150 fail=230 rate=8.37% newest=21:06:57
town04_opt_junction_0785 11/150 fail=30 rate=26.83%
```

Recent failure pressure is mostly in four active scenes. `town04_opt_junction_1197`
and `town05_opt_junction_1722` are the current lowest-rate unfinished scenes
(~8-10%) but still above the prior replacement threshold (<5% and >=500
attempts). Run metadata repair again before RF; missing tx/validation during live
collection is expected.


## MultiScene20 CARLA Crash Rate Check 2026-05-12 CST

User asked whether Linux-killed/CARLA segmentation-fault failures decreased after
the latest changes. Exact per-line Signal 11 rate cannot be reconstructed from
CARLA stderr because those logs are append-only and mostly lack timestamps. Used
failed-attempt `failure_code` as the measurable proxy:

```text
removed pathological scenes before replacement:
  town02_opt_junction_0076: carla_subprocess_failed=129, accepted=47
  town05_opt_junction_0979: carla_subprocess_failed=182, accepted=90
  town04_opt_junction_0916: carla_subprocess_failed=67, accepted=38
  total carla_subprocess_failed=378 / 175 accepted = 2.16 per accepted

replacement scenes current:
  town01_opt_junction_0143: carla_subprocess_failed=28, accepted=34
  town05_opt_junction_0359: carla_subprocess_failed=24, accepted=43
  town04_opt_junction_1197: carla_subprocess_failed=18, accepted=19
  total carla_subprocess_failed=70 / 96 accepted = 0.73 per accepted
```

Including adjacent CARLA availability/timeout failure codes gives about 2.22 per
accepted before vs 0.83 per accepted after, roughly a 60%+ normalized reduction.
CARLA Signal 11 still occurs, so this is improvement, not elimination.


## Code Cleanup 2026-05-12 CST

Performed conservative cleanup while MultiScene20 CARLA collection was still
running. Active runtime modules used by future CARLA attempts were not changed.
Removed unused/delegation-only or standalone legacy modules that had no package
inbound imports and were not part of the supported CLI path:

```text
scripts/dynamic_radio_dataset/carla/attempts.py
scripts/dynamic_radio_dataset/carla/collection_modes.py
scripts/dynamic_radio_dataset/carla/collection_summary.py
scripts/dynamic_radio_dataset/carla/plan_selection_state.py
scripts/dynamic_radio_dataset/qa/records.py
scripts/dynamic_radio_dataset/qa/rss_diagnostics.py
scripts/dynamic_radio_dataset/radio_dataset_utils.py
scripts/dynamic_radio_dataset/render/dynamic_scene.py
```

Also removed scoped `__pycache__` / `.pyc` artifacts under active project paths.
Validation after cleanup passed:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

Final live check during cleanup showed collection still alive at pid 849375 and
`accepted_total=1789/3000`. Do not do deeper refactors of active collection
modules until the current CARLA run is stopped or complete, because new collect
subprocesses import code from disk on every attempt.


## MultiScene20 Process Tree Check 2026-05-12 19:44 CST

User questioned whether collection is really running. Read-only process-tree check
confirmed the main collection is alive, but effective CARLA concurrency is not
currently 4. Process group 849375 contains the bash launcher, python orchestrator,
resource tracker, and four multiprocessing worker processes. At the check time:

```text
active CARLA servers: ports 2100 and 2110 only
active collect subprocesses: 2
  2100 -> town01_opt_junction_0143 attempt_000176
  2110 -> town04_opt_junction_1197 attempt_000165
workers 2/3: alive as multiprocessing workers, but no active 2120/2130 CARLA server at that instant; likely in restart/backoff after Signal 11 crashes
```

So collection is not dead, but current effective parallelism is about 2 workers,
not the intended 4. Latest accepted timestamps showed recent accepted episodes
for town05_opt_junction_1722 at 19:06:54 and town04_opt_junction_1197 at
19:07:23; several other unfinished scenes had not progressed since around 16:02.
Do not tell the user it is fully using 4 active CARLA instances unless ports
2100/2110/2120/2130 are all up with active collect subprocesses.


## MultiScene20 Live Status 2026-05-12 19:36 CST

Read-only status check; collection was not restarted. Current CARLA-only
collection process is alive:

```text
pid/pgid: 849375 / 849375
elapsed: about 2h43m
accepted_total: 1775 / 3000
remaining_total: 1225
failed_attempt_total: 14250
new accepted since 2026-05-12 16:53 launch: 85
new failed attempts since launch: 709
```

Current unfinished scenes and observed acceptance rates:

```text
town01_opt_junction_0143 24/150 fail=143 rate=14.37%
town05_opt_junction_1722 24/150 fail=276 rate=8.00%  # current worst unfinished
town04_opt_junction_0148 25/150 fail=49  rate=33.78%
town04_opt_junction_1368 51/150 fail=1   rate=98.08%
town05_opt_junction_0359 24/150 fail=147 rate=14.04%
town05_opt_junction_2086 56/150 fail=73  rate=43.41%
town10_junction_0189    17/150 fail=7   rate=70.83%
town10_junction_0719    26/150 fail=8   rate=76.47%
town04_opt_junction_1197 17/150 fail=145 rate=10.49%
town04_opt_junction_0785 11/150 fail=30  rate=26.83%
```

Failure reasons since launch are dominated by trajectory/plan failures, not only
CARLA process crashes:

```text
vehicle_vehicle_collision: 404
traffic_plan_spawn_incomplete: 170
carla_subprocess_failed: 47
long_all_stop: 37
required_primary_not_moving_enough: 25
vehicle_building_collision: 18
carla_collect_subprocess_timeout: 4
carla_server_unavailable: 2
```

CARLA UE4 Signal 11 crashes are still present in worker stderr logs. The high
restart budget/backoff path is keeping the main collection process alive, but
workers spend time restarting. Newly collected accepted episodes currently have
lagging metadata while the scene is still in progress: missing validation=8,
missing tx_assignment=71. This is not an RF-ready state; run
`repair-multi-scene-metadata` again after stopping/completing collection before
any RF stage.


## MultiScene20 Repair / Replacement / Resume 2026-05-12 17:05 CST

User asked to repair interrupted collection metadata, remove newly low-success
unfinished scenes, validate replacements, and restart CARLA-only trajectory
collection. Completed actions:

```text
metadata repair command: python3 scripts/drd.py repair-multi-scene-metadata --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
validation/tx missing after repair: 0 / 0
replaced unfinished low-success scenes:
  town02_opt_junction_0076 47 accepted / 2641 failed, 1.75%
  town05_opt_junction_0979 90 accepted / 3557 failed, 2.47%
  town04_opt_junction_0916 38 accepted / 1141 failed, 3.22%
validated replacements from existing candidate_id values only:
  town01_opt_junction_0143, route_count=6, 40 TX, 1200 plans
  town05_opt_junction_0359, route_count=6, 40 TX, 1200 plans
  town04_opt_junction_1197, route_count=6, 40 TX, 1200 plans
failed validation backup quarantined:
  town03_opt_junction_1696, SIGSEGV during reference capture
active accepted after replacement before resume: 1690 / 3000
remaining before resume: 1310
```

Artifacts:

```text
configs/dynamic_radio/selected_scene_manifest_high_failure_replacements_3_20260512.yaml
datasets/DynamicRadioMap/MultiScene20/indexes/metadata_repair_latest.json
datasets/DynamicRadioMap/MultiScene20/indexes/high_failure_scene_replacement_report.json
datasets/DynamicRadioMap/MultiScene20/indexes/low_success_scene_replacement_report.json
datasets/DynamicRadioMap/MultiScene20/indexes/inactive_scene_dirs_cleanup_latest.json
datasets/DynamicRadioMap/MultiScene20/indexes/current_live_status_latest.json
datasets/DynamicRadioMap/MultiScene20/quarantine/high_failure_replaced_20260512_164913/
datasets/DynamicRadioMap/MultiScene20/quarantine/validation_failed_20260512_164913/
datasets/DynamicRadioMap/MultiScene20/quarantine/config_backups/
```

Code changes in this pass:

```text
added repair-multi-scene-metadata CLI for recoverable missing validation_report/tx_assignment metadata
selected-scene validation now uses the sanitized multi_scene CARLA launcher instead of the older supervisor launcher
collect-multi-scene now uses static unfinished-first round-robin scheduling so 4 workers receive unfinished scenes
```

Validation passed before restart:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
python3 scripts/drd.py validate-multi-scene --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
collect-multi-scene --dry-run confirmed GPU mapping ['0','1','0','1'] and unfinished scenes on all 4 workers
```

Collection was restarted with no-rendering, 4 workers across GPU0/GPU1, restart
budget 1000, and 30s worker stagger:

```text
pid: 849375
pgid: 849375
log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_20260512_165250.log
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_latest.pid
command: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_latest_command.txt
```

Early live status showed the process alive and new accepted episodes already
appearing. Latest check before handoff at 2026-05-12 17:06 CST:

```text
accepted_total: 1706 / 3000
remaining: 1294
town01_opt_junction_0143: 5 accepted / 1 failed
town05_opt_junction_0359: 10 accepted / 13 failed
town04_opt_junction_1197: 1 accepted / 7 failed
process pid/pgid: 849375 / 849375
```

CARLA still shows intermittent Signal 11/startup crashes, but the high-budget
restart path is continuing and collection progress is being made. No Sionna RT/RF
processing was run.

Cleanup completed before handoff:

```text
active MultiScene20/scenes directory now has exactly the 20 active config scenes
15 stale inactive scene dirs were moved to quarantine/inactive_scene_dirs_cleanup_20260512_165417/
configs/dynamic_radio no longer contains *.backup files
scoped cleanup found no __pycache__, .pyc, .tmp, .bak, .orig, or editor backup files under active project/config docs paths
```

Because collection is live, brand-new accepted episodes may temporarily miss
`tx_assignment.json` or reconstructed legacy `validation_report.json` until the
scene finishes or `repair-multi-scene-metadata` is run again. This is expected
for live writes; do not run RF until collection is stopped/complete and a final
repair pass reports zero missing metadata.


## CARLA Stability Implementation 2026-05-12

Implemented dual-GPU/no-rendering stabilization for future multi-scene CARLA
collection. No collection process was restarted during the implementation.

Code-level behavior now available:

```text
dynamic_radio_dataset.carla.collect --no-rendering-mode
collect-multi-scene --gpu-ids 0,1
collect-multi-scene --no-rendering-mode  # default true
collect-multi-scene --rendering-mode     # visual/debug override
```

Multi-scene CARLA-only collection now sets `carla.no_rendering_mode=true` in each
worker runtime config by default, causing the per-attempt collect subprocess to
pass `--no-rendering-mode` to `dynamic_radio_dataset.carla.collect`. This keeps
trajectory physics/state export but disables CARLA world rendering for the
trajectory-only stage. RGB/topdown preview paths are unchanged.

4-worker dual-GPU mapping is supported with round-robin assignment:

```text
--gpu-ids 0,1 => worker0 GPU0, worker1 GPU1, worker2 GPU0, worker3 GPU1
```

CARLA server launches now also default to:

```text
sanitize DISPLAY/XAUTHORITY/WAYLAND/VSCode display env vars
disable core dumps with RLIMIT_CORE=0
restart backoff: 30s, 60s, 120s, 300s capped
no hard crash circuit breaker; keep high restart budget if requested
worker summary includes cumulative CARLA stderr signature counts
```

Validation run after implementation passed:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
python3 scripts/drd.py collect-multi-scene --help
collect-multi-scene dry-run with --carla-workers 4 --gpu-ids 0,1 --no-rendering-mode
```

Dry-run confirmed:

```text
worker_count: 4
gpu_ids: ['0', '1', '0', '1']
no_rendering_mode: true
max_carla_restarts_per_scene: 1000
```

Recommended next run command:

```bash
python3 scripts/drd.py collect-multi-scene \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --carla-workers 4 \
  --gpu-ids 0,1 \
  --rpc-ports 2100,2110,2120,2130 \
  --tm-ports 8100,8110,8120,8130 \
  --target-accepted-per-scene 150 \
  --max-carla-restarts-per-scene 1000 \
  --no-rendering-mode
```


## Cleanliness Note 2026-05-12

After the CARLA stability implementation, generated Python cache directories and
local `/tmp` dry-run capture files from verification were removed. A scoped scan
of active project paths found no `__pycache__`, `.pyc`, `.tmp`, `.bak`, `.orig`,
or editor backup files under `scripts/dynamic_radio_dataset/`,
`configs/dynamic_radio/`, or `.agent/`.

Repository still contains intentional untracked project files from the current
multi-scene feature branch/scaffold (for example `multi_scene/`, `tx/`, and
new multi-scene configs). These are not temporary files and should not be deleted
as cleanup.

## MultiScene20 Status Check 2026-05-12

User asked for previous run status only; no task was restarted. The previously
launched 4-worker collection process is no longer running. PID file still points
to stale PID 744955, but `ps` shows no process and no CARLA/collection ports are
listening on 2100/2110/2120/2130 or 8100/8110/8120/8130.

Active config:

```text
configs/dynamic_radio/multi_scene_20x150_resolved.yaml
```

Current active dataset counts from filesystem scan:

```text
accepted_total: 1865 / 3000
remaining_total: 1135
completed_scene_count: 10 / 20
active failed_attempt_total: 20884
```

Completed scenes include the four replacements, although several completed only
after many failures:

```text
town05_opt_junction_0207 150/150, fail=2582, replacement for town01_opt_junction_0110
town04_opt_junction_0053 150/150, fail=3732, replacement for town04_opt_junction_0483
town10_junction_0664     150/150, fail=4339, replacement for town05_opt_junction_1882
```

Remaining unfinished scenes at check time:

```text
town02_opt_junction_0076  47/150, fail=2641, rate=1.75%
town05_opt_junction_1722   4/150, fail=6,    rate=40.00%
town04_opt_junction_0148  25/150, fail=49,   rate=33.78%
town04_opt_junction_1368  51/150, fail=1,    rate=98.08%
town05_opt_junction_0979  90/150, fail=3557, rate=2.47%
town05_opt_junction_2086  56/150, fail=73,   rate=43.41%
town10_junction_0189     17/150, fail=7,    rate=70.83%
town10_junction_0719     26/150, fail=8,    rate=76.47%
town04_opt_junction_0916  38/150, fail=1141, rate=3.22%
town04_opt_junction_0785  11/150, fail=30,   rate=26.83%
```

Metadata completeness before any RF/finalize work needs repair:

```text
active episode dirs: 1865
missing validation_report.json: 38
missing tx_assignment.json: 132
```

Missing metadata by scene:

```text
town05_opt_junction_0207: validation missing 6, tx missing 0
town02_opt_junction_0076: validation missing 0, tx missing 16
town05_opt_junction_0979: validation missing 7, tx missing 90
town04_opt_junction_0053: validation missing 14, tx missing 0
town04_opt_junction_0916: validation missing 1, tx missing 26
town10_junction_0664:     validation missing 10, tx missing 0
```

No Sionna RT/RF has been run for MultiScene20. Before resuming collection, likely
next steps are: decide whether to replace newly pathological unfinished scenes
(`town02_opt_junction_0076`, `town05_opt_junction_0979`, `town04_opt_junction_0916`)
or let near-complete `town05_opt_junction_0979` continue; repair missing
validation/tx metadata; then restart CARLA collection only if user asks.

## MultiScene20 High-Failure Scene Replacement + Resume 2026-05-10 16:40 CST

User decided that scenes with very high runtime failure rates should be replaced
rather than spending unbounded CARLA attempts on them. The previous 4-worker
collection process from 2026-05-08 was no longer alive after reboot, so a fresh
replacement/collection cycle was run.

Current active config remains:

```text
configs/dynamic_radio/multi_scene_20x150_resolved.yaml
```

Runtime replacement policy used for this manual intervention:

```text
replace unfinished scenes with >=500 attempts and observed acceptance rate <5%
do not replace already completed scenes
validate replacements from existing discovery candidate_id only before use
```

Removed from the active 20-scene config and quarantined (data preserved, not
deleted):

```text
town01_opt_junction_0110: 64 accepted / 4127 failed, 1.53% acceptance
town05_opt_junction_0053: 96 accepted / 3898 failed, 2.40% acceptance
town04_opt_junction_0483: 1 accepted / 2915 failed, 0.03% acceptance
town05_opt_junction_1882: 96 accepted / 2691 failed, 3.44% acceptance
```

Quarantine path:

```text
datasets/DynamicRadioMap/MultiScene20/quarantine/high_failure_replaced_20260510_162751/
```

Validated replacements, all generated from the existing discovery catalog and all
prepared successfully with reference_scene, Sionna reference export, route_library,
40-TX tx_catalog, and 1200 accepted plans:

```text
town05_opt_junction_0207 -> replaces town01_opt_junction_0110
town05_opt_junction_0979 -> replaces town05_opt_junction_0053
town04_opt_junction_0053 -> replaces town04_opt_junction_0483
town10_junction_0664     -> replaces town05_opt_junction_1882
```

Replacement artifacts:

```text
configs/dynamic_radio/selected_scene_manifest_high_failure_replacements_4.yaml
configs/dynamic_radio/multi_scene_20x150_resolved.yaml.backup_before_high_failure_replacement_20260510_162751
datasets/DynamicRadioMap/MultiScene20/scene_validation_report.json
datasets/DynamicRadioMap/MultiScene20/indexes/high_failure_scene_replacement_report.json
datasets/DynamicRadioMap/MultiScene20/logs/validate_high_failure_replacements_20260510_162403.log
```

Validation checks after config replacement passed:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
python3 scripts/drd.py collect-multi-scene ... --dry-run
```

4-worker CARLA collection was resumed on GPU1. Current live process at last
check:

```text
PID/PGID: 744955
log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers_gpu1_post_high_failure_replacement_20260510_163029.log
status: datasets/DynamicRadioMap/MultiScene20/indexes/current_live_status_20260510_163512.json
latest status symlink-like copy: datasets/DynamicRadioMap/MultiScene20/indexes/current_live_status_latest.json
```

Launch command is saved in:

```text
datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers_restart_command.txt
```

Status at 2026-05-10 16:35 CST after replacement/resume:

```text
active accepted_total: 1305 / 3000
remaining_total_to_3000: 1695
completed_scene_count: 7 / 20
failed_attempt_total for active scenes: 3247
```

Early replacement yields looked much better than the removed pathological scenes:

```text
town05_opt_junction_0207: 3 accepted / 8 failed
town05_opt_junction_0979: 7 accepted / 1 failed
town04_opt_junction_0053: 6 accepted / 4 failed
town10_junction_0664:     6 accepted / 2 failed
```

CARLA servers may still crash/restart under the X/Vulkan environment (RenderThread
timeout / Signal 11 messages in worker logs), but the high restart budget remains
active (`--max-carla-restarts-per-scene 1000`) and the main collector was alive at
last check. Do not run Sionna RT/RF yet unless the user explicitly authorizes it.



## Standalone Vehicle Blueprint RF Audit 2026-05-08 15:42 CST

User requested a standalone diagnostic tool to screen CARLA vehicle blueprints
for 2D-occupancy radio-map input/label consistency. No production pipeline code,
Sionna numeric code, CARLA collection code, or generated runtime datasets were
modified for this audit.

Added standalone research tool:

```text
scripts/drd_research/vehicle_rf_audit/README.md
scripts/drd_research/vehicle_rf_audit/vehicle_rf_audit.py
```

The tool reads candidate blueprint lists or enumerates CARLA blueprints, accepts
manual/visual clearance review metadata, optionally scores fixed-scene RF cases,
writes `vehicle_audit_report.csv`, `vehicle_whitelist.json`,
`vehicle_exclusion_reasons.json`, `audit_manifest.json`, `rf_test_plan.json`,
and PPM preview heatmaps. `.gitignore` now un-ignores
`scripts/drd_research/**` so the standalone tool can be tracked.

Current main-program vehicle set extracted from the Town10 plan catalog/config:

```text
vehicle.chevrolet.impala
vehicle.mini.cooper_s
vehicle.mitsubishi.fusorosa
vehicle.nissan.micra
vehicle.seat.leon
vehicle.tesla.model3
```

Only one large vehicle is currently used by the main program:
`vehicle.mitsubishi.fusorosa`. It is the high-clearance/problematic bus. There
are no alternative low-bottom large CARLA blueprints in the current main vehicle
set, so the audit can prove exclusion of the current problematic large vehicle
and safe inclusion of the current low-body small cars, but cannot prove a
separate low-bottom large blueprint without adding audit-only candidates.

Audit artifacts:

```text
tmp/vehicle_rf_audit_current_main_20260508/main_vehicle_blueprints.json
tmp/vehicle_rf_audit_current_main_20260508/vehicle_view_review.json
tmp/vehicle_rf_audit_current_main_20260508/out_fresh_rx10/vehicle_audit_report.csv
tmp/vehicle_rf_audit_current_main_20260508/out_fresh_rx10/vehicle_whitelist.json
tmp/vehicle_rf_audit_current_main_20260508/out_fresh_rx10/vehicle_exclusion_reasons.json
tmp/vehicle_rf_audit_current_main_20260508/fresh_sionna_rx10_20260508_153754/
tmp/vehicle_rf_audit_current_main_20260508/fresh_rf_proof_summary_rx10.json
```

Fresh CPU Sionna RT was rerun for episode `episode_000051`, TX `tx_00`, frame
55, fixed `car_40` pose, TX z=1.5 m, RX z=1.0 m, resolution 128, max-depth 3,
250k samples, CPU `llvm_ad_rgb`. Two temporary exports were compared:

```text
real fusorosa mesh:
  datasets/DynamicRadioMap/Town10/episodes/episode_000051/sionna_export
solid metal bbox proxy:
  tmp/episode_000051_tx00_box_ablation_20260507/box_metal/sionna_export
```

Fresh audit result on current main vehicles:

```text
candidate_count: 6
whitelist: 5
exclude: 1
review: 0
excluded: vehicle.mitsubishi.fusorosa
whitelist: vehicle.chevrolet.impala, vehicle.mini.cooper_s,
           vehicle.nissan.micra, vehicle.seat.leon, vehicle.tesla.model3
```

Fusorosa RF metrics from `out_fresh_rx10/vehicle_audit_report.csv`:

```text
shadow_strength = 4.603 dB
underbody_bright_spot_score = 0.574  (> threshold 0.18)
shadow_consistency_score = 0.000     (< threshold 0.55)
affected_area_ratio = 0.0588
bright_area_ratio = 0.0341
```

Fresh proof summary in the broad behind-vehicle region:

```text
real fusorosa mesh: 150/198 cells > -70 dBm, mean -106.80 dBm
solid bbox proxy:     0/198 cells > -70 dBm, mean -270.00 dBm
real bright-patch mask: real mesh 150/150 > -70 dBm; solid bbox 0/150 > -70 dBm
```

Interpretation: the current large bus mesh creates a bright patch inconsistent
with a 2D solid occupancy blocker, while the same pose represented as a solid
low-underbody bbox removes the patch. This supports excluding fusorosa from a
2D-occupancy training vehicle whitelist.

Validation run for the standalone tool:

```bash
python3 -m py_compile scripts/drd_research/vehicle_rf_audit/vehicle_rf_audit.py
python3 scripts/drd_research/vehicle_rf_audit/vehicle_rf_audit.py --help
```


## Large Vehicle CARLA Mesh / Sionna RF Selection Audit 2026-05-08 17:30 CST

User asked whether CARLA has more large vehicles and requested an audit-only
large-vehicle selection report: export large CARLA vehicle meshes for visual
screening, then run fixed Sionna RT tests. No production dataset, CARLA
collection code, Sionna numeric code, or main pipeline code was modified.

A temporary live CARLA server on port 2200 was attempted for view export, but it
crashed under the current X/Vulkan environment (`GameThread timed out waiting for
RenderThread`, Signal 11). The failed temporary server was not left running. To
avoid interfering with the active multi-scene collection servers, visual export
was completed offline from CARLA Unreal assets via UModel instead.

Large/medium-large CARLA blueprint candidates audited:

```text
vehicle.bmw.grandtourer
vehicle.carlamotors.carlacola
vehicle.carlamotors.european_hgv
vehicle.carlamotors.firetruck
vehicle.ford.ambulance
vehicle.jeep.wrangler_rubicon
vehicle.mercedes.sprinter
vehicle.mitsubishi.fusorosa
vehicle.nissan.patrol
vehicle.nissan.patrol_2021
vehicle.tesla.cybertruck
vehicle.volkswagen.t2
vehicle.volkswagen.t2_2021
```

Artifacts:

```text
tmp/vehicle_rf_large_audit_20260508/large_vehicle_views_contact_sheet.png
tmp/vehicle_rf_large_audit_20260508/views/*_views.png
tmp/vehicle_rf_large_audit_20260508/large_vehicle_visual_metrics.json
tmp/vehicle_rf_large_audit_20260508/large_vehicle_asset_catalog.audit.json
tmp/vehicle_rf_large_audit_20260508/sionna_exports/<vehicle>/{catalog_mesh,bbox}/
tmp/vehicle_rf_large_audit_20260508/rf_runs/<vehicle>/{mesh,bbox}/rss_maps.npz
tmp/vehicle_rf_large_audit_20260508/out_large/vehicle_audit_report.csv
tmp/vehicle_rf_large_audit_20260508/out_large/vehicle_whitelist.json
tmp/vehicle_rf_large_audit_20260508/large_vehicle_rf_region_summary.csv
tmp/vehicle_rf_large_audit_20260508/large_vehicle_selection_report.{md,csv,json}
```

Visual screening excluded only `vehicle.carlamotors.european_hgv` before RF due
to clear exposed tractor/trailer chassis and high/open underbody. The remaining
12 candidates were run through fixed CPU Sionna RT at the same Town10
`episode_000051`, frame 55, TX00 context as the fusorosa artifact, comparing
real catalog mesh against a same-footprint solid bbox proxy. Each candidate used
TX z=1.5 m, RX z=1.0 m, 128x128 grid, max-depth 3, 250k samples, CPU
`llvm_ad_rgb`.

Result: no large/van/SUV candidate is selectable under the current real-mesh +
2D occupancy solid-blocker consistency policy. Every RF-tested candidate left
substantial high-RSS cells in the nominal behind-vehicle solid-bbox shadow
region. Summary from `large_vehicle_selection_report.md`:

```text
Selectable count: 0 / 13
RF-tested count: 12 / 13
visual-only excluded: vehicle.carlamotors.european_hgv
known worst control: vehicle.mitsubishi.fusorosa, 150 excess bright cells
least-bad but still not recommended:
  vehicle.bmw.grandtourer: 73 excess bright cells
  vehicle.jeep.wrangler_rubicon: 73 excess bright cells
  vehicle.nissan.patrol: 79 excess bright cells
  vehicle.volkswagen.t2_2021: 93 excess bright cells
  vehicle.carlamotors.carlacola: 96 excess bright cells
```

Interpretation: for the current Sionna real vehicle meshes and reflective ground
setup, simply choosing a visually lower large vehicle does not make the label
consistent with a 2D solid occupancy input. If large vehicles are required in
future training data, the defensible options are to use solid/proxy vehicle RF
geometry consistently during label generation, or add 3D/clearance/mesh
information to the model input. Using current real large meshes with only 2D
occupancy input is not recommended.

Standalone tool validation after the audit changes passed:

```bash
python3 -m py_compile scripts/drd_research/vehicle_rf_audit/vehicle_rf_audit.py
python3 scripts/drd_research/vehicle_rf_audit/vehicle_rf_audit.py --help
```


## All CARLA Vehicle RF Shadow Audit 2026-05-08 17:52 CST

User asked to expand beyond large vehicles and test all callable CARLA
`vehicle.*` blueprints. This was done audit-only; no production pipeline or
runtime dataset was modified. The active multi-scene collection process was left
running.

All 41 CARLA 0.9.15 `vehicle.*` blueprints were enumerated from a live existing
CARLA server and mapped to CARLA Unreal assets. Offline UModel mesh extraction
was used for views/exports. Every candidate successfully produced both a real
mesh Sionna export and a same-size solid bbox proxy export, then both were run
through CPU Sionna RT at the fixed Town10 `episode_000051`, frame 55, TX00
stress-test context.

Main artifacts:

```text
tmp/vehicle_rf_all_audit_20260508/all_vehicle_views_contact_sheet.png
tmp/vehicle_rf_all_audit_20260508/views/*_views.png
tmp/vehicle_rf_all_audit_20260508/sionna_rf_manifest.json
tmp/vehicle_rf_all_audit_20260508/rf_runs/<vehicle>/{mesh,bbox}/rss_maps.npz
tmp/vehicle_rf_all_audit_20260508/rf_cases/<blueprint_id>/{baseline,with_vehicle}.npz
tmp/vehicle_rf_all_audit_20260508/all_vehicle_rf_shadow_report.{md,csv,json}
tmp/vehicle_rf_all_audit_20260508/all_vehicle_practical_selection_report.{md,json}
tmp/vehicle_rf_all_audit_20260508/all_vehicle_tiered_selection_report.{md,json}
tmp/vehicle_rf_all_audit_20260508/out_all/vehicle_audit_report.csv
```

Metric used in the practical/tiered report:

```text
leak_cells = cells in the same-size solid-bbox shadow region where real mesh RSS > -70 dBm
```

Strict result: no real CARLA vehicle mesh passes strict 2D solid-blocker
consistency in this fixed stress test. This is a deliberately harsh location; it
should be interpreted as a ranking/filter rather than a universal physical law.

Practical compact-car recommendation from `all_vehicle_tiered_selection_report.md`:

```text
Tier A recommended compact passenger cars:
  vehicle.mini.cooper_s        leak=56
  vehicle.audi.a2              leak=57
  vehicle.nissan.micra         leak=57
  vehicle.mini.cooper_s_2021   leak=64
  vehicle.citroen.c3           leak=66
  vehicle.audi.tt              leak=69
Optional tiny/non-standard:
  vehicle.micro.microlino      leak=36

Tier B optional small cars if diversity is more important than strict consistency:
  vehicle.toyota.prius         leak=74
  vehicle.audi.etron           leak=76
  vehicle.seat.leon            leak=77
  vehicle.tesla.model3         leak=77
  vehicle.dodge.charger_2020   leak=82
  vehicle.dodge.charger_police leak=84
  vehicle.lincoln.mkz_2020     leak=84
```

Current main vehicle assessment from the same report:

```text
keep: vehicle.mini.cooper_s, vehicle.nissan.micra
replace/exclude: vehicle.mitsubishi.fusorosa, vehicle.tesla.model3,
                 vehicle.chevrolet.impala, vehicle.seat.leon
```

Engineering recommendation: keep compact passenger cars only for real-mesh RF
labels if mild label noise is acceptable; exclude large/van/SUV/truck/two-wheel
vehicles for the current 2D solid occupancy contract. If large vehicles are
required, switch RF label generation to solid/proxy vehicle geometry or add
height/clearance/mesh channels to model inputs.


## Episode 000051 TX00 Sionna Built-in Ground Material Ablation 2026-05-07 17:48 CST

User asked to switch the road/ground to Sionna built-in ground material
parameters and rerun. Temporary-only single-frame CPU Sionna jobs were run for
frame 55/TX00; no production code or original dataset files were modified.

Temporary exports changed only `road_support_region` material, keeping buildings
as `itu_concrete` and vehicles as `itu_metal`:

```text
tmp/episode_000051_tx00_ground_material_ablation_20260507/itu_very_dry_ground/
tmp/episode_000051_tx00_ground_material_ablation_20260507/itu_medium_dry_ground/
tmp/episode_000051_tx00_ground_material_ablation_20260507/itu_wet_ground/
tmp/episode_000051_tx00_ground_material_ablation_20260507/vacuum/
```

Analysis artifacts:

```text
tmp/episode_000051_tx00_ground_material_ablation_20260507/analysis/numeric_summary.json
tmp/episode_000051_tx00_ground_material_ablation_20260507/analysis/frame55_ground_material_compare_zoom.png
```

Sionna material properties at 5.89 GHz:

```text
itu_concrete:         eps=5.24,    sigma=0.184938 S/m
itu_very_dry_ground: eps=3.0,     sigma=0.013085 S/m
itu_medium_dry_ground: eps=12.56, sigma=0.630022 S/m
itu_wet_ground:      eps=14.76,   sigma=1.503977 S/m
vacuum:              eps=1.0,     sigma=0
```

In the baseline bright-patch mask:

```text
baseline concrete: 150/150 > -70 dBm, mean -58.35 dBm
very dry ground:   150/150 > -70 dBm, mean -57.49 dBm
medium dry ground: 143/150 > -70 dBm, mean -60.86 dBm
wet ground:        142/150 > -70 dBm, mean -61.52 dBm
vacuum ground:       5/150 > -70 dBm, mean -216.39 dBm
no road:             5/150 > -70 dBm, mean -262.97 dBm
```

Interpretation: Sionna built-in ground materials do not remove the bright patch;
very dry ground is even slightly brighter in this geometry, while medium/wet
only reduce it by a few dB. The decisive factor is not simply concrete vs ground
material conductivity; it is the existence of a smooth reflective planar ground
interface with reflection enabled. `vacuum` ground/no-road suppresses the patch,
but that is effectively disabling ground reflection rather than modeling road
asphalt.


## Episode 000051 Ground Material Parameters 2026-05-07 17:41 CST

Checked current ground material setup for the TX00 bright-patch discussion. No
code/data changes. `road_support_region.ply` is a 192 m x 192 m flat plane at
z=0 made from 4 vertices / 2 triangles, not an asphalt road mesh with lane-level
material detail. It is referenced in `scene.xml` with `mat-itu_concrete`, and
Sionna maps that to radio material `itu_concrete`. `rss_compute.py` sets
`scene.frequency = 5.89e9`, so the actual RF properties are the Sionna ITU
concrete values at 5.89 GHz:

```text
relative_permittivity = 5.24
conductivity = 0.184938 S/m
scattering_coefficient = 0
```

Earlier load-scene-only probes showed conductivity 0.123087 S/m because Sionna's
default scene frequency is 3.5 GHz before `rss_compute.py` sets 5.89 GHz. The
RF run uses 0.184938 S/m.

The XML optical BSDF roughness/specular fields are not the main radio-reflection
parameters; Sionna coverage-map propagation uses radio material permittivity and
conductivity with reflection enabled. Current ground is therefore an ideal smooth
ITU concrete support plane, not asphalt. This can over-idealize coherent ground
bounce, especially at low TX/RX heights and grazing/underbody paths.


## Episode 000051 TX00 TX-Height Ablation 2026-05-07 17:36 CST

User asked whether raising the TX to about 3 m would remove the anomalous
ground/underbody bright patch. Ran temporary single-frame CPU Sionna jobs only;
no production code or original dataset files were modified.

Artifacts:

```text
tmp/episode_000051_tx00_txheight_ablation_20260507/original_road_tx3/tx_00/rss_maps.npz
tmp/episode_000051_tx00_txheight_ablation_20260507/no_road_tx3/tx_00/rss_maps.npz
tmp/episode_000051_tx00_txheight_ablation_20260507/analysis/numeric_summary.json
tmp/episode_000051_tx00_txheight_ablation_20260507/analysis/frame55_txheight_ground_compare_zoom.png
```

Compared four frame-55 variants: baseline TX z=1.5 with road, no-road TX z=1.5,
TX z=3.0 with road, and TX z=3.0 no-road. In the baseline bright-patch mask:

```text
baseline tx1.5 + road: 150/150 cells > -70 dBm, mean -58.35 dBm
no-road tx1.5:          5/150 cells > -70 dBm, mean -262.97 dBm
tx3 + road:             37/150 cells > -70 dBm, mean -185.88 dBm
tx3 + no-road:          1/150 cells > -70 dBm, mean -268.57 dBm
```

Interpretation: raising TX to 3 m substantially weakens/shrinks and shifts the
bright patch but does not eliminate all road/vehicle-reflection effects while the
road remains reflective. Removing the road/ground reflection is much more
decisive for this artifact.


## Episode 000051 TX00 Ground-Reflection Ablation 2026-05-07 17:33 CST

User hypothesized the anomalous bright patch behind/near the fusorosa might be
caused by a TX-to-vehicle-underbody-to-ground reflection path. Performed a
temporary single-frame CPU ablation only; no production code or dataset files
were modified.

Current Sionna scene material settings for the original episode:

```text
road_support_region: Sionna radio material itu_concrete
  relative_permittivity ~= 5.24
  conductivity ~= 0.123 S/m at 5.89 GHz
  scattering_coefficient = 0
static_buildings_proxy: same itu_concrete
vehicles: itu_metal, conductivity = 1e7 S/m
coverage_map reflection=True, diffraction=False, scattering=False
TX00 height = 1.5 m, RX grid height = 1.0 m
```

Created temporary export with `road_support_region` removed from `scene.xml`:

```text
tmp/episode_000051_tx00_ground_ablation_20260507/no_road/sionna_export/
tmp/episode_000051_tx00_ground_ablation_20260507/no_road/tx_00/rss_maps.npz
tmp/episode_000051_tx00_ground_ablation_20260507/analysis/numeric_summary.json
tmp/episode_000051_tx00_ground_ablation_20260507/analysis/frame55_no_road_vs_original_zoom.png
```

Command used existing `dynamic_radio_dataset.rf.rss_compute` with CPU LLVM, frame
55 only, TX00 at `(-32.760105, 7.430441, 1.5)`, RX height 1.0, max_depth 3,
250000 samples.

Result: the broad behind-bus bright region is essentially removed when the road
mesh is absent. In the original-bright-patch mask, stored original had 150/150
cells > -70 dBm with mean about -58.35 dBm; no-road had only 5/150 cells > -70
dBm, 145/150 floor cells, mean about -262.97 dBm. The bus footprint itself
changed little, which points to the road/ground reflection path being critical
for the distant bright patch rather than just vehicle mesh leakage.


## Episode 000051 car_40 Sionna Mesh Visualization 2026-05-07 17:19 CST

User asked whether the anomalous TX00 bright patch could be caused by bad mesh
export and requested multi-view visualization of the vehicle mesh in the Sionna
RT/Mitsuba environment. No production code or dataset files were modified.
Temporary-only scripts/images were written under `tmp/`.

Rendered the exported Sionna PLY for the large vehicle:

```text
datasets/DynamicRadioMap/Town10/episodes/episode_000051/sionna_export/meshes/car_40.ply
vehicle: vehicle.mitsubishi.fusorosa
material in Sionna scene: mat-itu_metal
asset: Carla/Static/Bus/Mitsubishi_FusoRosa/SM_SC_FusoRosa.uasset
```

Existing geometry inspection artifacts:

```text
tmp/episode_000051_car40_mesh_visual_20260507/car40_mesh_multiview_triangles.png
tmp/episode_000051_car40_mesh_visual_20260507/car40_mesh_boundary_edges.png
tmp/episode_000051_car40_mesh_visual_20260507/car40_mesh_quality_stats.png
tmp/episode_000051_car40_mesh_visual_20260507/mesh_summary.json
```

New Mitsuba/Sionna-environment render artifacts:

```text
tmp/render_car40_mitsuba_views.py
tmp/render_car40_mitsuba_lit.py
tmp/episode_000051_car40_mesh_visual_20260507/mitsuba_lit_views/car40_mitsuba_lit_contact_sheet.png
tmp/episode_000051_car40_mesh_visual_20260507/mitsuba_lit_views/car40_mitsuba_lit_*.png
```

Mesh stats from `mesh_summary.json`: 436 PLY vertex records, 412 faces, 216
unique rounded vertex positions, no degenerate triangles, after welding duplicate
positions boundary edges = 0 and non-manifold edges = 0, 5 closed connected
components. Interpretation: the mesh does not look grossly corrupt; raw duplicate
vertices are likely game mesh seams/submesh splits. It is a low-poly but detailed
bus/fusorosa shape with slanted/beveled body panels and wheels, which can create
geometry-dependent specular/multipath behavior.






## MultiScene20 Failure-Rate Analysis 2026-05-08 17:30 CST

Budget-1000 collection is still running at this analysis point. Current progress:

```text
accepted_total: 1540 / 3000
remaining_total: 1460
completed_scene_count: 7 / 20
failed_attempt_total: 16417
```

Current run since the 2026-05-08 11:37 restart added 52 accepted trajectories
while failed attempts increased by roughly 4.2k. Marginal acceptance rate is
about 1.2%, so progress is real but inefficient. This is worse than the earlier
high-budget overnight segment, which added 258 accepted trajectories.

Worst per-scene acceptance rates among scenes with >=100 failed attempts:

```text
town04_opt_junction_0483: 1 accepted / 2800 failed, acc_rate ~= 0.04%
town01_opt_junction_0110: 64 accepted / 4004 failed, acc_rate ~= 1.57%
town05_opt_junction_0053: 96 accepted / 3790 failed, acc_rate ~= 2.47%
town05_opt_junction_1882: 96 accepted / 2591 failed, acc_rate ~= 3.57%
town10_junction_0895: 150 accepted / 1788 failed, acc_rate ~= 7.74%, now complete
```

Sampled failure-code histograms show these are mostly not just CARLA startup
crashes; they are plan/trajectory QA failures:

```text
town04_opt_junction_0483 sample: vehicle_building_collision, vehicle_vehicle_collision
town01_opt_junction_0110 sample: vehicle_vehicle_collision, required_primary_not_moving_enough
town05_opt_junction_0053 sample: vehicle_vehicle_collision
town05_opt_junction_1882 sample: vehicle_vehicle_collision, long_all_stop
town10_junction_0895 sample: traffic_plan_spawn_incomplete, vehicle_vehicle_collision
```

Interpretation: the runner is functioning and the high restart budget prevents
CARLA from stopping immediately, but some selected scenes/plans are pathological
for the current traffic/QA setup. In particular `town04_opt_junction_0483` is
near-zero-yield and will likely consume excessive time if brute-forced.
Recommended next decision if throughput matters: quarantine/replace or cap
`town04_opt_junction_0483`, and consider capping other sub-4% scenes rather than
letting one worker spend days on them. Do not change QA/router semantics unless
explicitly approved.

## MultiScene20 Live Status 2026-05-08 17:24 CST

Budget-1000 CARLA collection is still running.

```text
PID/PGID: 101551
log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers_gpu1_40tx_budget1000_20260508_113735.log
status snapshot: datasets/DynamicRadioMap/MultiScene20/indexes/current_live_status_20260508_172435.json
accepted_total: 1540 / 3000
remaining_total: 1460
completed_scene_count: 7 / 20
failed_attempt_total: 16371
attempt_dir_total: 17915
```

Since the 2026-05-08 11:37 restart, accepted count increased from 1488 to 1540
(+52). Active/recent collection scenes include:

```text
town01_opt_junction_0110: 64 / 150
town05_opt_junction_0053: 96 / 150
town04_opt_junction_0483: 1 / 150
town05_opt_junction_1882: 96 / 150
```

RPC health at snapshot: ports 2100, 2120, 2130 answered `get_world()`; port
2110 was between restart cycles / socket refused at the instant of check. The
process remains alive and the high restart budget is expected to keep cycling
through CARLA crashes.

## MultiScene20 Repair and Budget-1000 Resume 2026-05-08 11:40 CST

User requested repairing missing metadata and continuing collection. No Sionna
RT/RF was run.

Repair performed before restart:

```text
validation_report repair: datasets/DynamicRadioMap/MultiScene20/indexes/validation_report_repair_20260508_113136.json
tx assignment repair: datasets/DynamicRadioMap/MultiScene20/indexes/tx_assignment_repair_20260508_113700.json
```

Repair result:

```text
accepted_total before restart: 1488 / 3000
missing validation_report.json after repair: 0
missing tx_assignment.json after repair: 0
validation reports reconstructed: 30
tx assignments added: 96
trajectory QA for repaired validation reports: pass
```

Collection was relaunched with a larger per-scene CARLA restart budget:

```text
PID/PGID: 101551
max_carla_restarts_per_scene: 1000
log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers_gpu1_40tx_budget1000_20260508_113735.log
status snapshot: datasets/DynamicRadioMap/MultiScene20/indexes/current_collection_status_budget1000_20260508_114042.json
command includes: --carla-workers 4 --gpu-id 1 --rpc-ports 2100,2110,2120,2130 --tm-ports 8100,8110,8120,8130 --target-accepted-per-scene 150 --max-carla-restarts-per-scene 1000
```

At 11:40 CST the main process was running, all four CARLA RPC ports answered
`get_world()`, and four collect subprocesses were active on scenes:

```text
town01_opt_junction_0110
town05_opt_junction_0053
town04_opt_junction_0483
town05_opt_junction_1882
```

Progress at restart/status snapshot:

```text
accepted_total: 1488 / 3000
remaining_total: 1512
completed_scene_count: 7 / 20
```

## MultiScene20 Status 2026-05-08 11:06 CST

The high-budget 4-worker CARLA run launched on 2026-05-07 16:06 is no longer
running. Current process and port checks show no `collect-multi-scene`, no CARLA
collect subprocesses, and no listeners on worker RPC/TM ports
2100/2110/2120/2130 or 8100/8110/8120/8130. `last -x` shows the host rebooted
at 2026-05-08 08:46 CST. There is no top-level
`indexes/multi_scene_collection_summary.json`, so the run did not exit through
the normal summary path.

Current status snapshot:

```text
datasets/DynamicRadioMap/MultiScene20/indexes/current_status_20260508_110629.json
datasets/DynamicRadioMap/MultiScene20/indexes/current_status_latest.json
```

Progress:

```text
accepted_total: 1488 / 3000
remaining_total: 1512
completed_scene_count: 7 / 20
failed_attempt_total: 12163
attempt_dir_total: 13652
```

Completed scenes at 150 accepted trajectories:

```text
town04_opt_junction_1061
town10_junction_0532
town10_junction_0895
town04_opt_junction_1593
town04_opt_junction_1176
town05_opt_junction_0562
town05_opt_junction_0396
```

High-budget run added 258 accepted trajectories compared with the 1230 count at
2026-05-07 16:10. Main gains were: town05_opt_junction_0396 +95,
town05_opt_junction_1882 +70, town10_junction_0895 +37,
town05_opt_junction_0053 +25, town01_opt_junction_0110 +18,
town05_opt_junction_0562 +12, town04_opt_junction_0483 +1.

Per-scene counts at 11:06 CST:

```text
town01_opt_junction_0110       45 / 150
town02_opt_junction_0076       31 / 150
town05_opt_junction_1722        4 / 150
town04_opt_junction_0148       25 / 150
town04_opt_junction_1368       51 / 150
town04_opt_junction_1061      150 / 150
town05_opt_junction_0053       82 / 150
town05_opt_junction_2086       56 / 150
town10_junction_0189           17 / 150
town10_junction_0719           26 / 150
town10_junction_0532          150 / 150
town10_junction_0895          150 / 150
town04_opt_junction_0483        1 / 150
town04_opt_junction_0916       12 / 150
town04_opt_junction_0785       11 / 150
town04_opt_junction_1593      150 / 150
town04_opt_junction_1176      150 / 150
town05_opt_junction_0562      150 / 150
town05_opt_junction_0396      150 / 150
town05_opt_junction_1882       77 / 150
```

Metadata scan after reboot/stop:

```text
missing validation_report.json: 30
missing tx_assignment.json: 96
missing attempt_meta.json in active attempts: 0
bad frame-count episodes: 0
trajectory QA bad episodes: 0
```

The 30 validation reports and 96 TX assignments are repairable metadata gaps
from accepted episodes collected after the previous repair and before reboot.
Frames and trajectory QA are intact. Before RF or another clean resume, rerun
validation-report reconstruction for the 30 episodes and `ensure_tx_assignments`
for the 96 missing assignments.

Worker summaries show the high-budget behavior did help but still hit CARLA
stability issues. Example: town01_opt_junction_0110 advanced from 27 to 45, then
failed with `carla_restart_budget_exhausted` after 241 restarts, last reason
`carla_startup_failed`. Worker stderr contains repeated UE4 `Signal 11`,
`Segmentation fault`, `GameThread timed out waiting for RenderThread`, and
`MoTTY X11 proxy: Authorisation not recognised`.

## MultiScene20 High-Budget Resume and Validation-Report Repair 2026-05-07 16:10 CST

The 24-restart-budget 40-TX run was stopped so the higher restart budget could
take effect. Owned/stale CARLA processes on ports 2100/2110/2120/2130 were
terminated before restart.

Budget changes:

```text
scripts/dynamic_radio_dataset/cli.py: collect-multi-scene default --max-carla-restarts-per-scene changed 12 -> 120
scripts/dynamic_radio_dataset/multi_scene/carla_parallel.py: API default changed 12 -> 120
current launched run explicitly uses --max-carla-restarts-per-scene 240
```

Metadata repair:

```text
incomplete attempts quarantined before high-budget resume: datasets/DynamicRadioMap/MultiScene20/quarantine/cleanup_incomplete_attempts_before_high_budget_resume_20260507_155946
quarantined incomplete attempt dirs: 3
validation_report repair report: datasets/DynamicRadioMap/MultiScene20/indexes/validation_report_repair_20260507_160552.json
validation_report latest: datasets/DynamicRadioMap/MultiScene20/indexes/validation_report_repair_latest.json
```

All previously missing accepted-episode `validation_report.json` files were
reconstructed from saved `frames/actor_states.jsonl`, `scene_meta.json`, and
`routes.json`. This is not fake RF or relaxed QA; it restores the same legacy
diagnostic fields (`targets`, `first_core_frame`, `passed_target_count`,
`valid_clip`) that CARLA would have written after the complete actor-state clip.
Trajectory QA was rerun for repaired episodes and remained pass. Current missing
metadata counts before high-budget resume:

```text
missing validation_report.json: 0
missing tx_assignment.json: 0
missing attempt_meta.json in active attempts: 0
```

Validation commands passed:

```bash
PYTHONPATH=scripts python3 -m py_compile scripts/dynamic_radio_dataset/cli.py scripts/dynamic_radio_dataset/multi_scene/carla_parallel.py scripts/dynamic_radio_dataset/multi_scene/config.py
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py collect-multi-scene --help
```

High-budget 4-worker CARLA collection was relaunched:

```text
PID/PGID: 456979
log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers_gpu1_40tx_highbudget_20260507_160637.log
status snapshot: datasets/DynamicRadioMap/MultiScene20/indexes/current_collection_status_highbudget_20260507_160951.json
command includes: --carla-workers 4 --gpu-id 1 --rpc-ports 2100,2110,2120,2130 --tm-ports 8100,8110,8120,8130 --target-accepted-per-scene 150 --max-carla-restarts-per-scene 240
```

At 16:09 CST the main process was running, all four CARLA RPC ports answered
`get_world()`, and collection was active. Progress at snapshot time remained:

```text
accepted_total: 1230 / 3000
remaining_total: 1770
failed_attempt_total: 2134
attempt_dir_total: 3369
```

## MultiScene20 40-TX Update and Resume 2026-05-07 15:53 CST

User requested increasing per-scene TX candidates from 20 to 40 and resuming
CARLA collection with 4 workers. No Sionna RT/RF was run.

Config/code changes:

```text
configs/dynamic_radio/multi_scene_20x150_resolved.yaml
configs/dynamic_radio/multi_scene_20x150.yaml
scripts/dynamic_radio_dataset/multi_scene/config.py
```

The active config now has:

```text
dataset.tx_candidates_per_scene: 40
tx.candidate_count: 40
tx.selected_tx_per_episode: 5
tx.placement.max_distance_to_road_m: 10.0
tx.placement.max_distance_to_scene_center_m: 48.0
```

`multi_scene/config.py` was fixed so generated per-scene configs inherit the
top-level multi-scene `tx` block, including `tx.placement`; previously only
`candidate_count`, `selected_tx_per_episode`, and `placement_seed` were passed
down, which made 40-TX placement still use old default thresholds.

40-TX artifacts:

```text
tx catalog regeneration: datasets/DynamicRadioMap/MultiScene20/indexes/tx_catalog_regenerated_40_20260507_154334.json
tx catalog latest: datasets/DynamicRadioMap/MultiScene20/indexes/tx_catalog_regenerated_40_latest.json
old assignment deletion: datasets/DynamicRadioMap/MultiScene20/indexes/delete_old_tx_assignments_for_40tx_20260507_154509.json
40-TX assignment rebuild: datasets/DynamicRadioMap/MultiScene20/indexes/tx_assignment_rebuild_40tx_20260507_154642.json
cleanup scan: datasets/DynamicRadioMap/MultiScene20/indexes/current_collection_status_scan_after_40tx_cleanup_20260507_154748.json
```

All 20 scene `scene_static/tx_catalog.json` files now contain 40 candidates. All
1230 accepted episodes at the time of rebuild have `tx_assignment.json` with 5
selected TX ids from the 40-candidate catalog. Old 20-TX assignments were deleted
and rebuilt; RF has not been run, so no RSS artifacts were invalidated.

Cleanup before resume:

```text
incomplete attempt quarantine: datasets/DynamicRadioMap/MultiScene20/quarantine/cleanup_incomplete_attempts_before_40tx_resume_20260507_154358
moved incomplete attempt dirs: 4
hard storage problem count after cleanup: 0
remaining warnings: 99 validation_report_missing accepted episodes
```

The 99 `validation_report_missing` accepted episodes were not deleted because
their actor-state frames and trajectory QA are valid; this is a known post-clip
CARLA cleanup/report gap, not trajectory corruption.

Static validation passed after the change:

```bash
PYTHONPATH=scripts python3 -m py_compile scripts/dynamic_radio_dataset/multi_scene/config.py scripts/dynamic_radio_dataset/tx/placement.py scripts/dynamic_radio_dataset/tx/assignment.py scripts/dynamic_radio_dataset/multi_scene/carla_parallel.py scripts/dynamic_radio_dataset/multi_scene/carla_server.py scripts/dynamic_radio_dataset/cli.py
python3 scripts/drd.py --help
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
```

CARLA collection was resumed with 4 workers:

```text
PID/PGID: 400042
log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers_gpu1_40tx_resume_20260507_154807.log
status snapshot: datasets/DynamicRadioMap/MultiScene20/indexes/current_collection_status_40tx_resume_20260507_155313.json
command: python3 scripts/drd.py collect-multi-scene --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --carla-workers 4 --gpu-id 1 --rpc-ports 2100,2110,2120,2130 --tm-ports 8100,8110,8120,8130 --target-accepted-per-scene 150 --max-carla-restarts-per-scene 24
```

At 15:53 CST the main process was running, all four CARLA RPC ports responded
to `get_world()`, and two collect subprocesses had started attempts. The
collection started from 1230 accepted trajectories.

## MultiScene20 Status 2026-05-07 15:15 CST

The post-reboot 4-worker CARLA-only collection launched at 2026-05-06 23:42 is
no longer running. Current process/port check shows no `collect-multi-scene`, no
CARLA collect subprocesses, and no listeners on RPC/TM ports 2100/2110/2120/2130
or 8100/8110/8120/8130.

Progress snapshot:

```text
status scan: datasets/DynamicRadioMap/MultiScene20/indexes/current_collection_status_scan_20260507_151320.json
accepted_total: 1230 / 3000
remaining_total: 1770
failed_attempt_total: 2015
attempt_dir_total: 3250
scenes completed at 150: town04_opt_junction_1061, town10_junction_0532, town04_opt_junction_1593, town04_opt_junction_1176
```

Current per-scene accepted counts:

```text
town01_opt_junction_0110       27
town02_opt_junction_0076       31
town05_opt_junction_1722        4
town04_opt_junction_0148       25
town04_opt_junction_1368       51
town04_opt_junction_1061      150
town05_opt_junction_0053       57
town05_opt_junction_2086       56
town10_junction_0189           17
town10_junction_0719           26
town10_junction_0532          150
town10_junction_0895          113
town04_opt_junction_0483        0
town04_opt_junction_0916       12
town04_opt_junction_0785       11
town04_opt_junction_1593      150
town04_opt_junction_1176      150
town05_opt_junction_0562      138
town05_opt_junction_0396       55
town05_opt_junction_1882        7
```

Read-only storage scan after the latest stop found no hard episode storage
problems: accepted episodes have required files, 100 actor-state frames, and QA
pass. Warnings remain:

```text
validation_report_missing: 99 accepted episodes
tx_assignment_missing: 194 accepted episodes
attempt_missing_meta: 4 attempt work dirs
```

These warnings are repairable/pre-RF metadata cleanup items. They are not
trajectory-frame corruption. Before the next RF step, rerun TX assignment repair
and quarantine/clear the 4 incomplete attempt dirs.

Stop reason: worker summaries and CARLA stderr show repeated UE4 `Signal 11` /
segmentation faults and `carla_server_unavailable`; some scenes exhausted their
per-scene CARLA restart budget. The host also rebooted again at 2026-05-07
10:22, but collection had already stopped around 2026-05-07 02:21 based on latest
collection summary mtimes.

## Post-Reboot MultiScene20 Recovery 2026-05-06 23:50 CST

Server/power reboot recovery was performed for the CARLA-only MultiScene20
collection. No Sionna RT/RF step was run.

Artifacts and checks:

```text
integrity scan before repair: datasets/DynamicRadioMap/MultiScene20/indexes/post_reboot_integrity_scan_20260506_233823.json
final integrity scan after repair: datasets/DynamicRadioMap/MultiScene20/indexes/post_reboot_integrity_scan_final_20260506_234156.json
quarantine report: datasets/DynamicRadioMap/MultiScene20/indexes/post_reboot_quarantine_latest.json
tx assignment repair report: datasets/DynamicRadioMap/MultiScene20/indexes/tx_assignment_repair_post_reboot_latest.json
current status snapshot: datasets/DynamicRadioMap/MultiScene20/indexes/current_collection_status_post_reboot_20260506_235044.json
```

Recovery results:

```text
accepted episodes checked before restart: 801
accepted episodes with 100/100 actor-state frames and trajectory_qc_pass=true: 801
post-repair hard storage problem count: 0
post-repair warnings: 54 accepted episodes missing validation_report.json
  - treated as known post-clip CARLA cleanup/validation-report gap, not as trajectory corruption
missing tx_assignment.json repaired: 208 episodes
incomplete/interrupted attempt dirs quarantined: 10
```

Quarantined incomplete attempt work directories were moved under:

```text
datasets/DynamicRadioMap/MultiScene20/quarantine/post_reboot_incomplete_attempts_20260506_234008
```

The CARLA-only 4-worker collection was restarted with 30s worker startup
staggering:

```text
PID/PGID: 71701
config: configs/dynamic_radio/multi_scene_20x150_resolved.yaml
log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers_gpu1_post_reboot_20260506_234224.log
command: python3 scripts/drd.py collect-multi-scene --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --carla-workers 4 --gpu-id 1 --rpc-ports 2100,2110,2120,2130 --tm-ports 8100,8110,8120,8130 --target-accepted-per-scene 150 --max-carla-restarts-per-scene 24
```

Latest observed status at 23:50 CST:

```text
main collect-multi-scene process: running
accepted total: 818 / 3000
remaining accepted trajectories: 2182
failed attempts: 505
attempt dirs: 1328
scenes at target: town10_junction_0532 reached 150
replacement scenes not yet collected: town05_opt_junction_1722=0, town04_opt_junction_0483=0
```

CARLA workers are running but still show the expected crash/restart churn under
load: status snapshots may catch one RPC port between restart cycles. The
multi-scene runner is consuming the per-scene CARLA restart budget and continuing
instead of requiring manual restart.

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

## Active MultiScene20 CARLA Collection 2026-05-06

The pre-Sionna CARLA-only MultiScene20 collection was launched with:

```text
config: configs/dynamic_radio/multi_scene_20x150_resolved.yaml
dataset root: datasets/DynamicRadioMap/MultiScene20
command: python3 scripts/drd.py collect-multi-scene --carla-workers 4 --gpu-id 1 --rpc-ports 2100,2110,2120,2130 --tm-ports 8100,8110,8120,8130 --target-accepted-per-scene 150
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers.pid
pgidfile: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers.pgid
logs: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers_gpu1_*.log
plan: datasets/DynamicRadioMap/MultiScene20/indexes/multi_scene_collection_plan.json
```

Stop command if a future run is active with the same pidfile/pgidfile:

```bash
PGID=$(cat datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers.pgid)
kill -TERM -${PGID}
```

The runner sets CUDA/NVIDIA/PRIME/Vulkan hints and starts CARLA with
`-graphicsadapter=1`, but `nvidia-smi pmon` still reported the CARLA graphics
contexts on GPU0 on this host. Treat `--gpu-id 1` as best-effort unless the
host graphics stack is reconfigured.

Known collection blocker: two selected corridor scenes have empty accepted plan
catalogs and will not reach 150 accepted trajectories without a later plan/router
decision:

```text
town02_opt_corridor_0099: accepted_plan_count=0, rejection=insufficient_primary_controlled_routes
town02_opt_corridor_0078: accepted_plan_count=0, rejection=insufficient_primary_controlled_routes
```

This is a critical preflight gap: selected-scene validation verified
reference/scene_static/route_library/tx_catalog generation, but did not fail a
scene whose `plan_catalog/plans.jsonl` had zero accepted plans. Do not describe
MultiScene20 as fully collection-ready until these two scenes are replaced or a
deliberate corridor-specific plan policy is approved.

Mitigation added: future multi-scene scene preparation now fails with
`failure_code=plan_catalog_empty` when `generate_plan_bank` produces zero
accepted plans. A provisional supplemental replacement pair has been selected
from the discovered candidate catalog:

```text
config: configs/dynamic_radio/multi_scene_replacements_2x150.yaml
manifest: configs/dynamic_radio/selected_scene_manifest_replacements_2.yaml
selection report: datasets/DynamicRadioMap/MultiScene20/indexes/replacement_scene_selection_report.json
selected replacement candidates:
  town05_opt_junction_1722: Town05_Opt, route_count=16, left/right/straight=4/4/8
  town04_opt_junction_0483: Town04_Opt, route_count=12, left/right/straight=4/4/4
status: provisional; still requires program prepare + plan_catalog accepted_plan_count > 0 before collection
```

Note: the strict unused `backup_candidate_ids` from
`selected_scene_manifest_20.yaml` did not contain two reliable unused
plan-ready scenes: many were already in the current resolved config, previous
Town03/CARLA prep failures, or one-route corridors with plan-empty risk. The
two replacements are therefore selected from the same discovery candidate
catalog as extended backups.

Clarification: the two plan-empty corridor scenes were not made collectable by
changing router/traffic-control policy. They are now explicitly failed/skipped
as `plan_catalog_empty`, and the replacement config above is the current path to
recover the missing two 150-trajectory scene quotas.

Cleanup at user request: the two plan-empty corridor scenes have now been
removed from the active 20-scene config and replaced in
`configs/dynamic_radio/multi_scene_20x150_resolved.yaml` by:

```text
town05_opt_junction_1722
town04_opt_junction_0483
```

The active selected manifest was also updated. Generated artifacts for the old
corridor scenes were removed with `rm -rf` from:

```text
datasets/DynamicRadioMap/MultiScene20/scenes/town02_opt_corridor_0099
datasets/DynamicRadioMap/MultiScene20/scenes/town02_opt_corridor_0078
datasets/DynamicRadioMap/MultiScene20/diagnostics/carla_topdown_rgb/captures/town02_opt_corridor_0099
datasets/DynamicRadioMap/MultiScene20/diagnostics/carla_topdown_rgb/captures/town02_opt_corridor_0078
```

Their preview PNG/JSON and stale generated reports/logs referencing them were
also removed. The raw discovery catalog and replacement-selection report remain
as provenance records. A post-cleanup dry-run regenerated
`indexes/multi_scene_collection_plan.json` with 20 scenes and no references to
the removed corridor scenes. The two replacement scene directories are not
prepared yet, so the dry-run currently shows `plan_count=0` for them until
`prepare-multi-scene` is run for the replacements.

An accidental overlapping first launch briefly created attempts/episodes in four
scenes. Those artifacts were moved, not deleted, to:

```text
datasets/DynamicRadioMap/MultiScene20/quarantine/parallel_start_overlap_20260506_160650
```

Latest monitor at 2026-05-06 16:13 CST:

```text
main pid: 4058431, pgid: 4058429, status: running
CARLA RPC ports listening: 2100, 2110, 2120, 2130
selected-scene totals: attempts=67, failed_attempts=11, episodes=52, accepted=52
active collection scenes so far:
  town01_opt_junction_0110: accepted=4, failed_attempts=9
  town04_opt_junction_1061: accepted=14, failed_attempts=1
  town10_junction_0532: accepted=17, failed_attempts=0
  town04_opt_junction_1593: accepted=17, failed_attempts=1
```

Latest monitor at 2026-05-06 16:29 CST:

```text
main pid: 4058431, status: running
selected-scene totals: attempts=263, failed_attempts=71, episodes=189, accepted=189
artifact root: datasets/DynamicRadioMap/MultiScene20
accepted trajectories are under scenes/<scene_id>/episodes/episode_*
failed collection attempts are under scenes/<scene_id>/failed_attempts/attempt_*
active attempt work dirs are under scenes/<scene_id>/attempts/attempt_*
```

Manual stop and fix at 2026-05-06 16:45 CST:

```text
previous main pid: 4058431, stopped by user request
worker CARLA ports 2100/2110/2120/2130: stopped and released
selected-scene totals at stop: attempts=442, failed_attempts=119, episodes=320, accepted=320
stop report: datasets/DynamicRadioMap/MultiScene20/indexes/current_collection_stopped_report.json
```

Issue fixed after stop: per-scene collection had inherited the single-scene
Town10 `collection.bucket_targets` of 75 per vehicle bucket, so
`collect_from_plan_catalog` reported `target_accepted=300` for scenes despite
the multi-scene CLI being launched with `--target-accepted-per-scene 150`. The
multi-scene config builder and parallel runtime now clear inherited
`bucket_targets`, `bucket_order`, and `selection_matrix`, and both multi-scene
collection paths pass `bucket_targets={}` explicitly. Dry-run now shows
`target_accepted=150` per scene. Existing accepted trajectory artifacts remain
usable, but the stopped run did not have the intended stop semantics.

CARLA restart fix: worker restart now terminates stale owned CARLA before
starting another server on the same port and waits briefly for the port to
release. This addresses repeated UE4 `Signal 11` / `bind: Address already in
use` loops observed in worker logs.

New guardrail: `check-contract` now constructs sample resolved multi-scene
configs and fails if they inherit single-scene `bucket_targets` or
`selection_matrix`.

Resume after manual cleanup/QC at 2026-05-06 17:25 CST:

```text
all old CARLA / collect-multi-scene processes were terminated before restart
worker RPC/TM ports checked clear before restart: 2000, 2100, 2110, 2120, 2130, 8100, 8110, 8120, 8130
pre-resume visual QC artifacts:
  summary: datasets/DynamicRadioMap/MultiScene20/diagnostics/trajectory_visual_qc/trajectory_qc_summary.json
  contact sheet: datasets/DynamicRadioMap/MultiScene20/diagnostics/trajectory_visual_qc/contact_sheet_selected20_sampled_tracks.png
pre-resume QC result:
  accepted episodes scanned: 320
  trajectory_qc_pass: all true
  frame count: all 100/100
  episode problem histogram: empty
  scenes with no accepted episodes yet: town02_opt_corridor_0099, town10_junction_0719, town02_opt_corridor_0078, town05_opt_junction_1882
  visual sampled contact sheet showed plausible route-following tracks; no obvious collision/teleport artifacts were seen
current resumed command:
  PYTHONPATH=scripts TMPDIR=/share1/fzj/tmp CUDA_VISIBLE_DEVICES=1 NVIDIA_VISIBLE_DEVICES=1 ... python3 scripts/drd.py collect-multi-scene --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --carla-workers 4 --gpu-id 1 --rpc-ports 2100,2110,2120,2130 --tm-ports 8100,8110,8120,8130 --target-accepted-per-scene 150
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers.pid
pgidfile: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers.pgid
log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers_gpu1_fixed_20260506_172511.log
latest status snapshot: datasets/DynamicRadioMap/MultiScene20/indexes/current_collection_resume_status.json
latest observed totals shortly after restart: episodes=344, attempt_dirs=479, failed_attempts=129
```

The resumed run is producing new accepted episodes. One worker-side CARLA
instance on port 2130 restarted after a CARLA client timeout during an attempt;
the multi-scene restart path relaunched the server and continued. The top-level
nohup log is currently sparse/empty because the parent Python process is
buffered; per-attempt `carla_stdout.log` / `carla_stderr.log` files and the
status JSON above are the reliable monitors for this run.

Latest monitor at 2026-05-06 20:45 CST:

```text
collect-multi-scene main process: not running
worker CARLA RPC/TM ports 2100/2110/2120/2130 and 8100/8110/8120/8130: no listeners
summary: datasets/DynamicRadioMap/MultiScene20/indexes/multi_scene_collection_summary.json
status: partial_or_failed
elapsed_s before stop/failure: 3284.6
selected-scene accepted total: 593 / 3000 target rows before RF
status snapshot: datasets/DynamicRadioMap/MultiScene20/indexes/current_collection_status_20260506_2045.json
```

The run stopped around 18:20 CST rather than continuing unattended. The summary
shows worker/scene blockers dominated by `carla_server_unavailable` after CARLA
server exits/restarts, plus worker startup failures with exit code 139/1. No
scene reached 150 accepted trajectories. The two known corridor scenes still
have `plan_count=0` and remain unable to contribute until replaced or a corridor
plan policy is intentionally added.

Root-cause follow-up and fix at 2026-05-06 later CST:

```text
CARLA worker logs show repeated UE4 Signal 11 crashes, render-thread timeout,
and "bind: Address already in use" while restarting on the same RPC ports.
Primary code issue: collect-multi-scene only retried once after
carla_server_unavailable. If the second collect also lost CARLA, the worker
marked the scene target_not_reached and advanced; later startups could race
with stale CARLA wrapper/UE4 processes or related ports still bound.
```

Fix applied in `scripts/dynamic_radio_dataset/multi_scene/carla_parallel.py`
and `scripts/dynamic_radio_dataset/multi_scene/carla_server.py`:

- scene collection now loops through CARLA restart/re-collect cycles until the
  scene reaches target, a non-CARLA blocker appears, or
  `--max-carla-restarts-per-scene` is exhausted;
- startup and restart failures now consume the same per-scene restart budget
  instead of immediately failing the scene after a single startup crash burst;
- CARLA process/port management is split into the small `multi_scene/carla_server.py`
  module, keeping the parallel collector under 500 lines;
- stale CARLA processes matching the worker RPC port are killed before restart;
- stale CARLA listener PIDs on the worker RPC/RPC+1/RPC+2/TM ports are also
  killed when their command is a CARLA process;
- restart waits for RPC, RPC+1, RPC+2, and Traffic Manager ports to be clear;
- plan-empty scenes are marked `plan_catalog_empty` and skipped instead of
  pretending to collect.

CLI now exposes:

```bash
python3 scripts/drd.py collect-multi-scene ... --max-carla-restarts-per-scene 12
```

Replacement preparation and rerun at 2026-05-06 21:43 CST:

```text
replacement config: configs/dynamic_radio/multi_scene_replacements_2x150.yaml
active collection config: configs/dynamic_radio/multi_scene_20x150_resolved.yaml
replacement scenes now present in active config:
  town05_opt_junction_1722
  town04_opt_junction_0483
replaced plan-empty corridor scenes:
  town02_opt_corridor_0099
  town02_opt_corridor_0078
```

Both replacement scenes were prepared successfully before restarting collection:

```text
town05_opt_junction_1722: reference_scene ok, Sionna export manifest ok, tx_count=20, plan_count=1200
town04_opt_junction_0483: reference_scene ok, Sionna export manifest ok, tx_count=20, plan_count=1200
prepare log: datasets/DynamicRadioMap/MultiScene20/logs/prepare_replacements_2x150_20260506_212737.log
```

Operational fix added before the rerun: `multi_scene/carla_parallel.py` now
staggers initial worker startup by `DRD_CARLA_WORKER_START_STAGGER_S` seconds
per worker, default 15s. The immediate 4-worker launch without staggering left
three UE4 instances listening but unresponsive to `get_world()`; the staggered
rerun uses 30s and reached four active `dynamic_radio_dataset.carla.collect`
processes.

Current collection run:

```text
pidfile: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers.pid
pgidfile: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers.pgid
log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers_gpu1_replacements_stagger_20260506_213703.log
command: python3 scripts/drd.py collect-multi-scene --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --carla-workers 4 --gpu-id 1 --rpc-ports 2100,2110,2120,2130 --tm-ports 8100,8110,8120,8130 --target-accepted-per-scene 150 --max-carla-restarts-per-scene 24
extra env: DRD_CARLA_WORKER_START_STAGGER_S=30, CUDA/NVIDIA gpu hints set to 1
status snapshot: datasets/DynamicRadioMap/MultiScene20/indexes/current_collection_status_20260506_2143.json
latest observed selected-scene accepted total: 621 / 3000
active collect subprocesses at snapshot: 4
```

Latest monitor at 2026-05-06 21:45 CST:

```text
collect-multi-scene main pid: 289562, running
active CARLA collect subprocesses: 4
CARLA health: ports 2100, 2110, 2120, 2130 all responded to get_world()
selected-scene accepted total: 635 / 3000
remaining accepted trajectories: 2365
status snapshot: datasets/DynamicRadioMap/MultiScene20/indexes/current_collection_status_20260506_2145.json
```

Current active scenes at the snapshot were still early in each worker chunk:
worker 0 on `town01_opt_junction_0110`, worker 1 on
`town04_opt_junction_1061`, worker 2 on `town10_junction_0532`, worker 3 on
`town04_opt_junction_1593`. The two replacement scenes are prepared and have
1200 plans each, but have not started collection yet because they appear later
in their worker chunks:

```text
town05_opt_junction_1722: 0 accepted so far, 1200 plans
town04_opt_junction_0483: 0 accepted so far, 1200 plans
```

Latest attempt/vehicle distribution snapshot at 2026-05-06 21:51 CST:

```text
snapshot: datasets/DynamicRadioMap/MultiScene20/indexes/current_attempt_vehicle_distribution_latest.json
finished attempts: 1033
accepted episodes: 661
failed attempts: 372
active collect subprocesses at previous health check: 4
accepted planned vehicle_count histogram: 4=396, 5=88, 6=88, 7=89
accepted actual vehicle_count histogram: 3=5, 4=391, 5=88, 6=88, 7=89
accepted target/actual large-vehicle histogram: 0=142, 1=342, 2=177
failed top codes: vehicle_vehicle_collision=210, traffic_plan_spawn_incomplete=55,
  carla_subprocess_failed=38, carla_rpc_timeout=20
```

Vehicle-count policy: each scene plan catalog has 1200 plans with
`traffic.vehicle_count_mix={4:0.25,5:0.25,6:0.25,7:0.25}`, yielding 300 plans
per requested total vehicle count. Each plan has 2 required primary controlled
vehicles plus 1 optional auxiliary controlled vehicle; Traffic Manager
background requested count is therefore `vehicle_count - 3`. Large-vehicle
targets are from `plans.large_vehicle_count_mix_by_vehicle_count` in the base
config, with `vehicle.mitsubishi.fusorosa` as the large type and at most one
controlled large vehicle.

Stop command if needed:

```bash
kill -TERM -$(cat datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_4workers.pgid)
```

Static checks after the startup-stagger code change:

```text
PYTHONPATH=scripts python3 -m py_compile scripts/dynamic_radio_dataset/multi_scene/carla_parallel.py scripts/dynamic_radio_dataset/multi_scene/carla_server.py scripts/dynamic_radio_dataset/cli.py
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

All passed.

Validation after the fix passed:

```text
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name '*.py')
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py collect-multi-scene --help
python3 scripts/drd.py --help
python3 scripts/drd.py collect-multi-scene --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --carla-workers 4 --gpu-id 1 --rpc-ports 2100,2110,2120,2130 --tm-ports 8100,8110,8120,8130 --target-accepted-per-scene 150 --dry-run
```

After validation, no `collect-multi-scene` or worker CARLA processes were
running and no listeners were found on RPC/TM ports
2100/2110/2120/2130/8100/8110/8120/8130. Do not assume this means the dataset
is complete; the last completed collection summary remains `partial_or_failed`
with 593 accepted trajectories.

## Current Code Shape

RF responsibilities have been split without changing the formal CLI or dataset
schema:

- `pipeline/stages.py`: stage orchestration, finalize, verify/prune release.
- `rf/processing.py`: trajectory-QA gate, RF completeness checks, GPU/worker
  episode scheduling, `rf_failure_summary.json`.
- `rf/runtime.py`: shared Sionna Python/runtime/cache environment.
- `rf/rss_compute.py`: formal Sionna RSS numeric generation that writes
  `rss_maps.npz`, `frame_stats.jsonl`, and `rss_heatmap_meta.json` without
  PNG/MP4 rendering.
- `rf/episode_job.py`: single-episode export/RSS/traffic-grid/QA artifact job.
- `rf/artifacts.py`, `rf/scene_static.py`: RF artifact IO and scene-static/TX
  search helpers.
- `geometry/`, `raster/`, `qa/metrics.py`, `qa/rf_policy.py`: extracted
  geometry, rasterization, metric, and RF QA policy helpers.
- `indexing/finalize.py`, `indexing/episode_index.py`: index/split API used
  by `pipeline/stages.py` and the compatibility CLI.
- `pipeline/process_rf.py`: compatibility CLI for `prepare-scene`,
  `process-episode`, and `finalize-index`.
- `diagnostics/dataset_scan.py`, `diagnostics/histograms.py`,
  `diagnostics/failure_report.py`: read-only artifact scanning and failure
  diagnostics.
- `multi_scene/carla_parallel.py`: scene-level CARLA-only multi-scene collector
  with one CARLA RPC port and Traffic Manager port per worker. It does not run
  Sionna/RF.
- `render/rss_video.py`: cached RSS visualization/review path. Fresh RSS
  computation there remains temporarily compatible but prints a deprecation
  warning; formal RF now calls `dynamic_radio_dataset.rf.rss_compute`.
- `testing/harness.py`: optional scratch RF assertions.

Static checks after the module-boundary refactor passed on 2026-05-03:

```text
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.rf.rss_compute --help
```

The formal root was also scanned through the refactored read-only report code:

```text
python3 scripts/drd.py failure-report --config configs/dynamic_radio/dynamic_radiomap_town10_300.yaml --max-examples 1
python3 scripts/drd.py timing-report --config configs/dynamic_radio/dynamic_radiomap_town10_300.yaml
```

These commands rewrote diagnostics/timing JSON files under the ignored formal
dataset root only; they did not run CARLA or Sionna and did not change QA
decisions.

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
job was running under `datasets/refactor_20ep_dual_gpu_20260503`. That scratch
RF job was later stopped after the user decided the validation data collected
so far was sufficient; only Git metadata, documentation, and scratch dataset
artifacts were touched.

## Scratch Validation 2026-05-03

User-requested validation scratch config:

```text
config: configs/dynamic_radio/town10_junction_10s_4to7_refactor_20_dualgpu.yaml
root: datasets/refactor_20ep_dual_gpu_20260503
selection manifest: plan_catalog/refactor_selection_20.json
RF command: python3 scripts/drd.py process-rf --config <config> --use-gpu --gpu-ids 0,1 --rf-workers 2
```

Static checks passed before the run:

```text
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
CARLA env import check for numpy/carla/networkx
```

Scratch collection reached the requested 20 trajectory-QA accepted episodes:

```text
attempted: 25
trajectory accepted: 20
trajectory rejected: 5, failure_code=vehicle_vehicle_collision
stopped_reason: selection_targets_reached
vehicle_count histogram: 4=5, 5=5, 6=5, 7=5
target/actual large vehicle histogram: 0=3, 1=7, 2=6, 3=4
background shortfall episodes: 0
vehicle/large mismatch episodes: 0
selection matrix targets reached: true
```

Dual-GPU RF was confirmed to run on both GPU slots. After the user said the
current validation data was sufficient, the scratch RF process group was
stopped instead of completing all 20 RF episodes or running finalize:

```text
complete rss_dynamic_dbm.npz episodes: 14/20
complete shapes checked: all (3, 100, 128, 128)
tx_*/rss_maps.npz files: 43
rf_process_meta.json files: 16
complete RF episodes: 000000, 000001, 000002, 000003, 000004, 000005,
  000006, 000007, 000009, 000011, 000013, 000015, 000017, 000019
partial RF metadata episodes: 000008, 000010
recorded gpu_id values in RF metadata: GPU 0 = 6, GPU 1 = 10
rf_failure_summary.json: absent
finalize: not run for this scratch root
```

No scratch RF process remained after stopping. CARLA on port 2000 was left
running.

Clean two-episode GPU refactor smoke was then completed using a scratch root:

```text
config: configs/dynamic_radio/town10_junction_10s_4to7_refactor_2ep_gpu.yaml
root: datasets/refactor_2ep_gpu_20260503
RF: cuda_ad_rgb, use_gpu=true, gpu_ids=0,1, rf_workers=2
process-rf summary: candidates=2, queued=2, processed=2, failed=0
```

Validation commands passed:

```text
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.testing.harness --config configs/dynamic_radio/town10_junction_10s_4to7_refactor_2ep_gpu.yaml --expected-episodes 2 --expected-gpu-ids 0,1
python3 scripts/drd.py finalize --config configs/dynamic_radio/town10_junction_10s_4to7_refactor_2ep_gpu.yaml
python3 scripts/drd.py verify-release --config configs/dynamic_radio/town10_junction_10s_4to7_refactor_2ep_gpu.yaml --expected-episodes 2
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

Smoke results:

```text
accepted trajectory episodes: 2
rss_dynamic_dbm.npz shape: (3, 100, 128, 128)
complete per-TX rss_maps.npz: 6/6
rf_process_meta use_gpu: true for both episodes
GPU coverage: GPU 0 = 1 episode, GPU 1 = 1 episode
episode_index.jsonl rows after finalize: 6
verify-release accepted episodes: 2
verify-release episode-TX rows: 6
post-finalize process-rf rerun: skipped 2/2 complete episodes, queued=0
```

This scratch validation did not touch the formal release root. Formal release
RF recovery is still pending for `datasets/DynamicRadioMap/Town10`.

On 2026-05-03, `rf/rss_compute.py` was cleaned so the formal RSS numeric
compute module no longer carries visualization-only CLI/config/metadata fields
such as `--plot-style`, `--display-mode`, colormap, output video, visual
smoothing, marker, and render color options. The existing
`rss_heatmap_meta.json` filename is retained for artifact compatibility, but
new metadata from `rf/rss_compute.py` no longer writes `visualization`,
`output_video`, or `reuse_rss_dir` keys. `rf/episode_job.py` no longer passes
visualization parameters to numeric RF compute.

Clean two-episode GPU verification after this cleanup used:

```text
config: configs/dynamic_radio/town10_junction_10s_4to7_rss_compute_cleanup_verify.yaml
root: datasets/rss_compute_cleanup_verify_20260503
RF: cuda_ad_rgb, use_gpu=true, gpu_ids=0,1, rf_workers=2
process-rf summary: candidates=2, queued=2, processed=2, failed=0
```

Validation commands passed:

```text
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.rf.rss_compute --help
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.testing.harness --config configs/dynamic_radio/town10_junction_10s_4to7_rss_compute_cleanup_verify.yaml --expected-episodes 2 --expected-gpu-ids 0,1
python3 scripts/drd.py finalize --config configs/dynamic_radio/town10_junction_10s_4to7_rss_compute_cleanup_verify.yaml
python3 scripts/drd.py verify-release --config configs/dynamic_radio/town10_junction_10s_4to7_rss_compute_cleanup_verify.yaml --expected-episodes 2
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

Smoke results:

```text
rss_dynamic_dbm.npz shape: (3, 100, 128, 128) for both episodes
complete per-TX rss_maps.npz: 6/6
GPU coverage: GPU 0 = 1 episode, GPU 1 = 1 episode
episode_index.jsonl rows after finalize: 6
verify-release accepted episodes: 2, episode-TX rows: 6
new rss_heatmap_meta.json files contain no visualization/output_video/reuse_rss_dir keys
```

The cleanup verification re-ran Sionna/GPU rather than copying RSS outputs.
Frame indices and shapes matched the previous scratch baseline. One episode
matched to only float-roundoff scale; the other showed sparse ray/floor-boundary
differences on GPU recomputation, so do not treat GPU reruns as bitwise
deterministic evidence. The functional RF artifact, QA, finalize, and release
verification gates passed.

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

## Offline QA Audit 2026-05-03

Second-stage offline QA/failcase audit tooling was added outside the formal
pipeline under:

```text
scripts/qa_audit/
  README.md
  audit_dataset.py
  schemas.py
  utils.py
```

The tool is read-only for existing dataset artifacts. It scans accepted
episodes, attempts, failed attempts, collection summaries, RF failure
summaries, QA reports, and `frames/actor_states.jsonl`, then writes diagnostics
only under `datasets/DynamicRadioMap/Town10/diagnostics/qa_audit_<timestamp>/`.
It does not import or modify `scripts/dynamic_radio_dataset/`, does not rerun
CARLA/Sionna/RF, and marks map-dependent lane/offroad metrics unavailable.

Verification completed:

```text
python3 -m py_compile scripts/qa_audit/*.py
python3 scripts/qa_audit/audit_dataset.py --dataset-root datasets/DynamicRadioMap/Town10
```

Latest output:

```text
datasets/DynamicRadioMap/Town10/diagnostics/qa_audit_20260503_150319/
  summary.md
  summary.json
  tables/failure_cases.csv
  tables/accepted_episode_metrics.csv
  tables/actor_behavior_metrics.csv
  tables/route_failure_rates.csv
  tables/bucket_failure_rates.csv
  samples/suspicious_accepted_cases.json
  samples/top_failure_routes.json
  samples/missing_or_partial_records.json
```

Headline scan results from that report:

```text
accepted episode dirs: 300
failure case rows: 622
  attempt/trajectory/CARLA rows: 584
  RF rows: 38
  per-TX QA rows: 0
top failure codes: vehicle_vehicle_collision=333,
  required_primary_not_moving_enough=173,
  carla_subprocess_failed=76,
  rf_worker_failed=38
controlled-vs-background evidence: mixed_or_inconclusive
```

Two attempt directories were classified as `unknown` partial residue because
they had plan/log artifacts but no clear status/failure metadata. They are
listed in `samples/missing_or_partial_records.json` and `failure_cases.csv`.

## Next Work

1. Diagnose the 38 RF failures in `datasets/DynamicRadioMap/Town10` with
   `failure-report` and targeted episode stderr/stdout reproduction.
2. Resume RF only after confirming the failure mode; completed RF episodes
   should be skipped by completeness checks.
3. Run `finalize` and `verify-release` after all 300 episodes have complete RF
   artifacts.
4. Prune the release root only after `verify-release` passes.

## RF Resume 2026-05-06 CST

User requested regenerating failed RF samples without code changes and using only one GPU because GPUs may be shared. A resume run was started for the formal Town10 dataset:

```text
config: configs/dynamic_radio/dynamic_radiomap_town10_300.yaml
root: datasets/DynamicRadioMap/Town10
command: PYTHONNOUSERSITE=1 /share1/fzj/miniconda3/envs/carla0915/bin/python scripts/drd.py process-rf --config configs/dynamic_radio/dynamic_radiomap_town10_300.yaml --use-gpu --gpu-ids 1 --rf-workers 1
pidfile: datasets/DynamicRadioMap/Town10/rf_resume_gpu1.pid
active pid at start: 3852348
log: datasets/DynamicRadioMap/Town10/rf_resume_gpu1_20260506_104147.log
```

GPU choice at launch: GPU 0 was busy, GPU 1 was mostly idle, so the run uses only GPU 1 with one worker. An initial attempt using system `python3` failed immediately with `ModuleNotFoundError: No module named 'numpy'`; the real run above was then started with the `carla0915` Python. No code changes were made.

Initial observations from the resume log:

- The run skips RF-complete episodes and retries incomplete trajectory-QA accepted episodes.
- Several early incomplete episodes such as `episode_000014` through `episode_000020` fail before RSS because their episode scene signature has `num_valid_routes=16` / `turn_types=['left','right','straight']`, while the prepared fixed scene expects `num_valid_routes=14` / `turn_types=['right','straight']`.
- The run continued after those failures and started real GPU RSS generation for later episodes. `episode_000261/tx_00/rss_maps.npz` was written successfully while monitoring.

Monitor with:

```bash
PID=$(cat datasets/DynamicRadioMap/Town10/rf_resume_gpu1.pid)
ps -p "$PID" -o pid,ppid,stat,etime,cmd
pgrep -P "$PID" -a
tail -f datasets/DynamicRadioMap/Town10/rf_resume_gpu1_20260506_104147.log
```

## Multi-Scene Preview/TX Implementation 2026-05-06 CST

Implemented the first multi-scene preview-selection and selected-TX RF plumbing without changing `configs.py` or the Sionna numeric algorithm.

New capabilities:

- `discover-scenes` enumerates CARLA Town candidates into `scene_candidates.jsonl`.
- `render-scene-previews` renders dependency-light PNG previews and a contact sheet from candidate descriptors.
- `validate-selected-scenes` validates a Codex/user-selected candidate manifest against the candidate catalog, prepares selected scenes, and uses backup candidate ids when a selected candidate fails.
- `prepare-multi-scene`, `prepare-rf-cache`, `run-multi-scene-supervised`, and `finalize-multi-scene` provide a serial multi-scene wrapper.
- `tx/placement.py` creates 20 reproducible roadside-proxy TX candidates per scene and writes `tx_catalog.json` plus `tx_placement_summary.json`; it does not compute static RSS.
- `rf/static_cache.py` computes static RSS cache for all candidate TXs as a separate RF preparation/cache stage.
- `tx/assignment.py` writes per-episode `tx_assignment.json` for balanced selected-5 TX processing.
- `rf/episode_job.py` and `rf/processing.py` now process selected TXs when `tx.selected_tx_per_episode` and `tx_assignment.json` are present; old single-scene configs without assignment remain all-TX.
- Global merge writes `indexes/global_episode_index.jsonl`, `global_splits.json`, and TX/global summaries.

New configs:

```text
configs/dynamic_radio/multi_scene_discovery_smoke.yaml
configs/dynamic_radio/multi_scene_end2end_smoke.yaml
configs/dynamic_radio/multi_scene_20x150.yaml
```

Validation run in this turn:

```text
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name '*.py')
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
python3 scripts/drd.py validate-multi-scene --config configs/dynamic_radio/multi_scene_discovery_smoke.yaml
python3 scripts/drd.py validate-multi-scene --config configs/dynamic_radio/multi_scene_end2end_smoke.yaml
python3 scripts/drd.py discover-scenes --config configs/dynamic_radio/multi_scene_discovery_smoke.yaml --max-candidates 4
python3 scripts/drd.py render-scene-previews --config configs/dynamic_radio/multi_scene_discovery_smoke.yaml --limit 4
python3 scripts/drd.py prepare-multi-scene --config configs/dynamic_radio/multi_scene_end2end_smoke.yaml --max-scenes 1
```

Artifacts created under ignored dataset roots:

```text
datasets/DynamicRadioMap/MultiSceneDiscoverySmoke/discovery/scene_candidates.jsonl
datasets/DynamicRadioMap/MultiSceneDiscoverySmoke/discovery/previews/contact_sheet.png
datasets/DynamicRadioMap/MultiSceneEnd2EndSmoke/scenes/town10_junction_0189/scene_static/tx_catalog.json
datasets/DynamicRadioMap/MultiSceneEnd2EndSmoke/scenes/town10_junction_0189/scene_static/tx_placement_summary.json
```

`prepare-rf-cache` and full collection/RF were not run in this turn because they invoke Sionna/CARLA runtime work beyond the static/discovery smoke.

## RF Resume Result 2026-05-06 CST

The one-GPU RF resume run finished. It used GPU 1 with one worker and did not change code. The run wrote/updated:

```text
log: datasets/DynamicRadioMap/Town10/rf_resume_gpu1_20260506_104147.log
rf summary: datasets/DynamicRadioMap/Town10/rf_failure_summary.json
pidfile: datasets/DynamicRadioMap/Town10/rf_resume_gpu1.pid
```

Final summary from `rf_failure_summary.json` after the resume:

```text
candidate trajectory-QA accepted episodes: 300
complete skipped at start: 262
queued incomplete episodes: 38
newly RF processed successfully: 20
remaining failed: 18
GPU ids: ["1"]
worker_count: 1
```

Current trainable RF-complete episodes are 282/300. Remaining incomplete episodes are exactly:

```text
episode_000003, episode_000004, episode_000005, episode_000006,
episode_000007, episode_000008, episode_000009, episode_000010,
episode_000011, episode_000012, episode_000013, episode_000014,
episode_000015, episode_000016, episode_000017, episode_000018,
episode_000019, episode_000020
```

Root cause for the remaining 18 is not GPU/RSS instability. The RF per-episode job aborts before RSS because these episodes fail the fixed-scene signature check: their `scene_meta.scene_info` has `num_valid_routes=16`, `turn_types=['left','right','straight']`, 13 roads, and 16 lanes, while the prepared fixed Town10 scene expects `num_valid_routes=14`, `turn_types=['right','straight']`, 10 roads, and 14 lanes. The scene center/crop matches, but the route/topology signature does not. Without code or dataset-contract changes, simple RF reruns will keep rejecting these 18. For training, use the 282 RF-complete episodes or collect replacement episodes under the current scene signature.

## Training Release Packager 2026-05-06 CST

Added a standalone, fail-fast training release packager outside the main DRD CLI and pipeline:

```text
scripts/release_packager/README.md
scripts/release_packager/package_training_release.py
```

Scope/constraints preserved:

- Did not modify `scripts/drd.py` or `scripts/dynamic_radio_dataset/`.
- Does not run CARLA, Sionna, collection, QA, RF, or existing CLI stages.
- Does not delete, move, or clean `--runtime-root`; only reads it.
- Copies/links only the whitelisted training-core root metadata and per-episode files.
- Fails fast on missing index/splits, missing core files, invalid RSS shape, split disagreement, TX-count mismatch, or null TX candidate ids.

Validation run in this turn:

```text
python3 -m py_compile scripts/release_packager/package_training_release.py
python3 scripts/release_packager/package_training_release.py --help
python3 scripts/release_packager/package_training_release.py \
  --runtime-root datasets/DynamicRadioMap/Town10 \
  --out-root datasets/DynamicRadioMapRelease/Town10_v1 \
  --dry-run
```

The dry-run failed fast as expected for the current formal runtime root because `datasets/DynamicRadioMap/Town10` still has no root `episode_index.jsonl` or `splits.json`. No release files were copied or linked, and `datasets/DynamicRadioMapRelease/Town10_v1` was not created by the failed validation.


## Scene Signature Check Relaxed 2026-05-06 CST

To recover the remaining richer Town10 episodes, `scripts/dynamic_radio_dataset/geometry/regions.py`
was updated so `scene_signature_matches()` compares physical fixed-scene identity
rather than the full route-discovery inventory. It still checks town,
scene_mode, support/valid crop geometry, junction id, junction center, and
junction bbox extent. It no longer rejects solely on `num_valid_routes`,
`num_direction_bins`, `roads`, `lanes`, or `turn_types` differences.

This is intended to let the 18 RF-incomplete episodes with `num_valid_routes=16`
and `turn_types=['left','right','straight']` run against the same Town10 junction
static/RF scene. Run static checks and then resume RF on GPU 1 with one worker.

## Multi-Scene Pre-Sionna Validation Status 2026-05-06 CST

The selected-scene workflow has a 20-scene manifest:

```text
configs/dynamic_radio/selected_scene_manifest_20.yaml
source catalog: datasets/DynamicRadioMap/MultiScene20/discovery/scene_candidates.jsonl
contact sheet: datasets/DynamicRadioMap/MultiScene20/discovery/previews/contact_sheet.png
```

Manifest validation confirmed that all 20 selected ids and 15 backups exist in
the candidate catalog. `validate-selected-scenes` was started to run only
pre-Sionna steps (reference capture, Sionna scene export package, tx_catalog,
route_library, plan_catalog). It exposed two runtime issues:

1. `spawn_background_vehicles()` crashed for zero-background reference captures
   because `requested_types` was initialized inside a loop that does not run
   when no background vehicles are requested. Fixed in
   `scripts/dynamic_radio_dataset/carla/collect.py`.
2. Partial failed reference captures left `scene_meta.json` without
   `frames/actor_states.jsonl`; multi-scene reference prep now reruns capture
   unless both files are present, fixed in
   `scripts/dynamic_radio_dataset/multi_scene/runner.py`.

After restarting CARLA and rerunning, 10 scenes initially reached all
pre-Sionna prepared artifacts before CARLA stopped responding on port 2000
again. The unproductive validation parent was stopped to avoid accumulating 30s
timeout failures.

Prepared scene ids so far:

```text
town01_opt_junction_0110
town02_opt_junction_0076
town04_opt_junction_0148
town04_opt_junction_1061
town04_opt_junction_1368
town05_opt_junction_0053
town05_opt_junction_2086
town10_junction_0189
town10_junction_0532
town10_junction_0719
```

For each of these, `reference_scene/frames/actor_states.jsonl`,
`reference_scene/sionna_export/manifest.json`, `scene_static/tx_catalog.json`,
`route_library/`, and `plan_catalog/` are present. `prepare-rf-cache` and
dynamic `process-rf` were not run.

Follow-up fix/result in the same turn:

- `configs/dynamic_radio/selected_scene_manifest_20.yaml` now sets
  `validation_order: grouped_by_town`, `auto_manage_carla: true`, and
  `max_carla_restarts: 8`.
- `scripts/dynamic_radio_dataset/multi_scene/runner.py` now performs a fast
  CARLA health check before each selected scene, reuses the existing supervisor
  CARLA start/restart helpers, retries a candidate once after
  `carla_unavailable`, records transient failures, and stops the owned CARLA
  process at the end.
- Rerunning `validate-selected-scenes` completed with 20/20 accepted scenes.
  Some original selected/backup candidates failed program validation
  (`SIGSEGV` in CARLA scene collection for several Town03 junctions, or
  no-valid-corridor for several corridor candidates), and backups filled the
  target.
- Generated:

```text
configs/dynamic_radio/multi_scene_20x150_resolved.yaml
datasets/DynamicRadioMap/MultiScene20/scene_validation_report.json
```

Validated 20 scenes now in the resolved config:

```text
town01_opt_junction_0110
town02_opt_junction_0076
town02_opt_corridor_0099
town04_opt_junction_0148
town04_opt_junction_1368
town04_opt_junction_1061
town05_opt_junction_0053
town05_opt_junction_2086
town10_junction_0189
town10_junction_0719
town10_junction_0532
town10_junction_0895
town02_opt_corridor_0078
town04_opt_junction_0916
town04_opt_junction_0785
town04_opt_junction_1593
town04_opt_junction_1176
town05_opt_junction_0562
town05_opt_junction_0396
town05_opt_junction_1882
```

Artifact check for the 20 accepted scenes found no missing
`reference_scene/frames/actor_states.jsonl`,
`reference_scene/sionna_export/manifest.json`, `scene_static/tx_catalog.json`,
`scene_static/tx_placement_summary.json`, `route_library/route_features.json`,
or `plan_catalog/plans.jsonl`.

Static checks passed:

```text
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name '*.py')
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

Note: root `/` and `/tmp` are still full; avoid writing temp files under `/tmp`.

Status check on 2026-05-06 14:57 CST:

- No `drd.py` collection/RF process is running.
- CARLA is running and responding on port 2000, current world
  `Carla/Maps/Town05_Opt`.
- MultiScene20 has 20 program-validated scenes and pre-Sionna artifacts, but no
  full trajectory collection yet: 0 episode dirs / 0 accepted trajectories.
- Formal single-scene Town10 has enough collected trajectories:
  `collection_summary.json` reports `target_accepted=300`,
  `total_trajectory_accepted=300`, `target_reached=true`.
- Town10 RF is still incomplete: 284/300 accepted episodes have complete RF
  artifacts; 16 episodes remain RF-incomplete.
- MultiScene20 currently does not have CARLA sensor RGB top-down preview PNGs.
  The existing files under `discovery/previews/*.png` are schematic descriptor
  previews/contact sheets, not CARLA `sensor.camera.rgb` captures. Reference
  validation was run with `--no-video`, so
  `scenes/<scene_id>/reference_scene/frames/topdown_rgb/` is absent for all 20
  scenes.

Update: CARLA RGB top-down diagnostics were captured for all 20 validated
MultiScene20 scenes on 2026-05-06:

```text
flat PNGs: datasets/DynamicRadioMap/MultiScene20/diagnostics/carla_topdown_rgb/png/*.png
gallery: datasets/DynamicRadioMap/MultiScene20/diagnostics/carla_topdown_rgb/index.html
summary: datasets/DynamicRadioMap/MultiScene20/diagnostics/carla_topdown_rgb/capture_summary.json
raw capture dirs: datasets/DynamicRadioMap/MultiScene20/diagnostics/carla_topdown_rgb/captures/<scene_id>/
```

`capture_summary.json` reports 20/20 `ok`. These are diagnostics only; they do
not change formal `scene_static`, `reference_scene`, collection, or RF
artifacts. During capture CARLA became unresponsive once and was restarted by
the diagnostics helper; the current CARLA process is running on port 2000.

## RF Relaxed-Signature Resume Running 2026-05-06 CST

After the user pointed out the remaining 18 episodes are richer and physically
same-scene, the scene signature check was relaxed in
`scripts/dynamic_radio_dataset/geometry/regions.py` to ignore route-inventory
metadata differences. Static py_compile passed for `geometry/regions.py` and
`rf/episode_job.py`.

A new one-GPU resume is running:

```text
pidfile: datasets/DynamicRadioMap/Town10/rf_resume_gpu1_relaxed.pid
pid observed after launch: 3901694
log: datasets/DynamicRadioMap/Town10/rf_resume_gpu1_relaxed_20260506_115058.log
command: PYTHONNOUSERSITE=1 /share1/fzj/miniconda3/envs/carla0915/bin/python scripts/drd.py process-rf --config configs/dynamic_radio/dynamic_radiomap_town10_300.yaml --use-gpu --gpu-ids 1 --rf-workers 1
```

The signature mismatch is no longer blocking. The run entered Sionna RSS for
`episode_000003` and `episode_000004`, but those two hit a Sionna/TensorFlow
internal error during `scene.coverage_map`:

```text
ValueError: Attempt to convert a value (None) with an unsupported type (<class 'NoneType'>) to a Tensor
```

`episode_000005` then processed successfully for all three TXs, proving the
relaxed signature path can generate RF for these richer 16-route episodes.
At the last check during this run:

```text
RF-complete/processed metadata count: 283
remaining incomplete: 17
currently processing: around episode_000006
```

Continue monitoring with:

```bash
PID=$(cat datasets/DynamicRadioMap/Town10/rf_resume_gpu1_relaxed.pid)
ps -p "$PID" -o pid,ppid,stat,etime,cmd
pgrep -P "$PID" -a
tail -f datasets/DynamicRadioMap/Town10/rf_resume_gpu1_relaxed_20260506_115058.log
```

## Training Release Packager Adapter Update 2026-05-06 CST

Per user direction, the standalone packager was updated to match the current
Town10 runtime artifact layout without modifying `scripts/drd.py` or
`scripts/dynamic_radio_dataset/`:

- Runtime root `episode_index.jsonl` / `splits.json` are no longer required by
  the packager. If missing, it discovers QA-accepted RF-complete episodes and
  writes release index rows with `split: "unsplit"`.
- `traffic_grid_uint8.npz` is materialized as release `dynamic_input.npz`.
- Missing per-episode `static_input.npz` is generated in the release from
  current `scene_static/building_mask_uint8.npy`, `loss_mask_uint8.npy`, and
  `tx_catalog.json`.
- This remains a standalone release adapter; it does not run CARLA/Sionna, does
  not mutate `--runtime-root`, and does not change the main pipeline.

Validation after the update:

```text
python3 -m py_compile scripts/release_packager/package_training_release.py
python3 scripts/release_packager/package_training_release.py --help
python3 scripts/release_packager/package_training_release.py \
  --runtime-root datasets/DynamicRadioMap/Town10 \
  --out-root datasets/DynamicRadioMapRelease/Town10_v1 \
  --dry-run
```

Dry-run passed at the time of this check with:

```text
episodes: 284
episode_tx_rows: 852
files_to_materialize: 1138
split_counts: {"unsplit": 852}
```

The count may increase if the background RF resume completes more Town10
episodes before a later packaging run.

## Training Release Package Created 2026-05-06 CST

The standalone packager was run in full copy mode with overwrite:

```text
python3 scripts/release_packager/package_training_release.py \
  --runtime-root datasets/DynamicRadioMap/Town10 \
  --out-root datasets/DynamicRadioMapRelease/Town10_v1 \
  --overwrite
```

Output release root:

```text
datasets/DynamicRadioMapRelease/Town10_v1
```

Package result:

```text
episodes: 284
episode_tx_rows: 852
files_to_materialize: 1138
split_counts: {"unsplit": 852}
size: 2.0G
```

Top-level release files present:

```text
dataset_meta.json
episode_index.jsonl
package_report.json
scene_meta.json
tx_catalog.json
episodes/
```

Per-episode release core file counts:

```text
episode_meta.json: 284
static_input.npz: 284
dynamic_input.npz: 284
rss_dynamic_dbm.npz: 284
tx_assignment.json: 0  # expected for current all-TX Town10 runtime
```

No runtime/debug directories or forbidden diagnostic files were found in the
release root during the post-package spot check (`attempts`, `failed_attempts`,
`diagnostics`, `logs`, `plan_catalog`, `route_library`, `reference_scene`,
`sionna_export`, `rf_cache`, `frames`, `actor_states.jsonl`, `qa_report.json`,
`trajectory_qa.json`, etc.).

## RF Resume Analysis 2026-05-06

A one-GPU RF resume was run on the formal Town10 root after relaxing physical
scene-signature compatibility in `geometry/regions.py`. The relaxed run ended;
no `rf_resume_gpu1_relaxed.pid` process remained at inspection time. Results in
`datasets/DynamicRadioMap/Town10/rf_failure_summary.json`:

```text
complete_skipped_episode_count: 282
queued_episode_count: 18
processed_episode_count: 2
failed_episode_count: 16
new complete RF total by artifact scan: 284/300
successful rich episodes in this pass: episode_000005, episode_000014
remaining failed: episode_000003, 000004, 000006, 000007, 000008, 000009,
  000010, 000011, 000012, 000013, 000015, 000016, 000017, 000018,
  000019, 000020
partial TX outputs: episode_000011 tx_00/tx_01; episode_000012 tx_00;
  episode_000015 tx_00/tx_01; episode_000019 tx_00
```

The old failure mode (`scene_signature_matches` rejecting route inventory
metadata such as route count/turn types) is no longer the blocker. The remaining
failures occur inside Sionna 0.19 coverage-map computation:

```text
scene.coverage_map -> sionna/rt/solver_cm.py -> _apply_ris_reflection
->_extract_active_ris_rays -> tf.gather(radii_curv, active_ind, axis=0)
ValueError: Attempt to convert a value (None) with an unsupported type
(<class 'NoneType'>) to a Tensor.
```

The exported scene XMLs checked for failed/success examples contain no explicit
RIS, and loading them reports `len(scene.ris)==0`; the RIS stack frame is likely
an internal Sionna solver branch/bug rather than a project-level RIS object. Do
not fake or partially accept failed TX outputs for the formal dataset without an
explicit policy decision.

## Sionna RT no-RIS bug diagnosis and hotfix 2026-05-06

Investigated remaining RF failures for the richer Town10 episodes. Evidence now
points to a Sionna 0.19 coverage-map solver bug rather than bad DRD input data:

- Failed stack: `scene.coverage_map -> solver_cm._apply_ris_reflection ->
  _extract_active_ris_rays -> tf.gather(radii_curv, ...)`, with
  `radii_curv is None`.
- Exported scenes checked have no RIS (`len(scene.ris)==0`, no explicit RIS in
  XML), so entering the RIS branch is unintended for these runs.
- `episode_000004/tx_00` with GPU1, `max_depth=3`, reflections enabled, failed
  reproducibly on short multi-frame runs before the patch.
- `max_depth=1` or `--no-reflection` avoided the crash but changes RF physics,
  so these are diagnostics, not preferred formal-data fallbacks.
- Running one frame per subprocess also avoided the crash, indicating a Sionna
  repeated-coverage-map/no-RIS solver issue, not malformed actor motion.
- Even excluding all cars could still trigger the same error, so the dynamic
  vehicle positions are not the root cause.

Applied a local runtime hotfix to the Sionna env:

```text
patched: /share1/fzj/miniconda3/envs/sionna019/lib/python3.8/site-packages/sionna/rt/solver_cm.py
backup:  /share1/fzj/miniconda3/envs/sionna019/lib/python3.8/site-packages/sionna/rt/solver_cm.py.bak_drd_no_ris_guard_20260506
change:  guard RIS branch with `if ris and tf.shape(ris_reflect_ind)[0] > 0:`
```

Post-patch validation:

```text
episode_000004 tx_00 frames 0..20, GPU1, max_depth=3: passed
episode_000004 tx_00 full 100 frames, GPU1, max_depth=3: passed
formal process-episode episode_000003 all 3 TXs, GPU1: passed
```

Formal Town10 artifact scan after processing `episode_000003`:

```text
RF-complete episodes: 285/300
partial TX episodes: episode_000011, episode_000012, episode_000015, episode_000019
missing RF episodes: episode_000004, 000006, 000007, 000008, 000009, 000010,
  000013, 000016, 000017, 000018, 000020
```

If continuing recovery, prefer resuming the remaining incomplete episodes on one
GPU with the patched Sionna environment. Do not use `--no-reflection` or
`max_depth=1` for formal recovery unless the user explicitly chooses that lower
fidelity policy.

## Training Release Repacked with Scene-Level Static Radio 2026-05-06 CST

Per user clarification, static TX radio artifacts were moved to the scene level
in the standalone release packager. The release was rebuilt with overwrite:

```text
python3 scripts/release_packager/package_training_release.py \
  --runtime-root datasets/DynamicRadioMap/Town10 \
  --out-root datasets/DynamicRadioMapRelease/Town10_v1 \
  --overwrite
```

Current release root remains:

```text
datasets/DynamicRadioMapRelease/Town10_v1
```

Current release layout is scene-structured:

```text
dataset_meta.json
episode_index.jsonl
package_report.json
scenes/town10_junction_0189/scene_meta.json
scenes/town10_junction_0189/tx_catalog.json
scenes/town10_junction_0189/tx_static_radio/tx_static_radio_maps.npz
scenes/town10_junction_0189/tx_static_radio/tx_static_radio_meta.json
scenes/town10_junction_0189/episodes/episode_*/episode_meta.json
scenes/town10_junction_0189/episodes/episode_*/dynamic_input.npz
scenes/town10_junction_0189/episodes/episode_*/rss_dynamic_dbm.npz
```

`static_input.npz` is no longer written per episode. `episode_index.jsonl` rows
now reference `tx_static_radio_path` and `tx_static_radio_meta_path` under the
scene directory. For the current Town10 runtime, scene-level
`tx_static_radio_dbm` was derived from existing `dynamic_rss_dbm -
delta_from_static_db` and validated as frame-invariant for the source episode;
no CARLA/Sionna rerun was performed.

Package result after repack:

```text
scene_id: town10_junction_0189
episodes: 285
episode_tx_rows: 855
files_to_materialize: 859
split_counts: {"unsplit": 855}
```

Post-package checks confirmed no per-episode `static_input.npz`, no
`rss_delta_from_static_db.npz`, no QA/runtime/debug files, and no forbidden
runtime directories in the release root.

## Dataloader-Facing Release Description Provided 2026-05-06 CST

A read-only inspection of `datasets/DynamicRadioMapRelease/Town10_v1` confirmed
current release metadata for dataloader authors: one scene
`town10_junction_0189`, 285 episodes, 855 episode-TX index rows, 100 frames,
128x128 grids, 3 TXs, scene-level `tx_static_radio_dbm` shape `(3,128,128)`,
episode dynamic input `traffic_grid_uint8` shape `(100,128,128)`, and dynamic
RSS label `dynamic_rss_dbm` shape `(3,100,128,128)`. No code or dataset files
were changed besides this handoff note.

## Training Release Static Radio Repackage 2026-05-06 CST

The release at the original path was overwritten successfully with the updated
standalone packager:

```text
python3 scripts/release_packager/package_training_release.py \
  --runtime-root datasets/DynamicRadioMap/Town10 \
  --out-root datasets/DynamicRadioMapRelease/Town10_v1 \
  --overwrite
```

Current output:

```text
release root: datasets/DynamicRadioMapRelease/Town10_v1
scene id: town10_junction_0189
episodes: 285
episode-TX rows: 855
mode: copy
size: 2.0G
split_counts: {"unsplit": 855}
```

The release is now scene-structured and includes scene-level static radio maps:

```text
scenes/town10_junction_0189/tx_static_radio/tx_static_radio_maps.npz
scenes/town10_junction_0189/tx_static_radio/tx_static_radio_meta.json
```

`tx_static_radio_maps.npz` contains:

```text
tx_static_radio_dbm: (3, 128, 128) float32
building_mask_uint8: (128, 128) uint8
loss_mask_uint8: (128, 128) uint8
tx_ids: (3,)
tx_candidate_ids: (3,)
tx_positions_xyz: (3, 3) float32
```

Index rows reference `tx_static_radio_path` and `tx_static_radio_meta_path`.
Per-episode files are now `episode_meta.json`, `dynamic_input.npz`, and
`rss_dynamic_dbm.npz`; there are no per-episode `static_input.npz` copies in the
current scene-level-static release layout.

## Training Release Repacked Without Split Fields 2026-05-06 CST

Per user direction, split ownership was removed from the standalone training
release packager. The packager no longer writes `split` in `episode_index.jsonl`,
does not copy/generate split JSON, and no longer records `split_counts` in
release metadata or package report.

Validation and repack commands:

```text
python3 -m py_compile scripts/release_packager/package_training_release.py
python3 scripts/release_packager/package_training_release.py --help
python3 scripts/release_packager/package_training_release.py \
  --runtime-root datasets/DynamicRadioMap/Town10 \
  --out-root datasets/DynamicRadioMapRelease/Town10_v1 \
  --dry-run
python3 scripts/release_packager/package_training_release.py \
  --runtime-root datasets/DynamicRadioMap/Town10 \
  --out-root datasets/DynamicRadioMapRelease/Town10_v1 \
  --overwrite
```

Current release result:

```text
release_root: datasets/DynamicRadioMapRelease/Town10_v1
scene_id: town10_junction_0189
episodes: 285
episode_tx_rows: 855
files_to_materialize: 859
```

Post-check confirmed:

```text
episode_index.jsonl rows: 855
rows containing split: 0
root split files: none
dataset_meta/package_report split_counts: absent
```

## Tmp Radio-Map Video Inventory 2026-05-06 CST

Read-only scan of repo `tmp/` for video files matching radio-map visualization
keywords (`rss`, `radio`, `heatmap`, `green_absolute`, `absolute_rss`,
`reviewer_panel`, `coverage`) found 107 candidate radio/RSS visualization
videos. Most are archived under `tmp/archive_datasets_20260502_221203/`; the
currently top-level tmp radio-map style checks are:

```text
tmp/fresh_sionna_legacy_style_12s_rx080/rss_heatmap_video.mp4
tmp/visual_fresh_support_1200k_check_episode_000027_tx_01/rss_heatmap_video.mp4
tmp/visual_fresh_support_smoke_episode_000027_tx_01/rss_heatmap_video.mp4
tmp/visual_style_check_episode_000027_tx_01/rss_heatmap_video.mp4
tmp/visual_style_default_check_episode_000027_tx_01/rss_heatmap_video.mp4
```

No files were moved, deleted, or generated.

## Episode 000051 TX00 RSS visual anomaly check 2026-05-07 CST

Read-only/runtime diagnostic for user-reported bright patch inside/near a large
vehicle shadow in `datasets/DynamicRadioMap/Town10/episodes/episode_000051/tx_00`.
Generated temporary visualization outputs under:

```text
tmp/episode_000051_tx00_visual_check_20260507/
  contact_sheet_absolute_frames.png
  frame55_diagnostic_panels.png
  frame55_line_profile.png
  summary.json
```

Findings: stored `rss_maps.npz` really contains the visible pattern; it is not a
viewer smoothing artifact. Frame 55 has fusorosa `car_40` near TX00
(`tx=(-32.76,7.43)`, bus center approx `(-48.39,6.54)`). The bus footprint and
west-side nominal shadow contain high RSS cells mixed with floor cells. A CPU
single-frame Sionna rerun for frame 55 qualitatively reproduced the same high
region; GPU rerun was not completed because GPU1 hit Dr.Jit CUDA OOM. Actor
states, traffic grid, and QA are consistent; no obvious CARLA collection failure
was found. Interpretation: likely Sionna RT/coverage-map behavior from metal
vehicle reflection/specular caustics plus receiver grid cells inside/adjacent to
vehicle geometry. Delta-from-static exaggerates it because static cells are at
-270 dBm floor while dynamic reflection can be around -55 dBm.

## Episode 000051 TX00 bbox ablation 2026-05-07 CST

User requested temporary validation without modifying production code/data:
replace `car_40` fusorosa at episode 000051 with bbox variants and compare
frame 55 TX00. Only temporary directories under `tmp/` were created.
Production episode files and `scripts/dynamic_radio_dataset/` were not changed.

Temporary artifacts:

```text
tmp/episode_000051_tx00_box_ablation_20260507/
  box_metal/sionna_export/                 # copied export, car_40 mesh replaced by oriented bbox, material itu_metal
  box_concrete/sionna_export/              # same bbox, material itu_concrete
  box_vacuum/sionna_export/                # same bbox, material vacuum
  box_*/tx00_frame55_cpu/rss_maps.npz      # CPU/LLVM frame-55 TX00 reruns
  original_mesh_no_reflection/tx00_frame55_cpu/rss_maps.npz
  analysis/frame55_variant_absolute_zoom.png
  analysis/frame55_variant_diff_vs_mesh_cpu.png
  analysis/frame55_variant_line_profiles.png
  analysis/numeric_summary.json
```

Key result: original fusorosa mesh with reflection reproduces the bright patch in
the west shadow strip (`132/163` cells > -70 dBm, mean about -96.8 dBm). Original
mesh with `--no-reflection` removes it completely (`0/163` cells > -70 dBm; all
floor). Replacing fusorosa with a simple oriented bbox also removes almost all
of the west-shadow bright patch regardless of bbox material (`1/163` cells >
-70 dBm for metal/concrete/vacuum; mostly -270 dBm floor). Therefore the bright
area is caused by reflection from the detailed fusorosa mesh geometry, not by a
simple box-shaped blocker and not by CARLA collection corruption. Bbox material
choice had minimal effect in this frame compared with geometry simplification.

## Standalone Vehicle Blueprint RF Audit Tool 2026-05-08 CST

Added an independent research diagnostic tool for vehicle blueprint screening:

```text
scripts/drd_research/vehicle_rf_audit/README.md
scripts/drd_research/vehicle_rf_audit/vehicle_rf_audit.py
```

Purpose: generate a whitelist of CARLA vehicle blueprints suitable for a 2D
occupancy radio-map dataset under an input-label consistency constraint. It
keeps vehicles whose RF shadow can be approximated by a 2D occupancy solid
blocker and excludes/reviews vehicles with visual high clearance, non-solid
2-wheel geometry, or RF underbody/mesh bright-spot patterns.

Scope preserved:

- Does not modify `scripts/drd.py` or `scripts/dynamic_radio_dataset/`.
- Does not modify runtime datasets or formal release data.
- Does not change Sionna numeric algorithms or post-process/fix RSS.
- Can enumerate CARLA `vehicle.*` blueprints if requested, read candidate
  JSON/JSONL/CSV, consume manual visual-clearance review files, write a fixed
  RF test plan, optionally invoke an external RF command template, and score
  supplied RF before/after maps.

Outputs:

```text
vehicle_audit_report.csv
vehicle_whitelist.json
vehicle_exclusion_reasons.json
audit_manifest.json
previews/*.ppm
rf_test_plan.json  # optional
```

Default RF test-plan context uses the previously investigated anomaly location:
`town10_junction_0189`, `episode_000051`, `tx_00`, frame 55, substituting each
candidate at the `car_40` fusorosa world pose.

Validation run:

```text
python3 -m py_compile scripts/drd_research/vehicle_rf_audit/vehicle_rf_audit.py
python3 scripts/drd_research/vehicle_rf_audit/vehicle_rf_audit.py --help
```

A temporary self-test under `tmp/vehicle_rf_audit_selftest` was run and then
removed; it verified CSV/JSON/PPM output generation from synthetic RF maps.

## TX Placement Visualization Check 2026-05-12 CST

User asked whether MultiScene20 TX candidates are placed roadside and viewable.
Checked current artifacts and code path. Current active MultiScene20 scenes each
have 40 TX candidates under `scenes/<scene_id>/scene_static/tx_catalog.json` with
`placement_method=roadside_proxy`. The placement filters candidates by distance to
route centerline, building proxies when available, scene-center radius, and
pairwise spacing; it does not use exact CARLA sidewalk/lane polygons, so
"not on road" is a proxy guarantee rather than semantic map proof. Aggregate
current TX-to-route distance across 800 candidates: min 3.43m, mean 5.95m, max
8.50m. Visualization artifacts are available at
`datasets/DynamicRadioMap/MultiScene20/indexes/tx_visualization/`, especially
`tx_routes_contact_sheet.html` and per-scene `*_tx_routes.svg` files.

## MultiScene20 CARLA Collection Restart 2026-05-12 21:40 CST

User asked to restart CARLA trajectory collection after TX visualization/results.
The previous collect run `pid=849375` was alive but degraded: only stale/defunct
CARLA launcher children and one collect subprocess without the matching RPC port.
Stopped the old process group cleanly, confirmed target RPC/TM ports were clear,
then ran metadata repair:

```text
repair command: python3 scripts/drd.py repair-multi-scene-metadata --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
accepted before restart: 1871 / 3000
validation_report_missing_after: 0
tx_assignment_missing_after: 0
```

Restarted CARLA-only collection:

```text
pid: 1850941
log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_20260512_213953.log
latest pidfile: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_latest.pid
latest log symlink: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_latest.log
command: python3 scripts/drd.py collect-multi-scene --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --carla-workers 4 --gpu-ids 0,1 --rpc-ports 2100,2110,2120,2130 --tm-ports 8100,8110,8120,8130 --target-accepted-per-scene 150 --max-carla-restarts-per-scene 1000 --no-rendering-mode
```

Initial post-restart check showed the orchestrator alive and workers started, but
CARLA effective concurrency was still fluctuating: at around 2-3 minutes after
launch, port 2100 had an active CARLA server and other worker launches had
already produced several defunct `CarlaUE4.sh` children, indicating crashes still
occur. Continue monitoring before assuming 4 active CARLA instances.

## TX Placement Road-Extension Issue Analysis 2026-05-12 CST

User flagged visible bad TX candidates in
`indexes/tx_visualization/town05_opt_junction_2086_tx_routes.svg` (e.g. tx_23,
tx_22, tx_32, tx_14, tx_38, tx_28, tx_16) that appear on road/lane extensions
outside the finite route segments. Analysis: current `roadside_proxy` only filters
by distance to selected route polylines and building proxies, not full CARLA
semantic drivable geometry. In multi-lane scenes or near route endpoints, points
~6m from a route centerline can still be inside a lane/road continuation. TX
placement does not drive CARLA spawn/control in the current sampler (`tx_id` is
not used for primary route selection), and trajectory QA treats TX corridor hits
as diagnostics, so collected trajectories do not need rerun. If TX catalog is
changed before RF, regenerate `tx_assignment.json` and visualization; rebuild
route library/plan catalog only if continuing collection with consistency-clean
TX corridor metadata. Recommended fix is to add a CARLA-map driving-lane exclusion
or precomputed road mask for TX placement, not just route-centerline distance.

## MultiScene20 Status Check 2026-05-13 12:20 CST

Read-only check after user asked whether collection was interrupted/restarted and
whether system freeze may be caused by CARLA. Current `collect_multi_scene_latest.pid`
still points to `1850941`, but that process is gone and no CARLA/collect related
processes or target RPC/TM ports are listening. The machine boot time is
`2026-05-13 10:21 CST`, so the previous collection was definitely interrupted by
server reboot; the pidfile is stale.

Current data status:

```text
accepted_total: 1885 / 3000
completed_scenes: 10 / 20
remaining: 1115
missing_validation: 2
missing_tx_assignment: 14
latest accepted episode: town10_junction_0189/episode_000030 at 2026-05-13 00:47:03 CST
accepted since 2026-05-12 21:40 restart: 14, all in town10_junction_0189
```

Unfinished scenes:

```text
town01_opt_junction_0143 41/150
town04_opt_junction_0148 25/150
town04_opt_junction_0785 11/150
town04_opt_junction_1197 21/150
town04_opt_junction_1368 51/150
town05_opt_junction_0359 83/150
town05_opt_junction_1722 40/150
town05_opt_junction_2086 56/150
town10_junction_0189 31/150
town10_junction_0719 26/150
```

Post-launch CARLA stderr signatures from files modified after 2026-05-12 21:40:

```text
stderr files: 259
TimeoutException/time-out waiting for simulator: 227
Traffic Manager bind/create RPC server errors: 32
terminate called recursively: 3
Signal11/segfault signatures in this latest run: 0 observed
```

Latest attempt writes stop around 2026-05-13 03:21 CST; latest accepted episode
is earlier at 00:47. dmesg is not readable by this user (`Operation not
permitted`), and available user-level logs do not prove a kernel/GPU Xid/OOM hard
lockup caused by CARLA. Current GPU processes are non-CARLA `python main.py`
processes on GPU1. If resuming, first run metadata repair, then restart CARLA
collection intentionally; do not assume the old pid is alive.

## MultiScene20 2-Worker Resume 2026-05-13 CST

User requested repair, then continue collection with 2 workers only, one worker
per GPU, with immediate restart after CARLA server failures. Completed:

```text
repair command: python3 scripts/drd.py repair-multi-scene-metadata --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml
accepted after repair: 1885 / 3000
validation_report_missing_after: 0
tx_assignment_missing_after: 0
```

Code changes:

```text
scripts/dynamic_radio_dataset/carla/runner.py
  - classify Traffic Manager bind error as carla_traffic_manager_bind_error
  - optional carla.stop_on_collect_infra_failure stops collection immediately on CARLA infrastructure failures
scripts/dynamic_radio_dataset/multi_scene/carla_parallel.py
  - enables stop_on_collect_infra_failure for multi-scene collection
  - treats carla_attempt_infrastructure_failure and carla_post_clip_infrastructure_failure as restart reasons
scripts/dynamic_radio_dataset/multi_scene/carla_server.py
  - stale cleanup also kills matching dynamic_radio_dataset.carla.collect subprocesses by RPC/TM port
```

Static checks passed after the code change:

```bash
PYTHONPATH=scripts python3 -m py_compile scripts/dynamic_radio_dataset/carla/runner.py scripts/dynamic_radio_dataset/multi_scene/carla_server.py scripts/dynamic_radio_dataset/multi_scene/carla_parallel.py
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

Active launcher:

```text
pid: 500374
log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_latest.log
launcher: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_2workers_autorestart_latest.sh
mode: 2 workers, gpu_ids=0,1, rpc_ports=2100,2110, tm_ports=8100,8110
```

The launcher is an outer while-loop: repair metadata, run `collect-multi-scene`,
check accepted count, sleep 30s, restart if target is still below 3000. It was
started detached and is alive at the time of this handoff. Initial observation:
worker processes are active and new attempts are being written for
`town04_opt_junction_0785` and `town04_opt_junction_1197` around 15:26-15:32 CST,
but accepted count has not yet increased beyond 1885. CARLA Signal 11 remains
present in worker logs, and both GPUs were at 100% / ~88-89C with substantial
non-CARLA python GPU jobs also running. Continue monitoring; do not assume both
CARLA workers are healthy simultaneously.

## MultiScene20 CARLA Root-Cause Fix / Relaunch 2026-05-13 CST

User asked not to label remaining failures generically as CARLA instability. The
latest investigation found three concrete infrastructure causes and corresponding
fixes:

1. Complete clips were aborting at `collection_stage=cleanup_started` after
   `actor_states.jsonl` was fully written. The CARLA Python API raised an
   uncaught C++ `TimeoutException` during actor destruction / cleanup RPC when
   the simulator had already become unavailable. Fix in
   `scripts/dynamic_radio_dataset/carla/collect.py`:
   - write `validation_report.json` immediately after recording, before cleanup;
   - in no-rendering/no-video trajectory mode skip final actor destruction;
   - force-load the requested town at the start of each no-rendering attempt to
     provide a clean actor slate instead of relying on teardown DestroyActor RPC;
   - keep a port-availability guard before any remaining cleanup destroy path.

2. One worker hung mid-recording (`recording_started`, last observed around frame
   41) while `world.tick()` stopped advancing. Fix in
   `scripts/dynamic_radio_dataset/multi_scene/carla_parallel.py`:
   - no-rendering multi-scene attempts cap `collect_subprocess_timeout_s` at 180s
     so infrastructure hangs are killed/restarted promptly instead of waiting
     600s.

3. Both GPUs were already saturated by unrelated Python jobs (`nvidia-smi` showed
   GPU0/GPU1 at 100%, high temperatures), and CARLA UE4 startup repeatedly failed
   with `Signal 11` before RPC ports became ready. Fix in
   `scripts/dynamic_radio_dataset/multi_scene/carla_server.py`:
   - strict port-release failure instead of starting on busy ports;
   - process reaping improvement for exited `CarlaUE4.sh` wrappers;
   - GPU start health gate enabled by default for multi-scene workers
     (`util<95%`, `temp<88C`) to back off instead of launching UE4 into a known
     crash-prone GPU state.

Validation passed after changes:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

Current active launcher after relaunch:

```text
launcher_pid: 779609
collect process: 780038
log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_latest.log
script: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_2workers_autorestart_latest.sh
workers: 2
GPU assignment: 0,1
ports: RPC 2100/2110, TM 8100/8110
```

Current dataset status at the check:

```text
accepted_total: 1885 / 3000
metadata repair before relaunch: validation_report_missing=0, tx_assignment_missing=0
since latest GPU-gated relaunch: 0 attempts started because both GPUs were still at 100% utilization
```

The previous post-cleanup abort behavior improved: after the cleanup-skip patch,
new attempts used `cleanup_skipped_no_rendering_mode` and returned normally; the
remaining rejection pressure before the GPU gate was trajectory QA, mainly
`vehicle_vehicle_collision` in `town04_opt_junction_0785` and
`town04_opt_junction_1197`.

Current low-rate unfinished scenes:

```text
town04_opt_junction_1197: 21/150, failed=382, observed rate ~5.21%
town04_opt_junction_0785: 11/150, failed=184, observed rate ~5.64%
```

They are close to the replacement threshold but have not clearly crossed the
previous pathological policy (`>=500 attempts` and `<5%` acceptance). If they keep
producing only collisions once GPUs are available, prefer candidate-id-based scene
replacement over router/control/QA changes.

### Correction / Latest Runtime Snapshot 2026-05-13 16:56 CST

The final active launcher was relaunched again after enabling GPU-health warning
logging:

```text
launcher_pid: 807097
collect process: 807537
log: datasets/DynamicRadioMap/MultiScene20/logs/collect_multi_scene_latest.log
```

After GPU pressure temporarily dropped, workers resumed attempts. Latest observed
since this final relaunch:

```text
accepted_total: 1885 / 3000
attempt rows since launch: 8
accepted since launch: 0
post_clip infrastructure aborts since launch: 0
failure reasons since launch: vehicle_vehicle_collision=8
```

This means the cleanup/metadata abort issue is mitigated for new attempts. The
current blocker is now trajectory QA collision rejection concentrated in the two
active low-rate scenes:

```text
town04_opt_junction_1197
town04_opt_junction_0785
```

Both are near the prior pathological replacement threshold. If they continue to
produce only collision rejections, the next intervention should be candidate-ID
based scene replacement, not router/control/QA relaxation.

### Visualization Program Locator 2026-05-26 CST

For the remembered Sionna visualization variants:

- Direct/fresh Sionna visualization is `scripts/dynamic_radio_dataset/render/rss_video.py`
  without `--reuse-rss-dir`. It calls `scene.coverage_map()` in
  `compute_rss_map()` and can use high `--resolution`/`--num-samples`.
- Cached 128x128 visualization is `scripts/dynamic_radio_dataset/render/sample.py`,
  which wraps `dynamic_radio_dataset.render.rss_video --reuse-rss-dir <episode>/<tx>`.
  Manual cached rendering also uses `rss_video.py --reuse-rss-dir`.
- Formal dataset RSS generation is `scripts/dynamic_radio_dataset/rf/rss_compute.py`;
  it computes labels with Sionna and writes `rss_maps.npz`, but is not the main
  review-video wrapper.

Potential cause of apparent same-radius brightness changes in cached videos:
`render/rss_video.py` clean style performs visual nearest fill, optional Gaussian
smoothing, and defaults to bilinear interpolation. `render/sample.py` fixes
`visual_smooth_sigma=0.8`, absolute viridis, and `vmin/vmax=-108/-42`, so cached
128x128 cells can be visually smoothed/exaggerated. Prior anomaly notes also
showed at least one pattern existed in `rss_maps.npz` itself, so compare raw
nearest/no-smoothing cached frames against a fresh high-res rerun before
classifying it as viewer-only.
