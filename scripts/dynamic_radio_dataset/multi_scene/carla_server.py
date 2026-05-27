from __future__ import annotations

import os
import resource
import re
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from dynamic_radio_dataset.carla.runner import check_carla_server
from dynamic_radio_dataset.json_utils import save_json
from dynamic_radio_dataset.paths import repo_root


def configure_worker_env(gpu_id: str | None, worker_id: int) -> None:
    for key in (
        "CUDA_VISIBLE_DEVICES",
        "NVIDIA_VISIBLE_DEVICES",
        "DRI_PRIME",
        "VK_LAYER_NV_optimus",
        "__NV_PRIME_RENDER_OFFLOAD",
        "__GLX_VENDOR_LIBRARY_NAME",
    ):
        os.environ.pop(key, None)
    if gpu_id is not None and str(gpu_id).strip():
        gpu_text = str(gpu_id)
        os.environ["DRD_CARLA_GPU_ID"] = gpu_text
    os.environ["DRD_CARLA_PARALLEL_WORKER_ID"] = str(worker_id)
    scripts = str(repo_root() / "scripts")
    existing = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = scripts if not existing else f"{scripts}:{existing}"


def ensure_worker_carla(
    scene_config: Mapping[str, Any],
    proc: subprocess.Popen[str] | None,
    handles: list[Any],
    worker_id: int,
    *,
    startup_retries: int,
) -> tuple[subprocess.Popen[str] | None, list[Any], bool]:
    server = check_carla_server(dict(scene_config))
    if server.get("available"):
        return proc, handles, False
    last_error: Exception | None = None
    for attempt in range(int(startup_retries) + 1):
        try:
            if proc is not None:
                terminate_worker_carla(proc, handles, scene_config)
                proc = None
                handles = []
            kill_stale_carla_for_port(scene_config)
            wait_for_port_release(scene_config)
            proc, handles = start_worker_carla(scene_config, worker_id, startup_attempt=attempt)
            wait_for_carla(scene_config, proc)
            return proc, handles, True
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if proc is not None:
                terminate_worker_carla(proc, handles, scene_config)
                proc = None
                handles = []
            sleep_startup_backoff(scene_config, attempt)
    raise RuntimeError(f"CARLA worker failed to start after {int(startup_retries) + 1} attempts: {last_error}")


def restart_worker_carla(
    scene_config: Mapping[str, Any],
    proc: subprocess.Popen[str] | None,
    handles: list[Any],
    worker_id: int,
    *,
    startup_retries: int,
) -> tuple[subprocess.Popen[str] | None, list[Any]]:
    if proc is not None:
        terminate_worker_carla(proc, handles, scene_config)
    kill_stale_carla_for_port(scene_config)
    wait_for_port_release(scene_config)
    proc, handles, _started = ensure_worker_carla(scene_config, None, [], worker_id, startup_retries=startup_retries)
    return proc, handles


