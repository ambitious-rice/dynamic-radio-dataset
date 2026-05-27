from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path


ROUTE_ID_RE = re.compile(r"junction_\d+_route_\d+")
OBJECT_ID_RE = re.compile(r"\b(?:episode|attempt|plan)_\d{6}\b")
OLD_ROOT_SCRIPT_RE = re.compile(
    r"scripts/(?:build_dynamic_radio_dataset|collect_dynamic_radio_scene|export_sionna_scene_from_carla|"
    r"render_sionna_rss_heatmap_video|build_single_scene_radio_dataset|generate_carla_traffic_plans|"
    r"radio_dataset_utils)\.py"
)
LEGACY_DEPENDENCY_RE = re.compile(r"dynamic_radio_dataset\.legacy|scripts/dynamic_radio_dataset/legacy|legacy/")
DEPRECATED_CONSTRAINT_TERMS = (
    "tx_clearance_violation",
    "violates_tx_clearance",
    "missing_target_tx_corridor_hit",
    "insufficient_expected_core_visits",
    "primary_vehicle_missed_core_or_tx_corridor",
    "missing_straight_route",
    "missing_turning_route",
    "label_crossing_count",
    "collision_risk_too_high",
)
DEPRECATED_CONFIG_TERMS = (
    "tx_vehicle_clearance_m",
    "min_target_tx_corridor_hits",
    "min_expected_core_visits",
    "max_collision_risk",
)


def run_checks(repo_root: Path) -> dict:
    failures: list[str] = []
    warnings: list[str] = []

    package_root = repo_root / "scripts" / "dynamic_radio_dataset"
    if not package_root.exists():
        failures.append("dynamic_radio_dataset package is missing")
        return _result(failures, warnings)

    _check_no_hardcoded_ids(package_root, failures)
    _check_no_empty_or_stub_modules(package_root, failures)
    _check_no_random_collect_in_main_path(package_root, failures)
    _check_no_deprecated_random_collect_defs(package_root, failures)
    _check_no_active_legacy_dependencies(repo_root, package_root, failures)
    _check_no_old_root_script_dependencies(repo_root, package_root, failures, warnings)
    _check_module_boundaries(package_root, failures)
    _check_train_config(repo_root, failures)
    _check_deprecated_constraint_residuals(repo_root, package_root, failures, warnings)
    _check_multi_scene_config_isolation(repo_root, failures)
    _check_docs(repo_root, failures, warnings)
    _check_lightweight_init(package_root, failures)
    _check_trajectory_gate(package_root, failures)
    return _result(failures, warnings)


def _check_no_hardcoded_ids(package_root: Path, failures: list[str]) -> None:
    for path in _python_files(package_root):
        rel = path.relative_to(package_root)
        if _is_ignored(rel):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if ROUTE_ID_RE.search(text):
            failures.append(f"hard-coded route id outside legacy/tests: {rel}")
        if OBJECT_ID_RE.search(text):
            failures.append(f"hard-coded episode/attempt/plan id outside legacy/tests: {rel}")
        for tx_id in ("tx_00", "tx_01", "tx_02"):
            if tx_id in text and "tx_catalog" not in text:
                failures.append(f"hard-coded TX id outside tx_catalog-driven logic: {rel}")
                break


