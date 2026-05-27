#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, shutil, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
import numpy as np
PACKAGER_VERSION = "0.3.0"
SCENE_META_CANDIDATES = ("scene_meta.json", "reference_scene/scene_meta.json")
TX_CATALOG_CANDIDATES = ("tx_catalog.json", "scene_static/tx_catalog.json")
class PackageError(RuntimeError):
    pass

def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PackageError(f"required JSON file is missing: {path}") from None
    except json.JSONDecodeError as exc:
        raise PackageError(f"invalid JSON in {path}: {exc}") from None

def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PackageError(f"invalid JSONL in {path}:{lineno}: {exc}") from None
        if not isinstance(row, dict):
            raise PackageError(f"invalid JSONL row in {path}:{lineno}: expected object")
        rows.append(row)
    if not rows:
        raise PackageError(f"episode index is empty: {path}")
    return rows

def fail_if_bad_episode_id(episode_id: str) -> None:
    if not episode_id or "/" in episode_id or "\\" in episode_id or episode_id in {".", ".."}:
        raise PackageError(f"invalid episode_id: {episode_id!r}")

def first_existing(root: Path, names: Iterable[str]) -> Path | None:
    for name in names:
        path = root / name
        if path.exists():
            if not path.is_file():
                raise PackageError(f"expected file but found non-file: {path}")
            return path
    return None

def index_episode_ids(path: Path) -> set[str]:
    episode_ids: set[str] = set()
    for row in read_jsonl(path):
        if row.get("accepted") is False:
            continue
        if "episode_id" not in row:
            raise PackageError(f"episode index row missing episode_id: {path}")
        episode_id = str(row["episode_id"])
        fail_if_bad_episode_id(episode_id)
        episode_ids.add(episode_id)
    if not episode_ids:
        raise PackageError(f"episode index contains no accepted episodes: {path}")
    return episode_ids
def qa_accepts_episode(qa_path: Path) -> bool:
    qa = load_json(qa_path)
    if not isinstance(qa, dict):
        raise PackageError(f"invalid QA report: {qa_path} must contain an object")
    scene_qa = qa.get("scene_qa") if isinstance(qa.get("scene_qa"), dict) else {}
    if scene_qa and not bool(scene_qa.get("scene_qc_pass")):
        return False
    reports = qa.get("tx_reports", [])
    if isinstance(reports, list) and reports:
        return any(bool(row.get("pair_qc_pass")) for row in reports if isinstance(row, dict))
    return bool(qa.get("accepted_tx_ids"))
def dynamic_source(episode_dir: Path) -> Path | None:
    for name in ("dynamic_input.npz", "traffic_grid_uint8.npz"):
        path = episode_dir / name
        if path.exists():
            return path
    return None
def discover_current_runtime_episodes(root: Path) -> list[str]:
    episodes_dir = root / "episodes"
    if not episodes_dir.is_dir():
        raise PackageError(f"episodes directory is missing: {episodes_dir}")
    episode_ids = []
    for episode_dir in sorted(episodes_dir.glob("episode_*")):
        if not episode_dir.is_dir():
            continue
        needed = ["episode_meta.json", "qa_report.json", "rss_dynamic_dbm.npz", "rss_delta_from_static_db.npz"]
        if any(not (episode_dir / name).exists() for name in needed) or dynamic_source(episode_dir) is None:
            continue
        if qa_accepts_episode(episode_dir / "qa_report.json"):
            fail_if_bad_episode_id(episode_dir.name)
            episode_ids.append(episode_dir.name)
    if not episode_ids:
        raise PackageError("no QA-accepted RF-complete episodes found in current runtime layout")
    return episode_ids
def accepted_episode_ids(root: Path) -> list[str]:
    index_path = root / "episode_index.jsonl"
    if index_path.exists():
        return sorted(index_episode_ids(index_path))
    return discover_current_runtime_episodes(root)