def terminate_worker_carla(
    proc: subprocess.Popen[str],
    handles: list[Any],
    scene_config: Mapping[str, Any] | None = None,
) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=20.0)
        except Exception:  # noqa: BLE001
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=20.0)
            except Exception:  # noqa: BLE001
                proc.kill()
    else:
        try:
            proc.wait(timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
    if scene_config is not None:
        kill_stale_carla_for_port(scene_config)
    close_handles(handles)


def start_worker_carla(
    scene_config: Mapping[str, Any],
    worker_id: int,
    *,
    startup_attempt: int,
) -> tuple[subprocess.Popen[str], list[Any]]:
    multi_scene = scene_config.get("multi_scene", {}) if isinstance(scene_config.get("multi_scene"), Mapping) else {}
    raw_root = multi_scene.get("parent_root", scene_config.get("dataset", {}).get("root", "datasets"))
    root = Path(str(raw_root))
    if not root.is_absolute():
        root = repo_root() / root
    log_dir = root / "supervisor_logs" / "carla_parallel" / f"worker_{worker_id}"
    log_dir.mkdir(parents=True, exist_ok=True)
    supervisor_cfg = scene_config.get("supervisor", {}) if isinstance(scene_config.get("supervisor"), Mapping) else {}
    carla_cfg = scene_config.get("carla", {}) if isinstance(scene_config.get("carla"), Mapping) else {}
    executable = Path(str(supervisor_cfg.get("carla_executable", repo_root() / "CarlaUE4.sh")))
    if not executable.is_absolute():
        executable = repo_root() / executable
    port = int(carla_cfg.get("port", 2000))
    use_nullrhi = _use_nullrhi(scene_config, supervisor_cfg)
    cmd = [str(executable)]
    if use_nullrhi:
        cmd.append("-nullrhi")
    else:
        cmd.append("-RenderOffScreen")
    cmd.extend(["-nosound", "-quality-level=Low", f"-carla-rpc-port={port}"])
    gpu_hint = os.environ.get("DRD_CARLA_GPU_ID")
    if gpu_hint and not use_nullrhi:
        cmd.append(f"-graphicsadapter={gpu_hint}")
        enforce_gpu_start_health(scene_config, gpu_hint)
    extra_args = supervisor_cfg.get("carla_args", [])
    if isinstance(extra_args, list):
        cmd.extend(str(item) for item in extra_args)
    stdout_f = (log_dir / "carla_stdout.log").open("a", encoding="utf-8")
    stderr_f = (log_dir / "carla_stderr.log").open("a", encoding="utf-8")
    start_marker = _carla_start_marker(
        worker_id=worker_id,
        startup_attempt=startup_attempt,
        port=port,
        use_nullrhi=use_nullrhi,
        gpu_hint=gpu_hint,
        cmd=cmd,
    )
    for handle in (stdout_f, stderr_f):
        handle.write(start_marker + "\n")
        handle.flush()
    proc = subprocess.Popen(
        cmd,
        cwd=str(repo_root()),
        stdout=stdout_f,
        stderr=stderr_f,
        env=worker_env(scene_config),
        text=True,
        start_new_session=True,
        preexec_fn=_disable_core_dumps if bool(supervisor_cfg.get("disable_core_dumps", True)) else None,
    )
    save_json(
        log_dir / "carla_start.json",
        {
            "cmd": cmd,
            "pid": proc.pid,
            "port": port,
            "traffic_manager_port": carla_cfg.get("traffic_manager_port"),
            "worker_id": int(worker_id),
            "startup_attempt": int(startup_attempt),
            "graphicsadapter": None if use_nullrhi else gpu_hint,
            "nullrhi": bool(use_nullrhi),
            "env_gpu": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "sanitize_display_env": bool(supervisor_cfg.get("sanitize_display_env", True)),
            "disable_core_dumps": bool(supervisor_cfg.get("disable_core_dumps", True)),
            "sanitize_gpu_env": bool(supervisor_cfg.get("sanitize_gpu_env", True)),
            "log_start_marker": start_marker,
        },
    )
    return proc, [stdout_f, stderr_f]


def _carla_start_marker(
    *,
    worker_id: int,
    startup_attempt: int,
    port: int,
    use_nullrhi: bool,
    gpu_hint: str | None,
    cmd: list[str],
) -> str:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    command_text = " ".join(str(item) for item in cmd)
    return (
        "=== DRD_CARLA_START "
        f"ts={timestamp} worker={int(worker_id)} startup_attempt={int(startup_attempt)} "
        f"port={int(port)} nullrhi={int(bool(use_nullrhi))} gpu={gpu_hint or ''} "
        f"cmd={command_text} ==="
    )


def enforce_gpu_start_health(scene_config: Mapping[str, Any], gpu_id: str) -> None:
    supervisor_cfg = scene_config.get("supervisor", {}) if isinstance(scene_config.get("supervisor", {}), Mapping) else {}
    if not bool(supervisor_cfg.get("gpu_start_health_check", True)):
        return
    try:
        max_util = float(supervisor_cfg.get("gpu_start_max_util_pct", 95.0))
        max_temp = float(supervisor_cfg.get("gpu_start_max_temp_c", 88.0))
    except (TypeError, ValueError):
        max_util = 95.0
        max_temp = 88.0
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "-i",
                str(gpu_id),
                "--query-gpu=utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            capture_output=True,
            timeout=5.0,
            check=False,
        )
    except Exception:
        return
    if proc.returncode != 0:
        return
    line = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 2:
        return
    try:
        util = float(parts[0])
        temp = float(parts[1])
    except ValueError:
        return
    if util >= max_util or temp >= max_temp:
        message = (
            f"GPU {gpu_id} too busy/hot for CARLA start: util={util:.0f}% temp={temp:.0f}C "
            f"(thresholds util<{max_util:.0f}% temp<{max_temp:.0f}C)"
        )
        print(f"[WARN] {message}", flush=True)
        raise RuntimeError(message)