def _check_no_empty_or_stub_modules(package_root: Path, failures: list[str]) -> None:
    stub_terms = ("TODO " + "place" + "holder", "Not" + "Implemented" + "Error")
    for path in _python_files(package_root):
        rel = path.relative_to(package_root)
        if _is_ignored(rel) or path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            failures.append(f"empty non-init module remains: {rel}")
            continue
        if any(term in text for term in stub_terms):
            failures.append(f"stub marker remains in active module: {rel}")
        try:
            module = ast.parse(text)
        except SyntaxError:
            continue
        body = list(module.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        if not body:
            failures.append(f"docstring-only non-init module remains: {rel}")
            continue
        if all(isinstance(node, ast.Pass) or (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and node.value.value is Ellipsis) for node in body):
            failures.append(f"pass/ellipsis-only module remains: {rel}")


def _check_no_random_collect_in_main_path(package_root: Path, failures: list[str]) -> None:
    main_paths = [
        package_root / "cli.py",
        package_root / "pipeline" / "stages.py",
        package_root / "carla" / "runner.py",
    ]
    for path in main_paths:
        if not path.exists():
            failures.append(f"missing main path module: {path.relative_to(package_root)}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "collect_one_episode" in text or "choose_num_target_vehicles" in text:
            failures.append(f"main path references deprecated random collect logic: {path.relative_to(package_root)}")


def _check_no_deprecated_random_collect_defs(package_root: Path, failures: list[str]) -> None:
    for path in _python_files(package_root):
        rel = path.relative_to(package_root)
        if _is_ignored(rel):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"def\s+(collect_one_episode|choose_num_target_vehicles)\b", text):
            failures.append(f"deprecated random collect function still defined outside legacy/tests: {rel}")


def _check_no_active_legacy_dependencies(repo_root: Path, package_root: Path, failures: list[str]) -> None:
    scan_roots = [
        package_root,
        repo_root / "configs" / "dynamic_radio",
    ]
    for root in scan_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path == package_root / "checks" / "check_contract.py":
                continue
            if package_root in path.parents and _is_ignored(path.relative_to(package_root)):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if LEGACY_DEPENDENCY_RE.search(text):
                failures.append(f"active code/config depends on legacy path: {path.relative_to(repo_root)}")


def _check_no_old_root_script_dependencies(
    repo_root: Path,
    package_root: Path,
    failures: list[str],
    warnings: list[str],
) -> None:
    scan_roots = [
        package_root,
        repo_root / "configs" / "dynamic_radio",
        repo_root / ".agent",
        repo_root / "docs",
    ]
    for root in scan_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path == package_root / "checks" / "check_contract.py":
                continue
            if package_root in path.parents and _is_ignored(path.relative_to(package_root)):
                continue
            rel_repo = path.relative_to(repo_root)
            if len(rel_repo.parts) >= 2 and rel_repo.parts[0] == "docs" and rel_repo.parts[1] == "archive":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if not OLD_ROOT_SCRIPT_RE.search(text):
                continue
            failures.append(f"old root script path referenced in active docs/code/config: {rel_repo}")


def _check_module_boundaries(package_root: Path, failures: list[str]) -> None:
    boundary_rules = [
        ("routes", [r"\bsubprocess\b", r"import\s+carla", r"from\s+carla", r"import\s+sionna", r"import\s+mitsuba", r"import\s+drjit"]),
        ("plans", [r"\bsubprocess\b", r"import\s+carla", r"from\s+carla", r"import\s+sionna", r"import\s+mitsuba", r"import\s+drjit"]),
        ("qa", [r"\bsubprocess\b", r"import\s+carla", r"from\s+carla", r"import\s+sionna", r"import\s+mitsuba", r"import\s+drjit"]),
        ("carla", [r"import\s+sionna", r"from\s+sionna", r"import\s+mitsuba", r"import\s+drjit"]),
        ("sionna", [r"plans\.sampler", r"plans\.validator", r"routes\.route_library"]),
        ("radio", [r"plans\.sampler", r"plans\.validator", r"routes\.route_library"]),
    ]
    for dirname, patterns in boundary_rules:
        root = package_root / dirname
        if not root.exists():
            continue
        for path in _python_files(root):
            rel = path.relative_to(package_root)
            if _is_ignored(rel):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in patterns:
                if re.search(pattern, text):
                    failures.append(f"module boundary violation in {rel}: pattern {pattern!r}")


def _check_train_config(repo_root: Path, failures: list[str]) -> None:
    for path in sorted((repo_root / "configs" / "dynamic_radio").glob("*train*.yaml")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"rf_policy\s*:\s*target_tx_first", text):
            failures.append(f"train config defaults to target_tx_first: {path.relative_to(repo_root)}")


def _check_deprecated_constraint_residuals(
    repo_root: Path,
    package_root: Path,
    failures: list[str],
    warnings: list[str],
) -> None:
    del warnings
    for path in _python_files(package_root):
        rel = path.relative_to(package_root)
        if _is_ignored(rel) or rel == Path("checks/check_contract.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for term in DEPRECATED_CONSTRAINT_TERMS:
            if term in text:
                failures.append(f"deprecated constraint term remains in active code: {rel}: {term}")
        for line in text.splitlines():
            if "passed_target_count" in line and "blocker" in line:
                failures.append(f"passed_target_count appears tied to blocker logic: {rel}")
                break
        if "fail_on_invalid" in text:
            failures.append(f"legacy passed-target fail switch remains in active code: {rel}")

    for path in sorted((repo_root / "configs" / "dynamic_radio").glob("*.yaml")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for term in DEPRECATED_CONFIG_TERMS:
            if term in text:
                failures.append(f"deprecated hard-decision config remains: {path.relative_to(repo_root)}: {term}")


def _check_multi_scene_config_isolation(repo_root: Path, failures: list[str]) -> None:
    try:
        from dynamic_radio_dataset.multi_scene.config import load_multi_scene_config, make_single_scene_config
    except Exception as exc:  # noqa: BLE001
        failures.append(f"could not import multi-scene config helpers: {type(exc).__name__}: {exc}")
        return
    for path in sorted((repo_root / "configs" / "dynamic_radio").glob("multi_scene*.yaml")):
        try:
            config = load_multi_scene_config(path)
        except Exception:
            continue
        for scene in list(config.get("scenes", []))[:1]:
            scene_config = make_single_scene_config(config, scene)
            collection = scene_config.get("collection", {})
            if isinstance(collection, dict) and collection.get("bucket_targets"):
                failures.append(f"multi-scene config inherits single-scene bucket_targets: {path.relative_to(repo_root)}")
            if isinstance(collection, dict) and collection.get("selection_matrix"):
                failures.append(f"multi-scene config inherits single-scene selection_matrix: {path.relative_to(repo_root)}")


def _check_docs(repo_root: Path, failures: list[str], warnings: list[str]) -> None:
    doc_paths = list((repo_root / "docs").glob("*.md")) + list((repo_root / ".agent").glob("*.md"))
    for path in doc_paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        if "python3 scripts/build_dynamic_radio_dataset.py" in text:
            failures.append(f"docs still present old scripts/build_dynamic_radio_dataset.py as primary command: {path.relative_to(repo_root)}")
        if "full Sionna RT coverage-map/radio-label generation" in text and "future work" in text:
            warnings.append(f"possibly stale pre-formal-label wording: {path.relative_to(repo_root)}")


def _check_lightweight_init(package_root: Path, failures: list[str]) -> None:
    init_path = package_root / "__init__.py"
    text = init_path.read_text(encoding="utf-8", errors="replace") if init_path.exists() else ""
    for token in ("import carla", "import sionna", "import mitsuba", "import drjit"):
        if token in text:
            failures.append(f"heavy dependency import in dynamic_radio_dataset/__init__.py: {token}")


def _check_trajectory_gate(package_root: Path, failures: list[str]) -> None:
    stages_path = package_root / "pipeline" / "stages.py"
    processing_path = package_root / "rf" / "processing.py"
    text = ""
    for path in (stages_path, processing_path):
        if path.exists():
            text += path.read_text(encoding="utf-8", errors="replace")
    if "trajectory_accepted_episode_dirs" not in text or "trajectory_qc_pass" not in text:
        failures.append("process-rf gate does not clearly require trajectory_qc_pass before Sionna")


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _is_ignored(rel: Path) -> bool:
    return rel.parts[0] in {"legacy"} or "fixtures" in rel.parts or "tests" in rel.parts


def _result(failures: list[str], warnings: list[str]) -> dict:
    return {
        "schema": "dynamic_radio_dataset_contract_check_v1",
        "pass": not failures,
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "failures": failures,
        "warnings": warnings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check dynamic_radio_dataset engineering and data-contract rules.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_checks(args.repo_root.resolve())
    print(result)
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