def load_catalog(path: Path) -> list[dict[str, Any]]:
    data = load_json(path)
    rows = data.get("tx_catalog") if isinstance(data, dict) else data if isinstance(data, list) else None
    if not isinstance(rows, list) or not rows:
        raise PackageError(f"invalid tx_catalog format in {path}: missing non-empty tx_catalog list")
    out = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("tx_id") is None:
            raise PackageError(f"invalid tx_catalog row {i} in {path}: missing tx_id")
        item = dict(row); item["tx_id"] = str(row["tx_id"]); item["tx_candidate_id"] = str(row.get("tx_candidate_id", row["tx_id"]))
        if not item["tx_candidate_id"]:
            raise PackageError(f"invalid tx_catalog row {i} in {path}: empty tx_candidate_id")
        out.append(item)
    return out

def scene_id_from_meta(root: Path, meta: Any) -> str:
    if isinstance(meta, dict) and meta.get("scene_id"):
        return str(meta["scene_id"])
    if isinstance(meta, dict):
        town = str(meta.get("town", root.name)).split("/")[-1].replace("HD_Opt", "").replace("_Opt", "").lower()
        info = meta.get("scene_info") if isinstance(meta.get("scene_info"), dict) else {}
        if info.get("junction_id") is not None:
            return f"{town}_junction_{int(info['junction_id']):04d}"
    return root.name

def npz_array(path: Path, key: str) -> np.ndarray:
    try:
        with np.load(path, allow_pickle=False) as data:
            if key not in data.files:
                raise PackageError(f"{path} missing required array key {key!r}")
            return data[key]
    except PackageError:
        raise
    except Exception as exc:
        raise PackageError(f"failed to read NPZ {path}: {type(exc).__name__}: {exc}") from None

def npz_string_list(path: Path, key: str) -> list[str] | None:
    try:
        with np.load(path, allow_pickle=False) as data:
            if key not in data.files:
                return None
            return [str(item) for item in data[key].tolist()]
    except Exception as exc:
        raise PackageError(f"failed to read NPZ metadata {path}:{key}: {type(exc).__name__}: {exc}") from None

def validate_dynamic(path: Path, num_frames: int, grid_shape: list[int]) -> None:
    key = "traffic_grid_uint8" if path.name == "traffic_grid_uint8.npz" else "dynamic_input"
    try:
        with np.load(path, allow_pickle=False) as data:
            if key not in data.files:
                if path.name == "dynamic_input.npz" and "traffic_grid_uint8" in data.files:
                    key = "traffic_grid_uint8"
                else:
                    raise PackageError(f"{path} missing dynamic array key {key!r}")
            arr = data[key]
    except PackageError:
        raise
    except Exception as exc:
        raise PackageError(f"failed to read dynamic input {path}: {type(exc).__name__}: {exc}") from None
    if arr.shape != (num_frames, grid_shape[0], grid_shape[1]):
        raise PackageError(f"{path} shape {list(arr.shape)} does not match RSS frames/grid {(num_frames, *grid_shape)}")

def selected_tx_required(root: Path, episode_ids: list[str]) -> bool:
    if any((root / "episodes" / episode_id / "tx_assignment.json").exists() for episode_id in episode_ids):
        return True
    for rel in ("dataset_meta.json", "scene_index.json", "tx_summary.json", "indexes/tx_summary.json", "configs/resolved_config.json"):
        path = root / rel
        if path.exists() and contains_selected_tx_marker(load_json(path)):
            return True
    return False

def contains_selected_tx_marker(obj: Any) -> bool:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "selected_tx_per_episode":
                try:
                    return int(value) > 0
                except Exception:
                    return True
            if key in {"selected_tx_ids", "selected_tx_candidate_ids"} and value:
                return True
            if contains_selected_tx_marker(value):
                return True
    if isinstance(obj, list):
        return any(contains_selected_tx_marker(item) for item in obj)
    return False

