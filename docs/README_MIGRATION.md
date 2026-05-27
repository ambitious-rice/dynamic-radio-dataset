# Migration to a New Server

This project layer lives under a CARLA 0.9.15 tree. Migrate code through GitHub,
and optionally migrate the small CARLA-side state needed to rerun Sionna/RF
without recollecting trajectories.

## Push From Source Server

From the source repository:

```bash
cd /share1/fzj/carla
git status --short
git add AGENTS.md .agent docs/README.md docs/README_MIGRATION.md envs scripts/drd.py scripts/dynamic_radio_dataset configs/dynamic_radio
git commit -m "Prepare dynamic radio pipeline migration"
git remote -v
git push origin HEAD:<migration-branch>
```

Do not add generated artifacts:

```text
datasets/
tmp/
docs/archive/
__pycache__/
*.pyc
large logs, videos, and review images
```

## Optional CARLA State Sync

If the new server should reuse existing CARLA trajectories and TX placement, do
not copy the whole `datasets/` tree. Export the CARLA-only state instead:

```bash
cd /share1/fzj/carla
PYTHONPATH=scripts python3 scripts/drd.py export-carla-state \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --output-dir carla_state/MultiScene20 \
  --archive carla_state/MultiScene20.tar.gz \
  --overwrite
```

This includes accepted episode trajectories, plans, trajectory QA, TX
assignments, `scene_static` TX catalogs/signatures, reference-scene metadata,
and the reference Sionna export used as the static building proxy source. It
excludes dynamic RSS, static RSS cache, per-TX `rss_maps.npz`, generated videos,
RF process metadata, and episode Sionna exports.

For a size check without copying files:

```bash
PYTHONPATH=scripts python3 scripts/drd.py export-carla-state \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --output-dir tmp/carla_state_multiscene20_dryrun \
  --dry-run --overwrite
```

On 2026-05-27, the current MultiScene20 CARLA state dry run reported:

```text
3000 accepted episodes
33542 whitelisted files
1706.155 MiB uncompressed
```

The current CARLA state has already been uploaded to GitHub as a separate data
branch:

```text
remote: origin
branch: carla-state-multiscene20
commit: ee82aeb7061cdc4a727ec41c99d92d48fc5b3498
archive: carla_state/MultiScene20.tar.gz split into 3 parts
archive sha256: f4614ad64c73dd37184cd1d53ecc62249cf98ed521535301a6d7b7d2d3abf595
remote verification: fresh shallow clone + reassembled archive passed sha256
```

To retrieve it on the target server:

```bash
git clone --depth 1 --branch carla-state-multiscene20 \
  git@github.com:ambitious-rice/dynamic-radio-dataset.git \
  /tmp/dynamic-radio-carla-state
cd /tmp/dynamic-radio-carla-state/carla_state
cat MultiScene20.tar.gz.part-* > MultiScene20.tar.gz
sha256sum -c MultiScene20.tar.gz.sha256
```

Because the uncompressed state is not tiny, use one of these GitHub flows when
refreshing the uploaded state:

```bash
# Option A: separate data branch with files, if GitHub repo size is acceptable.
git switch --orphan carla-state-multiscene20
git rm -rf . --ignore-unmatch
git add -f carla_state/MultiScene20 carla_state/MultiScene20.tar.gz
git commit -m "Add MultiScene20 CARLA state export"
git push origin carla-state-multiscene20

# Option B: split the archive if the .tar.gz is over GitHub's 100 MB file limit.
split -b 90M carla_state/MultiScene20.tar.gz carla_state/MultiScene20.tar.gz.part-
git add -f carla_state/MultiScene20.tar.gz.part-*
git commit -m "Add split MultiScene20 CARLA state export"
git push origin carla-state-multiscene20
```

On the target server, reconstruct or import the state after cloning the code:

```bash
# If the archive is not split:
PYTHONPATH=scripts python3 scripts/drd.py import-carla-state \
  --source carla_state/MultiScene20.tar.gz \
  --destination-root .

# If the archive was split:
cat carla_state/MultiScene20.tar.gz.part-* > carla_state/MultiScene20.tar.gz
PYTHONPATH=scripts python3 scripts/drd.py import-carla-state \
  --source carla_state/MultiScene20.tar.gz \
  --destination-root .
```

Then recompute RF on the target server:

```bash
export DRD_SIONNA_PYTHON=/path/to/miniconda3/envs/sionna-rt-2x/bin/python
python3 scripts/drd.py prepare-rf-cache \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --force
python3 scripts/drd.py process-multi-scene-rf \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --use-gpu --gpu-ids 0 --rf-workers 1
```

If you intentionally want the target server to rebuild static building proxies
from CARLA instead of reusing the reference Sionna export, add
`--no-reference-export` during export. In that mode, the target server must be
able to run the scene preparation/export path before RF cache generation.

## Target Codex Handoff Prompt

Use this prompt for Codex on the target server:

```text
你现在接手一个 CARLA + Sionna RT 动态 radio-map 数据集项目。代码仓库是：
git@github.com:ambitious-rice/dynamic-radio-dataset.git
优先使用分支 dynamic-radio-migration-sionna2。

核心目标：
1. 不重新跑 CARLA 轨迹采集。
2. 复用源服务器已经导出的 CARLA-only state：轨迹、episode plans、trajectory QA、TX catalog、TX assignment、scene signature、reference scene/static building proxy。
3. 在当前服务器上重新跑 Sionna RT 2.x 的 static RF cache 和 dynamic RF。

先执行：
cd /path/to/carla
git clone --branch dynamic-radio-migration-sionna2 git@github.com:ambitious-rice/dynamic-radio-dataset.git .
或者如果 SSH 不可用，使用 HTTPS clone。

读这些文件：
AGENTS.md
.agent/HANDOFF.md
.agent/DATASET_PIPELINE.md
docs/README_MIGRATION.md

安装环境：
conda env create -f envs/dynamic-radio-orchestrator.yaml
conda env create -f envs/sionna-rt-2x.yaml
export DRD_SIONNA_PYTHON=/path/to/miniconda3/envs/sionna-rt-2x/bin/python

导入 CARLA-only state：
优先从 GitHub 数据分支下载：
git clone --depth 1 --branch carla-state-multiscene20 \
  git@github.com:ambitious-rice/dynamic-radio-dataset.git \
  /tmp/dynamic-radio-carla-state
cd /tmp/dynamic-radio-carla-state/carla_state
cat MultiScene20.tar.gz.part-* > MultiScene20.tar.gz
sha256sum -c MultiScene20.tar.gz.sha256
cd /path/to/carla
PYTHONPATH=scripts python3 scripts/drd.py import-carla-state \
  --source /tmp/dynamic-radio-carla-state/carla_state/MultiScene20.tar.gz \
  --destination-root .

如果用户手动传过来的是 carla_state/MultiScene20.tar.gz：
PYTHONPATH=scripts python3 scripts/drd.py import-carla-state \
  --source carla_state/MultiScene20.tar.gz \
  --destination-root .

如果拿到的是分卷：
cat carla_state/MultiScene20.tar.gz.part-* > carla_state/MultiScene20.tar.gz
PYTHONPATH=scripts python3 scripts/drd.py import-carla-state \
  --source carla_state/MultiScene20.tar.gz \
  --destination-root .

导入后检查：
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help

然后重新跑 RF：
python3 scripts/drd.py prepare-rf-cache \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml --force
python3 scripts/drd.py process-multi-scene-rf \
  --config configs/dynamic_radio/multi_scene_20x150_resolved.yaml \
  --use-gpu --gpu-ids 0 --rf-workers 1

注意：
- 不要运行 collect-multi-scene，除非明确要求重新采集 CARLA。
- 不要把旧的 dynamic RSS、static RSS cache、per-TX rss_maps.npz 当作新结果复用。
- episode 级 sionna_export 应该由新服务器根据轨迹重新导出。
- 大型车辆 fusorosa 的 sealed-underbody 遮挡在当前 exporter 里实现，重新导出 episode sionna_export 时会生效。
- 如果 GitHub 数据分支不可用，就让用户手动传 carla_state/MultiScene20.tar.gz 或分卷包。
```

If this workspace is not already connected to a GitHub remote, create a private
GitHub repository first and add it:

```bash
git remote add origin git@github.com:<owner>/<repo>.git
git push -u origin HEAD:<migration-branch>
```

## Clone On Target Server

Clone into or beside a CARLA 0.9.15 tree, depending on how the target server is
organized:

```bash
git clone --branch <migration-branch> git@github.com:<owner>/<repo>.git /path/to/carla
cd /path/to/carla
```

The target tree should contain:

```text
AGENTS.md
.agent/
docs/
envs/
scripts/drd.py
scripts/dynamic_radio_dataset/
configs/dynamic_radio/
```

## Create Environments

Use the orchestration environment for `scripts/drd.py`:

```bash
conda env create -f envs/dynamic-radio-orchestrator.yaml
conda activate drd-orchestrator
```

Use the Sionna RT 2.x environment for RF subprocesses:

```bash
conda env create -f envs/sionna-rt-2x.yaml
export DRD_SIONNA_PYTHON=/path/to/miniconda3/envs/sionna-rt-2x/bin/python
```

If a package mirror cannot see the latest Sionna RT package, install from PyPI:

```bash
conda activate sionna-rt-2x
python -m pip install --index-url https://pypi.org/simple sionna-rt==2.0.1
```

## CARLA Prerequisites

Install or verify CARLA 0.9.15 on the target server. The project expects the
CARLA Python API to be available from the CARLA tree or `PYTHONPATH`.

Before collection, verify the server can start:

```bash
./CarlaUE4.sh -RenderOffScreen -nosound -quality-level=Low -carla-rpc-port=2000
```

## Post-Clone Checks

Run from the repository root:

```bash
PYTHONPATH=scripts python3 -m py_compile $(find scripts/dynamic_radio_dataset -name "*.py")
PYTHONPATH=scripts python3 -m dynamic_radio_dataset.checks.check_contract --repo-root .
python3 scripts/drd.py --help
```

Then run a lightweight pipeline smoke before starting a full collection:

```bash
python3 scripts/drd.py prepare-scene --config configs/dynamic_radio/town10_junction_smoke.yaml
python3 scripts/drd.py build-route-library --config configs/dynamic_radio/town10_junction_smoke.yaml
python3 scripts/drd.py generate-plans --config configs/dynamic_radio/town10_junction_smoke.yaml --num-plans 5
```

Only start CARLA collection and RF processing after these checks pass.
