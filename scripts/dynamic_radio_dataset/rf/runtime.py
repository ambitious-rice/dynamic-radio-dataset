from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence

from dynamic_radio_dataset.paths import repo_root, resolve_repo_path


DEFAULT_SIONNA_PYTHON = Path("/share1/fzj/miniconda3/envs/sionna019/bin/python")


def sionna_python(config: dict) -> Path:
    return resolve_repo_path(config.get("sionna", {}).get("python", DEFAULT_SIONNA_PYTHON))


def sionna_env(runtime_key: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONNOUSERSITE", "1")
    share_home = Path("/share1/fzj")
    runtime_home = sionna_runtime_base() / runtime_key if runtime_key else share_home
    configure_sionna_runtime_env(env, runtime_home)
    return with_package_path(env)


def manual_sionna_env(*, use_gpu: bool) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONNOUSERSITE", "1")
    runtime_home = Path(env["DRD_SIONNA_RUNTIME_HOME"]) if env.get("DRD_SIONNA_RUNTIME_HOME") else default_runtime_home(use_gpu=use_gpu)
    configure_sionna_runtime_env(env, runtime_home)
    return with_package_path(env)


def default_runtime_home(*, use_gpu: bool) -> Path:
    if use_gpu:
        return sionna_runtime_base() / f"manual_{os.getpid()}"
    return Path("/share1/fzj")


def sionna_runtime_key(*, gpu_id: str | None, worker_index: int) -> str:
    gpu_token = safe_runtime_token(gpu_id if gpu_id is not None else "cpu")
    return f"worker_{int(worker_index):02d}_gpu_{gpu_token}"


def safe_runtime_token(value: object) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value))


def sionna_runtime_base() -> Path:
    override = os.environ.get("DRD_SIONNA_RUNTIME_BASE")
    candidates = [Path(override)] if override else []
    candidates.extend([Path("/dev/shm/fzj_drd_sionna_rf"), Path("/share1/fzj/.drd_runtime/sionna_rf")])
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        return candidate
    return Path("/share1/fzj/.drd_runtime/sionna_rf")


def configure_sionna_runtime_env(env: dict[str, str], runtime_home: Path) -> None:
    cache_home = runtime_home / ".cache"
    drjit_cache = cache_home / "drjit"
    cuda_cache = runtime_home / ".nv" / "ComputeCache"
    for path in (runtime_home, runtime_home / ".drjit", cache_home, drjit_cache, cuda_cache):
        path.mkdir(parents=True, exist_ok=True)
    env["DRD_SIONNA_RUNTIME_HOME"] = str(runtime_home)
    env["HOME"] = str(runtime_home)
    env["XDG_CACHE_HOME"] = str(cache_home)
    env["DRJIT_CACHE_DIR"] = str(drjit_cache)
    env["CUDA_CACHE_PATH"] = str(cuda_cache)
    env.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")


def run_command(cmd: Sequence[object], env: dict[str, str] | None = None) -> None:
    str_cmd = [str(item) for item in cmd]
    subprocess.run(str_cmd, cwd=str(repo_root()), check=True, env=with_package_path(env))


def with_package_path(env: dict[str, str] | None = None) -> dict[str, str]:
    result = os.environ.copy() if env is None else dict(env)
    scripts_dir = str(repo_root() / "scripts")
    existing = result.get("PYTHONPATH")
    result["PYTHONPATH"] = scripts_dir if not existing else f"{scripts_dir}:{existing}"
    return result