def tx_candidate_ids_for_episode(episode_dir: Path, rss_path: Path, num_tx: int, selected: bool, catalog: list[dict[str, Any]]) -> list[str]:
    by_tx_id = {row["tx_id"]: row for row in catalog}; candidate_set = {row["tx_candidate_id"] for row in catalog}
    if selected:
        assignment_path = episode_dir / "tx_assignment.json"
        if not assignment_path.exists():
            raise PackageError(f"selected-TX dataset missing {assignment_path}")
        assignment = load_json(assignment_path)
        tx_ids = [str(item) for item in assignment.get("selected_tx_ids", [])]
        candidate_ids = [str(item) for item in assignment.get("selected_tx_candidate_ids", [])]
        if len(tx_ids) != num_tx:
            raise PackageError(f"{assignment_path} selected_tx_ids count does not match RSS num_tx")
        if not candidate_ids:
            missing = [tx_id for tx_id in tx_ids if tx_id not in by_tx_id]
            if missing:
                raise PackageError(f"{assignment_path} references unknown tx_id {missing[0]!r}")
            candidate_ids = [by_tx_id[tx_id]["tx_candidate_id"] for tx_id in tx_ids]
        if len(candidate_ids) != num_tx or any(not cid or (candidate_set and cid not in candidate_set) for cid in candidate_ids):
            raise PackageError(f"{assignment_path} has invalid tx candidate mapping")
        return candidate_ids
    npz_candidate_ids = npz_string_list(rss_path, "tx_candidate_ids")
    if npz_candidate_ids is not None:
        if len(npz_candidate_ids) != num_tx:
            raise PackageError(f"{rss_path} tx_candidate_ids count does not match RSS num_tx")
        return npz_candidate_ids
    npz_tx_ids = npz_string_list(rss_path, "tx_ids")
    if npz_tx_ids is not None:
        if len(npz_tx_ids) != num_tx:
            raise PackageError(f"{rss_path} tx_ids count does not match RSS num_tx")
        missing = [tx_id for tx_id in npz_tx_ids if tx_id not in by_tx_id]
        if missing:
            raise PackageError(f"{rss_path} references unknown tx_id {missing[0]!r}")
        return [by_tx_id[tx_id]["tx_candidate_id"] for tx_id in npz_tx_ids]
    if len(catalog) != num_tx:
        raise PackageError(f"{rss_path} num_tx={num_tx} does not match tx_catalog count={len(catalog)}")
    return [row["tx_candidate_id"] for row in catalog]

def derive_static_radio(root: Path, episode_ids: list[str], num_tx: int, grid_shape: list[int]) -> tuple[np.ndarray, str, float]:
    source_episode = episode_ids[0]
    ep = root / "episodes" / source_episode
    dynamic = npz_array(ep / "rss_dynamic_dbm.npz", "dynamic_rss_dbm")
    delta = npz_array(ep / "rss_delta_from_static_db.npz", "delta_from_static_db")
    if dynamic.shape != delta.shape or dynamic.ndim != 4:
        raise PackageError(f"static radio derivation shape mismatch in {ep}")
    if list(dynamic.shape)[0] != num_tx or list(dynamic.shape)[2:] != grid_shape:
        raise PackageError(f"static radio derivation does not match release RSS shape in {ep}")
    static_all = dynamic - delta
    frame_max_abs_diff = float(np.nanmax(np.abs(static_all - static_all[:, :1, :, :])))
    if frame_max_abs_diff > 1e-3:
        raise PackageError(f"derived static radio is not frame-invariant in {ep}: max_diff={frame_max_abs_diff}")
    return static_all[:, 0, :, :].astype(np.float32, copy=False), source_episode, frame_max_abs_diff