def _use_nullrhi(scene_config: Mapping[str, Any], supervisor_cfg: Mapping[str, Any]) -> bool:
    carla_cfg = scene_config.get("carla", {}) if isinstance(scene_config.get("carla"), Mapping) else {}
    if "use_nullrhi" in supervisor_cfg:
        return bool(supervisor_cfg.get("use_nullrhi"))
    if "use_nullrhi_for_no_rendering" in supervisor_cfg:
        return bool(carla_cfg.get("no_rendering_mode", False)) and bool(supervisor_cfg.get("use_nullrhi_for_no_rendering"))
    return False


def wait_for_carla(scene_config: Mapping[str, Any], proc: subprocess.Popen[str]) -> None:
    supervisor_cfg = scene_config.get("supervisor", {}) if isinstance(scene_config.get("supervisor"), Mapping) else {}
    timeout_s = float(supervisor_cfg.get("carla_startup_timeout_s", 240.0))
    poll_s = float(supervisor_cfg.get("carla_startup_poll_s", 2.0))
    deadline = time.time() + timeout_s
    last = {"available": False, "error": "not checked"}
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"CARLA worker process exited during startup with code {proc.returncode}")
        time.sleep(poll_s)
        last = check_carla_server(dict(scene_config))
        if last.get("available"):
            return
    raise RuntimeError(f"CARLA worker did not become ready within {timeout_s:.1f}s: {last}")


def wait_for_port_release(scene_config: Mapping[str, Any]) -> None:
    supervisor_cfg = scene_config.get("supervisor", {}) if isinstance(scene_config.get("supervisor", {}), Mapping) else {}
    timeout_s = float(supervisor_cfg.get("port_release_timeout_s", 45.0))
    deadline = time.time() + max(1.0, timeout_s)
    ports = carla_related_ports(scene_config)
    while time.time() < deadline:
        listening = listening_ports()
        if listening is not None and not (ports & listening):
            return
        if listening is None and all(not port_accepts(port) for port in ports):
            return
        time.sleep(1.0)
    listening = listening_ports()
    if listening is not None:
        still_busy = sorted(ports & listening)
        if still_busy:
            raise RuntimeError(f"CARLA ports still listening after {timeout_s:.1f}s: {still_busy}")
    still_accepting = sorted(port for port in ports if port_accepts(port))
    if still_accepting:
        raise RuntimeError(f"CARLA ports still accepting connections after {timeout_s:.1f}s: {still_accepting}")


def close_handles(handles: list[Any]) -> None:
    for handle in handles:
        try:
            handle.close()
        except Exception:  # noqa: BLE001
            pass


