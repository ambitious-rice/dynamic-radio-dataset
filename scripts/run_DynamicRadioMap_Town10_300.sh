#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_PATH="${REPO_ROOT}/configs/dynamic_radio/dynamic_radiomap_town10_300.yaml"
CARLA_PYTHON="/share1/fzj/miniconda3/envs/carla0915/bin/python"
SIONNA_PYTHON="/share1/fzj/miniconda3/envs/sionna019/bin/python"
TARGET_EPISODES=300

export PYTHONPATH="${REPO_ROOT}/scripts${PYTHONPATH:+:${PYTHONPATH}}"
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export DRD_SIONNA_RUNTIME_BASE="${DRD_SIONNA_RUNTIME_BASE:-/dev/shm/fzj_drd_sionna_rf}"

require_executable() {
  local path="$1"
  local label="$2"
  if [[ ! -x "${path}" ]]; then
    echo "Missing executable ${label}: ${path}" >&2
    exit 2
  fi
}

require_command() {
  local name="$1"
  if ! command -v "${name}" >/dev/null 2>&1; then
    echo "Missing command: ${name}" >&2
    exit 2
  fi
}

cd "${REPO_ROOT}"

require_executable "${CARLA_PYTHON}" "CARLA python"
require_executable "${SIONNA_PYTHON}" "Sionna python"
require_executable "${REPO_ROOT}/CarlaUE4.sh" "CARLA server"

PYTHONNOUSERSITE=1 "${CARLA_PYTHON}" - <<'PY'
import importlib.util
import sys

missing = [name for name in ("numpy", "carla", "networkx") if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit(
        "CARLA python is missing module(s): "
        + ", ".join(missing)
        + f"\npython: {sys.executable}"
        + "\nThis stage intentionally uses carla0915, not the Sionna env."
    )
print(f"CARLA python OK: {sys.executable}")
PY

mapfile -t DRD_PYTHON_FILES < <(find scripts/dynamic_radio_dataset -name "*.py" -print)
PYTHONNOUSERSITE=1 "${CARLA_PYTHON}" -m py_compile "${DRD_PYTHON_FILES[@]}"
PYTHONNOUSERSITE=1 "${CARLA_PYTHON}" -m dynamic_radio_dataset.checks.check_contract --repo-root .
PYTHONNOUSERSITE=1 "${CARLA_PYTHON}" scripts/drd.py --help >/dev/null
PYTHONNOUSERSITE=1 "${SIONNA_PYTHON}" - <<'PY'
import importlib.util
import sys

missing = [name for name in ("numpy", "mitsuba", "drjit", "sionna") if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit(f"Missing Sionna runtime module(s): {', '.join(missing)}")
print(f"Sionna runtime OK: {sys.executable}")
PY

PYTHONNOUSERSITE=1 "${CARLA_PYTHON}" scripts/drd.py run-supervised \
  --config "${CONFIG_PATH}" \
  --target-accepted "${TARGET_EPISODES}" \
  --num-plans 1200 \
  --no-render-sample \
  --use-gpu \
  --gpu-ids 0,1 \
  --rf-workers 2

PYTHONNOUSERSITE=1 "${CARLA_PYTHON}" scripts/drd.py verify-release \
  --config "${CONFIG_PATH}" \
  --expected-episodes "${TARGET_EPISODES}"

PYTHONNOUSERSITE=1 "${CARLA_PYTHON}" scripts/drd.py prune-release \
  --config "${CONFIG_PATH}" \
  --expected-episodes "${TARGET_EPISODES}"

echo "DynamicRadioMap Town10 release dataset is ready at datasets/DynamicRadioMap/Town10"