def validate_zero_vehicle_static_cache(root: Path, catalog: list[dict[str, Any]], num_tx: int) -> None:
    for row in catalog[:num_tx]:
        tx_id = str(row["tx_id"])
        tx_dir = root / "scene_static" / tx_id
        if not (tx_dir / "static_rss_dbm.npy").exists():
            raise PackageError(f"static RSS cache is missing for {tx_id}: {tx_dir / 'static_rss_dbm.npy'}")
        meta_path = tx_dir / "rss_heatmap_meta.json"
        if not meta_path.exists():
            raise PackageError(f"static RSS cache metadata is missing for {tx_id}: {meta_path}")
        meta = load_json(meta_path)
        propagation = meta.get("propagation") if isinstance(meta, dict) else None
        if not isinstance(propagation, dict):
            raise PackageError(f"static RSS cache metadata has no propagation block for {tx_id}: {meta_path}")
        active = propagation.get("active_vehicle_objects", [])
        if active:
            raise PackageError(
                f"static RSS cache for {tx_id} contains active vehicle objects {active}; "
                "regenerate static cache with prepare-rf-cache --force before packaging"
            )

def write_tx_static_radio(path: Path, meta_path: Path, maps: np.ndarray, source_episode: str, frame_diff: float, catalog: list[dict[str, Any]], masks: tuple[Path, Path]) -> None:
    building = np.load(masks[0], allow_pickle=False); loss = np.load(masks[1], allow_pickle=False)
    tx_ids = np.array([row["tx_id"] for row in catalog], dtype=str); candidate_ids = np.array([row["tx_candidate_id"] for row in catalog], dtype=str)
    positions = []
    for row in catalog:
        pos = row.get("position", {}) if isinstance(row.get("position"), dict) else {}
        positions.append([float(pos.get("x", 0.0)), float(pos.get("y", 0.0)), float(pos.get("z", 0.0))])
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, tx_static_radio_dbm=maps, building_mask_uint8=building.astype(np.uint8, copy=False), loss_mask_uint8=loss.astype(np.uint8, copy=False), tx_ids=tx_ids, tx_candidate_ids=candidate_ids, tx_positions_xyz=np.asarray(positions, dtype=np.float32))
    write_json(meta_path, {"schema": "tx_static_radio_scene_v1", "source": "derived_from_runtime_dynamic_minus_delta", "source_episode_id": source_episode, "packager_version": PACKAGER_VERSION, "shape": list(maps.shape), "frame_invariance_max_abs_diff_db": frame_diff})

def static_mask_sources(root: Path) -> tuple[Path, Path]:
    building = root / "scene_static" / "building_mask_uint8.npy"; loss = root / "scene_static" / "loss_mask_uint8.npy"
    if not building.exists() or not loss.exists():
        raise PackageError("scene-level static masks are missing")
    b = np.load(building, allow_pickle=False); l = np.load(loss, allow_pickle=False)
    if b.shape != l.shape or len(b.shape) != 2:
        raise PackageError(f"scene_static mask shape mismatch: building={b.shape}, loss={l.shape}")
    return building, loss

def summarize_counter(values: Iterable[Any]) -> dict[str, int]:
    return {str(k): int(v) for k, v in sorted(Counter(values).items(), key=lambda item: str(item[0]))}