def worker_env(scene_config: Mapping[str, Any] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    scripts = str(repo_root() / "scripts")
    env["PYTHONPATH"] = scripts if not env.get("PYTHONPATH") else f"{scripts}:{env['PYTHONPATH']}"
    supervisor_cfg = (
        scene_config.get("supervisor", {})
        if scene_config is not None and isinstance(scene_config.get("supervisor", {}), Mapping)
        else {}
    )
    if bool(supervisor_cfg.get("sanitize_display_env", True)):
        for key in (
            "DISPLAY",
            "XAUTHORITY",
            "WAYLAND_DISPLAY",
            "DBUS_SESSION_BUS_ADDRESS",
            "SSH_ASKPASS",
            "VSCODE_IPC_HOOK_CLI",
            "VSCODE_GIT_ASKPASS_MAIN",
            "VSCODE_GIT_ASKPASS_NODE",
            "VSCODE_GIT_ASKPASS_EXTRA_ARGS",
            "VSCODE_GIT_IPC_HANDLE",
        ):
            env.pop(key, None)
    if bool(supervisor_cfg.get("sanitize_gpu_env", True)):
        for key in (
            "CUDA_VISIBLE_DEVICES",
            "NVIDIA_VISIBLE_DEVICES",
            "DRI_PRIME",
            "VK_LAYER_NV_optimus",
            "__NV_PRIME_RENDER_OFFLOAD",
            "__GLX_VENDOR_LIBRARY_NAME",
        ):
            env.pop(key, None)
    if not env.get("VK_ICD_FILENAMES"):
        nvidia_icd = Path("/etc/vulkan/icd.d/nvidia_icd.json")
        if nvidia_icd.exists():
            env["VK_ICD_FILENAMES"] = str(nvidia_icd)
    return env


def restart_backoff_seconds(scene_config: Mapping[str, Any], restart_count: int) -> float:
    supervisor_cfg = scene_config.get("supervisor", {}) if isinstance(scene_config.get("supervisor", {}), Mapping) else {}
    raw = supervisor_cfg.get("restart_backoff_s", [30.0, 60.0, 120.0, 300.0])
    if isinstance(raw, (int, float)):
        values = [float(raw)]
    elif isinstance(raw, list) and raw:
        values = [float(item) for item in raw]
    else:
        values = [30.0, 60.0, 120.0, 300.0]
    index = max(0, min(int(restart_count) - 1, len(values) - 1))
    return max(0.0, float(values[index]))


def sleep_restart_backoff(scene_config: Mapping[str, Any], restart_count: int) -> float:
    seconds = restart_backoff_seconds(scene_config, restart_count)
    if seconds > 0.0:
        time.sleep(seconds)
    return seconds


def sleep_startup_backoff(scene_config: Mapping[str, Any], startup_attempt: int) -> float:
    supervisor_cfg = scene_config.get("supervisor", {}) if isinstance(scene_config.get("supervisor", {}), Mapping) else {}
    if "startup_retry_backoff_s" in supervisor_cfg:
        raw = supervisor_cfg.get("startup_retry_backoff_s")
        values = [float(raw)] if isinstance(raw, (int, float)) else [float(item) for item in raw]
        index = max(0, min(int(startup_attempt), len(values) - 1))
        seconds = max(0.0, values[index])
        if seconds > 0.0:
            time.sleep(seconds)
        return seconds
    return sleep_restart_backoff(scene_config, int(startup_attempt) + 1)


def summarize_worker_carla_stderr(scene_config: Mapping[str, Any], worker_id: int) -> dict[str, Any]:
    multi_scene = scene_config.get("multi_scene", {}) if isinstance(scene_config.get("multi_scene"), Mapping) else {}
    raw_root = multi_scene.get("parent_root", scene_config.get("dataset", {}).get("root", "datasets"))
    root = Path(str(raw_root))
    if not root.is_absolute():
        root = repo_root() / root
    path = root / "supervisor_logs" / "carla_parallel" / f"worker_{worker_id}" / "carla_stderr.log"
    patterns = {
        "signal_11": "Signal 11",
        "segmentation_fault": "Segmentation fault",
        "render_thread_timeout": "RenderThread",
        "low_level_fatal": "LowLevelFatalError",
        "x11_auth": "X11",
        "motty_auth": "MoTTY",
        "vulkan": "Vulkan",
    }
    counts = {key: 0 for key in patterns}
    counts_since_last_start = {key: 0 for key in patterns}
    last_matches: list[str] = []
    last_matches_since_last_start: list[str] = []
    last_start_marker: str | None = None
    if not path.exists():
        return {
            "stderr_log": str(path),
            "exists": False,
            "signature_counts_cumulative": counts,
            "signature_counts_since_last_start": counts_since_last_start,
        }
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "DRD_CARLA_START" in line:
                    last_start_marker = line.strip()
                    counts_since_last_start = {key: 0 for key in patterns}
                    last_matches_since_last_start = []
                matched = False
                for key, pattern in patterns.items():
                    if pattern in line:
                        counts[key] += 1
                        counts_since_last_start[key] += 1
                        matched = True
                if matched:
                    last_matches.append(line.strip())
                    last_matches = last_matches[-8:]
                    last_matches_since_last_start.append(line.strip())
                    last_matches_since_last_start = last_matches_since_last_start[-8:]
    except Exception as exc:  # noqa: BLE001
        return {"stderr_log": str(path), "exists": True, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "stderr_log": str(path),
        "exists": True,
        "signature_counts_cumulative": counts,
        "signature_counts_since_last_start": counts_since_last_start,
        "last_signature_lines": last_matches,
        "last_signature_lines_since_last_start": last_matches_since_last_start,
        "last_start_marker": last_start_marker,
    }


def _disable_core_dumps() -> None:
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:  # noqa: BLE001
        pass


def carla_related_ports(scene_config: Mapping[str, Any]) -> set[int]:
    carla_cfg = scene_config.get("carla", {}) if isinstance(scene_config.get("carla"), Mapping) else {}
    rpc = int(carla_cfg.get("port", 2000))
    ports = {rpc, rpc + 1, rpc + 2}
    tm = carla_cfg.get("traffic_manager_port")
    if tm not in (None, ""):
        ports.add(int(tm))
    return ports


def port_accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.25):
            return True
    except OSError:
        return False


