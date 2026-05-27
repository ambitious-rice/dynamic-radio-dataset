from __future__ import annotations

import inspect
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class SionnaRtBackendInfo:
    package_version: str
    mitsuba_variant: str
    radio_map_api: str


@dataclass(frozen=True)
class RadioMapRequest:
    center: Sequence[float]
    orientation: Sequence[float]
    size: Sequence[float]
    cell_size: Sequence[float]
    max_depth: int
    num_samples: int
    num_runs: int
    los: bool
    reflection: bool
    diffraction: bool
    scattering: bool
    edge_diffraction: bool
    seed: int = 42


def array_to_numpy(value: Any) -> np.ndarray:
    if hasattr(value, "numpy"):
        return np.asarray(value.numpy())
    return np.asarray(value)


def load_scene_preserving_names(scene_path: str) -> Any:
    from sionna.rt import load_scene

    supported = _supported_keyword_arguments(load_scene)
    if "merge_shapes" in supported:
        return load_scene(str(scene_path), merge_shapes=False)
    return load_scene(str(scene_path))


def value_to_python(value: Any) -> Any:
    arr = array_to_numpy(value)
    if arr.shape == ():
        return arr.item()
    return arr.tolist()


def configure_scene_arrays(scene: Any, *, planar_array_cls: Any) -> None:
    array = planar_array_cls(
        num_rows=1,
        num_cols=1,
        vertical_spacing=0.5,
        horizontal_spacing=0.5,
        pattern="iso",
        polarization="V",
    )
    scene.tx_array = array
    scene.rx_array = array


def configure_scene_frequency(scene: Any, *, frequency: float, bandwidth: float | None = None) -> None:
    scene.frequency = float(frequency)
    if bandwidth is not None and hasattr(scene, "bandwidth"):
        scene.bandwidth = float(bandwidth)
    if hasattr(scene, "synthetic_array"):
        scene.synthetic_array = True


def make_backend_info(scene: Any) -> SionnaRtBackendInfo:
    import mitsuba as mi

    try:
        import sionna

        version = str(getattr(sionna, "__version__", "unknown"))
    except Exception:
        version = "unknown"
    if version == "unknown":
        try:
            import importlib.metadata as metadata

            for package_name in ("sionna-rt", "sionna"):
                try:
                    version = metadata.version(package_name)
                    break
                except metadata.PackageNotFoundError:
                    continue
            else:
                version = "unknown"
        except Exception:
            version = "unknown"
    return SionnaRtBackendInfo(
        package_version=version,
        mitsuba_variant=str(mi.variant()),
        radio_map_api=("scene.coverage_map" if hasattr(scene, "coverage_map") else "RadioMapSolver"),
    )


def compute_radio_map_rss_watt(scene: Any, request: RadioMapRequest) -> np.ndarray:
    if hasattr(scene, "coverage_map"):
        return _compute_legacy_coverage_map_rss_watt(scene, request)
    return _compute_radio_map_solver_rss_watt(scene, request)


def _compute_legacy_coverage_map_rss_watt(scene: Any, request: RadioMapRequest) -> np.ndarray:
    cm = scene.coverage_map(
        max_depth=int(request.max_depth),
        cm_center=list(request.center),
        cm_orientation=list(request.orientation),
        cm_size=list(request.size),
        cm_cell_size=list(request.cell_size),
        num_samples=int(request.num_samples),
        num_runs=int(request.num_runs),
        los=bool(request.los),
        reflection=bool(request.reflection),
        diffraction=bool(request.diffraction),
        scattering=bool(request.scattering),
        edge_diffraction=bool(request.edge_diffraction),
    )
    return array_to_numpy(cm.rss).astype(np.float64)


def _compute_radio_map_solver_rss_watt(scene: Any, request: RadioMapRequest) -> np.ndarray:
    from sionna.rt import RadioMapSolver

    solver = RadioMapSolver()
    call_kwargs = {
        "center": list(request.center),
        "orientation": list(request.orientation),
        "size": list(request.size),
        "cell_size": list(request.cell_size),
        "samples_per_tx": int(request.num_samples),
        "max_depth": int(request.max_depth),
        "los": bool(request.los),
        "specular_reflection": bool(request.reflection),
        "diffuse_reflection": bool(request.scattering),
        "diffraction": bool(request.diffraction),
        "edge_diffraction": bool(request.edge_diffraction),
        "seed": int(request.seed),
    }
    supported = _supported_keyword_arguments(solver.__call__)
    call_kwargs = {key: value for key, value in call_kwargs.items() if key in supported}
    rss_runs = []
    for run_idx in range(max(1, int(request.num_runs))):
        kwargs = dict(call_kwargs)
        if "seed" in supported:
            kwargs["seed"] = int(request.seed) + run_idx
        radio_map = solver(scene, **kwargs)
        rss_runs.append(array_to_numpy(radio_map.rss).astype(np.float64))
    if len(rss_runs) == 1:
        return rss_runs[0]
    return np.mean(np.stack(rss_runs, axis=0), axis=0)


def _supported_keyword_arguments(func: Any) -> set[str]:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return set()
    return {
        name
        for name, parameter in signature.parameters.items()
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }


def radio_map_request_from_region(
    region: Mapping[str, Any],
    *,
    resolution: int,
    rx_height: float,
    max_depth: int,
    num_samples: int,
    num_runs: int,
    los: bool,
    reflection: bool,
    diffraction: bool,
    scattering: bool,
    edge_diffraction: bool,
) -> RadioMapRequest:
    center = region["center"]
    width = float(region["width_m"])
    height = float(region["height_m"])
    return RadioMapRequest(
        center=[float(center["x"]), float(center["y"]), float(rx_height)],
        orientation=[0.0, 0.0, math.radians(float(region.get("yaw_deg", 0.0)))],
        size=[width, height],
        cell_size=[width / float(resolution), height / float(resolution)],
        max_depth=int(max_depth),
        num_samples=int(num_samples),
        num_runs=int(num_runs),
        los=bool(los),
        reflection=bool(reflection),
        diffraction=bool(diffraction),
        scattering=bool(scattering),
        edge_diffraction=bool(edge_diffraction),
    )