def build_release_plan(root: Path, out_root: Path, mode: str, dry_run: bool) -> dict[str, Any]:
    if not root.is_dir():
        raise PackageError(f"runtime root does not exist or is not a directory: {root}")
    episode_ids = accepted_episode_ids(root)
    scene_src = first_existing(root, SCENE_META_CANDIDATES); tx_src = first_existing(root, TX_CATALOG_CANDIDATES)
    if scene_src is None:
        raise PackageError("missing required scene metadata: scene_meta.json or reference_scene/scene_meta.json")
    if tx_src is None:
        raise PackageError("missing required TX metadata: tx_catalog.json or scene_static/tx_catalog.json")
    scene_meta = load_json(scene_src); scene_id = scene_id_from_meta(root, scene_meta); scene_root = out_root / "scenes" / scene_id
    catalog = load_catalog(tx_src); selected = selected_tx_required(root, episode_ids); masks = static_mask_sources(root)
    copy_ops: list[tuple[Path, Path]] = [(scene_src, scene_root / "scene_meta.json"), (tx_src, scene_root / "tx_catalog.json")]
    rows = []; tx_counts = []; frame_counts = []; grid_shapes = []; first_num_tx = None; first_grid_shape = None
    for episode_id in episode_ids:
        ep = root / "episodes" / episode_id; meta_path = ep / "episode_meta.json"; rss_path = ep / "rss_dynamic_dbm.npz"; dyn_src = dynamic_source(ep)
        if dyn_src is None:
            raise PackageError(f"required dynamic input is missing for {episode_id}")
        for path in (meta_path, rss_path, ep / "rss_delta_from_static_db.npz"):
            if not path.exists() or not path.is_file():
                raise PackageError(f"required episode file is missing: {path}")
        rss = npz_array(rss_path, "dynamic_rss_dbm")
        if rss.ndim != 4:
            raise PackageError(f"{rss_path} dynamic_rss_dbm must be 4D, found shape {list(rss.shape)}")
        num_tx, num_frames, height, width = [int(v) for v in rss.shape]
        if min(num_tx, num_frames, height, width) <= 0:
            raise PackageError(f"{rss_path} has invalid shape {list(rss.shape)}")
        if first_num_tx is None:
            first_num_tx = num_tx; first_grid_shape = [height, width]
        elif first_num_tx != num_tx or first_grid_shape != [height, width]:
            raise PackageError(f"RSS tx/grid shape differs across episodes at {episode_id}")
        validate_dynamic(dyn_src, num_frames, [height, width])
        candidate_ids = tx_candidate_ids_for_episode(ep, rss_path, num_tx, selected, catalog)
        if len(candidate_ids) != num_tx or any(cid in (None, "", "None", "null") for cid in candidate_ids):
            raise PackageError(f"{rss_path} has invalid tx_candidate_id mapping")
        copy_ops.extend([(meta_path, scene_root / "episodes" / episode_id / "episode_meta.json"), (dyn_src, scene_root / "episodes" / episode_id / "dynamic_input.npz"), (rss_path, scene_root / "episodes" / episode_id / "rss_dynamic_dbm.npz")])
        if selected:
            tx_assign = ep / "tx_assignment.json"; copy_ops.append((tx_assign, scene_root / "episodes" / episode_id / "tx_assignment.json"))
        tx_counts.append(num_tx); frame_counts.append(num_frames); grid_shapes.append([height, width])
        for tx_local_index, tx_candidate_id in enumerate(candidate_ids):
            rows.append({"scene_id": scene_id, "episode_id": episode_id, "tx_local_index": int(tx_local_index), "tx_candidate_id": str(tx_candidate_id), "rss_path": f"scenes/{scene_id}/episodes/{episode_id}/rss_dynamic_dbm.npz", "episode_meta_path": f"scenes/{scene_id}/episodes/{episode_id}/episode_meta.json", "dynamic_input_path": f"scenes/{scene_id}/episodes/{episode_id}/dynamic_input.npz", "tx_static_radio_path": f"scenes/{scene_id}/tx_static_radio/tx_static_radio_maps.npz", "tx_static_radio_meta_path": f"scenes/{scene_id}/tx_static_radio/tx_static_radio_meta.json", "num_frames": int(num_frames), "grid_shape": [height, width]})
    if first_num_tx is None or first_grid_shape is None:
        raise PackageError("no release rows were produced")
    validate_zero_vehicle_static_cache(root, catalog, first_num_tx)
    static_maps, static_source_episode, frame_diff = derive_static_radio(root, episode_ids, first_num_tx, first_grid_shape)
    dataset_meta = {"source_runtime_root": str(root.resolve()), "release_created_at": datetime.now(timezone.utc).isoformat(), "packager_version": PACKAGER_VERSION, "episode_count": len(episode_ids), "episode_tx_row_count": len(rows), "scene_count": 1, "scenes": [scene_id], "tx_count_summary": summarize_counter(tx_counts), "frame_count_summary": summarize_counter(frame_counts), "grid_shape_summary": summarize_counter("x".join(map(str, shape)) for shape in grid_shapes), "scene_level_static_radio": True}
    report = {"source_runtime_root": str(root.resolve()), "out_root": str(out_root), "mode": mode, "dry_run": bool(dry_run), "episode_count": len(episode_ids), "episode_tx_row_count": len(rows), "copied_or_linked_file_count": len(copy_ops) + 2, "validation_passed": True}
    return {"scene_id": scene_id, "scene_root": scene_root, "copy_ops": copy_ops, "rows": rows, "dataset_meta": dataset_meta, "package_report": report, "static_maps": static_maps, "static_source_episode": static_source_episode, "static_frame_diff": frame_diff, "catalog": catalog, "masks": masks}

