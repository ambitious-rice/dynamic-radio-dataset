# Dynamic Radio Environment Files

Use two environments on a fresh server:

```bash
conda env create -f envs/dynamic-radio-orchestrator.yaml
conda env create -f envs/sionna-rt-2x.yaml
```

The orchestration environment runs `scripts/drd.py`, CARLA collection wrappers,
indexing, QA, and review utilities. It still needs CARLA 0.9.15 Python API to be
available through the CARLA tree or `PYTHONPATH`.

The Sionna environment runs RF subprocesses. Point the project to it with:

```bash
export DRD_SIONNA_PYTHON="$(conda run -n sionna-rt-2x python - <<'PY'
import sys
print(sys.executable)
PY
)"
```

or set the absolute interpreter path manually, for example:

```bash
export DRD_SIONNA_PYTHON=/path/to/miniconda3/envs/sionna-rt-2x/bin/python
```

If the target machine uses a stale package mirror that cannot see
`sionna-rt==2.0.1`, use the official PyPI index or install with:

```bash
python -m pip install --index-url https://pypi.org/simple sionna-rt==2.0.1
```
