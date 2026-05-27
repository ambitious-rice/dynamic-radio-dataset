# Code-Only Migration to a New Server

This project layer lives under a CARLA 0.9.15 tree. Migrate code through GitHub,
but do not migrate generated datasets or runtime artifacts.

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