def ensure_output_ready(out_root: Path, overwrite: bool) -> None:
    if out_root.exists():
        if not overwrite:
            raise PackageError(f"out root already exists; pass --overwrite to replace it: {out_root}")
        if out_root.resolve() == Path("/"):
            raise PackageError("refusing to overwrite filesystem root")
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=False)

def link_or_copy(src: Path, dst: Path, mode: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if mode == "copy": shutil.copy2(src, dst)
    elif mode == "hardlink": os.link(src, dst)
    elif mode == "symlink": os.symlink(src.resolve(), dst)
    else: raise PackageError(f"unsupported mode: {mode}")

def write_index(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")

def validate_release_rows(out_root: Path, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        for key in ("rss_path", "episode_meta_path", "dynamic_input_path", "tx_static_radio_path", "tx_static_radio_meta_path"):
            path = out_root / str(row[key])
            if not path.exists():
                raise PackageError(f"generated index row points to missing file: {path}")
        episode_id = str(row["episode_id"])
        if "split" in row:
            raise PackageError(f"generated index row still contains split for episode {episode_id}")
        if row.get("tx_candidate_id") in (None, "", "None", "null"):
            raise PackageError(f"index row has null tx_candidate_id for episode {episode_id}")

def print_summary(plan: dict[str, Any]) -> None:
    report = plan["package_report"]
    print(f"scene_id: {plan['scene_id']}")
    print(f"episodes: {report['episode_count']}")
    print(f"episode_tx_rows: {report['episode_tx_row_count']}")
    print(f"files_to_materialize: {report['copied_or_linked_file_count']}")

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Package a minimal scene-structured training release from current DRD runtime artifacts.")
    parser.add_argument("--runtime-root", required=True, type=Path, help="Runtime dataset root to read")
    parser.add_argument("--out-root", required=True, type=Path, help="Training release output root")
    parser.add_argument("--mode", choices=("copy", "hardlink", "symlink"), default="copy", help="How to materialize copied/linked episode files")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing out-root")
    parser.add_argument("--dry-run", action="store_true", help="Validate and report without materializing training files")
    return parser.parse_args(argv)

def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        plan = build_release_plan(args.runtime_root, args.out_root, args.mode, args.dry_run)
        if args.dry_run:
            print_summary(plan); args.out_root.mkdir(parents=True, exist_ok=True); write_json(args.out_root / "package_report.dry_run.json", plan["package_report"]); return 0
        ensure_output_ready(args.out_root, args.overwrite)
        for src, dst in plan["copy_ops"]:
            link_or_copy(src, dst, args.mode)
        radio_dir = plan["scene_root"] / "tx_static_radio"
        write_tx_static_radio(radio_dir / "tx_static_radio_maps.npz", radio_dir / "tx_static_radio_meta.json", plan["static_maps"], plan["static_source_episode"], plan["static_frame_diff"], plan["catalog"], plan["masks"])
        write_index(args.out_root / "episode_index.jsonl", plan["rows"])
        write_json(args.out_root / "dataset_meta.json", plan["dataset_meta"])
        write_json(args.out_root / "package_report.json", plan["package_report"])
        validate_release_rows(args.out_root, plan["rows"])
        print_summary(plan); print(f"release_root: {args.out_root}"); return 0
    except PackageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 2
if __name__ == "__main__":
    raise SystemExit(main())