def listening_ports() -> set[int] | None:
    try:
        proc = subprocess.run(["ss", "-ltn"], text=True, capture_output=True, timeout=3.0, check=False)
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    ports: set[int] = set()
    for line in proc.stdout.splitlines():
        match = re.search(r":(\d+)\s+", line)
        if match:
            ports.add(int(match.group(1)))
    return ports


def listening_port_pids(ports: set[int]) -> set[int]:
    try:
        proc = subprocess.run(["ss", "-ltnp"], text=True, capture_output=True, timeout=3.0, check=False)
    except Exception:  # noqa: BLE001
        return set()
    if proc.returncode != 0:
        return set()
    pids: set[int] = set()
    for line in proc.stdout.splitlines():
        match = re.search(r":(\d+)\s+", line)
        if not match or int(match.group(1)) not in ports:
            continue
        for pid_match in re.finditer(r"pid=(\d+)", line):
            pids.add(int(pid_match.group(1)))
    return pids


def kill_stale_carla_for_port(scene_config: Mapping[str, Any]) -> None:
    carla_cfg = scene_config.get("carla", {}) if isinstance(scene_config.get("carla"), Mapping) else {}
    port = int(carla_cfg.get("port", 2000))
    tm_port = carla_cfg.get("traffic_manager_port")
    pattern = f"carla-rpc-port={port}"
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,pgid=,cmd="],
            text=True,
            capture_output=True,
            timeout=5.0,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return
    if proc.returncode != 0:
        return
    group_targets: set[tuple[int, int]] = set()
    pid_targets: set[int] = set()
    process_table: dict[int, tuple[int, str]] = {}
    current = os.getpid()
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
            pgid = int(parts[1])
        except ValueError:
            continue
        cmd = parts[2] if len(parts) > 2 else ""
        process_table[pid] = (pgid, cmd)
        if pid == current:
            continue
        if pattern in line:
            group_targets.add((pid, pgid))
            continue
        if _collect_cmd_matches_port(cmd, port, tm_port):
            pid_targets.add(pid)
    for pid in listening_port_pids(carla_related_ports(scene_config)):
        if pid == current or pid not in process_table:
            continue
        pgid, cmd = process_table[pid]
        if "CarlaUE4" in cmd or "carla-rpc-port" in cmd:
            group_targets.add((pid, pgid))
        elif _collect_cmd_matches_port(cmd, port, tm_port):
            pid_targets.add(pid)
    for _pid, pgid in sorted(group_targets):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except Exception:  # noqa: BLE001
            pass
    for pid in sorted(pid_targets):
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:  # noqa: BLE001
            pass
    if group_targets or pid_targets:
        time.sleep(3.0)
    for pid, pgid in sorted(group_targets):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass
    for pid in sorted(pid_targets):
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            pass


def _collect_cmd_matches_port(cmd: str, rpc_port: int, tm_port: Any) -> bool:
    if "dynamic_radio_dataset.carla.collect" not in cmd:
        return False
    if _has_cli_option(cmd, "--port", str(int(rpc_port))):
        return True
    if tm_port not in (None, "") and _has_cli_option(cmd, "--traffic-manager-port", str(int(tm_port))):
        return True
    return False


def _has_cli_option(cmd: str, option: str, value: str) -> bool:
    parts = cmd.split()
    for idx, part in enumerate(parts):
        if part == option and idx + 1 < len(parts) and parts[idx + 1] == value:
            return True
        if part == f"{option}={value}":
            return True
    return False
