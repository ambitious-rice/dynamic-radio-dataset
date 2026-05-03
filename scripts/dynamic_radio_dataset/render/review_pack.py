#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import numpy as np

from dynamic_radio_dataset.render.rss_video import build_rows_from_frame_indices, draw_heatmap_frame, load_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render fixed-scale delta-from-static RSS review keyframes.")
    parser.add_argument("--episode-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--frames", type=str, default="0,40,79")
    parser.add_argument("--vmin-db", type=float, default=-20.0)
    parser.add_argument("--vmax-db", type=float, default=20.0)
    parser.add_argument("--figure-dpi", type=int, default=120)
    parser.add_argument("--floor-dbm", type=float, default=-200.0)
    parser.add_argument("--no-common-mask", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return render_delta_review(
        episode_dir=args.episode_dir,
        output_dir=args.output_dir,
        frames=args.frames,
        vmin_db=float(args.vmin_db),
        vmax_db=float(args.vmax_db),
        figure_dpi=int(args.figure_dpi),
        floor_dbm=float(args.floor_dbm),
        use_common_mask=not bool(args.no_common_mask),
    )


def render_delta_review(
    episode_dir: Path,
    output_dir: Path,
    frames: str = "0,40,79",
    vmin_db: float = -20.0,
    vmax_db: float = 20.0,
    figure_dpi: int = 120,
    floor_dbm: float = -200.0,
    use_common_mask: bool = True,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)

    delta_npz = np.load(episode_dir / "rss_delta_from_static_db.npz")
    delta = np.asarray(delta_npz["delta_from_static_db"], dtype=np.float32)
    tx_ids = [str(item) for item in delta_npz["tx_ids"].tolist()]
    frame_indices = [int(item) for item in delta_npz["frame_indices"].tolist()]
    requested_frames = [int(item.strip()) for item in frames.split(",") if item.strip()]
    selected = [_closest_frame(frame_indices, frame) for frame in requested_frames]

    manifest = load_json(episode_dir / "sionna_export" / "manifest.json")
    rx_region = manifest.get("valid_region") or manifest["valid_crop"]
    motion_rows = build_rows_from_frame_indices(episode_dir / "sionna_export" / "motion.jsonl", selected)
    render_args = _render_args(vmin_db=vmin_db, vmax_db=vmax_db, figure_dpi=figure_dpi)
    rendered = []

    for tx_index, tx_id in enumerate(tx_ids):
        tx_dir = episode_dir / tx_id
        rss_npz = np.load(tx_dir / "rss_maps.npz")
        dynamic_rss = np.asarray(rss_npz["rss_dbm"], dtype=np.float32)
        tx_position = [float(item) for item in rss_npz["tx_position"].tolist()]
        building_mask = np.asarray(rss_npz["building_mask"], dtype=bool) if "building_mask" in rss_npz.files else None
        plot_mask = _plot_mask(
            episode_dir=episode_dir,
            tx_id=tx_id,
            dynamic_rss=dynamic_rss,
            building_mask=building_mask,
            floor_dbm=floor_dbm,
            use_common_mask=use_common_mask,
        )
        per_tx_dir = output_dir / tx_id
        per_tx_dir.mkdir(parents=True, exist_ok=True)
        for frame in selected:
            seq_index = frame_indices.index(frame)
            row = motion_rows[selected.index(frame)]
            frame_path = per_tx_dir / f"delta_frame_{frame:06d}.png"
            draw_heatmap_frame(
                frame_path=frame_path,
                display_db=delta[tx_index, seq_index],
                frame_index=frame,
                tx_position=tx_position,
                manifest=manifest,
                moving_vehicles=row.get("vehicles", []),
                building_mask=plot_mask,
                rx_region=rx_region,
                args=render_args,
            )
            rendered.append((tx_id, frame, frame_path))

    sheet_path = output_dir / "delta_from_static_contact_sheet.png"
    _write_contact_sheet(rendered, sheet_path, vmin_db, vmax_db)
    print({"rendered_frames": len(rendered), "contact_sheet": str(sheet_path)})
    return 0


def _plot_mask(
    episode_dir: Path,
    tx_id: str,
    dynamic_rss: np.ndarray,
    building_mask: Optional[np.ndarray],
    floor_dbm: float,
    use_common_mask: bool,
) -> Optional[np.ndarray]:
    if building_mask is None:
        return None
    mask = np.asarray(building_mask, dtype=bool).copy()
    if not use_common_mask:
        return mask
    scene_static_dir = episode_dir.parents[1] / "scene_static"
    static_path = scene_static_dir / tx_id / "static_rss_dbm.npy"
    if not static_path.exists():
        return mask
    static = np.asarray(np.load(static_path), dtype=np.float32)
    dynamic_finite_all = np.all(np.isfinite(dynamic_rss), axis=0)
    dynamic_above_floor_all = np.all(dynamic_rss > float(floor_dbm), axis=0)
    static_valid = np.isfinite(static) & (static > float(floor_dbm))
    common = (~mask) & static_valid & dynamic_finite_all & dynamic_above_floor_all
    return mask | (~common)


def _render_args(vmin_db: float, vmax_db: float, figure_dpi: int) -> SimpleNamespace:
    return SimpleNamespace(
        figure_dpi=int(figure_dpi),
        plot_style="clean",
        visual_fill_threshold_dbm=-200.0,
        visual_smooth_sigma=0.0,
        view_region="valid",
        vehicle_overlay="dark",
        clean_background_color="#111111",
        building_facecolor="#202020",
        building_edgecolor="#202020",
        vehicle_facecolor="#f8f8f8",
        vehicle_edgecolor="#111111",
        tx_marker_color="#ffff66",
        interpolation="nearest",
        display_mode="delta_first",
        cmap="coolwarm",
        vmin_dbm=float(vmin_db),
        vmax_dbm=float(vmax_db),
        hide_tx_marker=False,
    )


def _closest_frame(frame_indices: list[int], requested: int) -> int:
    return min(frame_indices, key=lambda value: abs(value - requested))


def _write_contact_sheet(rendered: list[tuple[str, int, Path]], output_path: Path, vmin_db: float, vmax_db: float) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.image as mpimg
    import matplotlib.pyplot as plt

    tx_ids = sorted({item[0] for item in rendered})
    frames = sorted({item[1] for item in rendered})
    by_key = {(tx_id, frame): path for tx_id, frame, path in rendered}
    fig, axes = plt.subplots(len(tx_ids), len(frames), figsize=(4.2 * len(frames), 4.2 * len(tx_ids)), dpi=120)
    axes_arr = np.asarray(axes).reshape(len(tx_ids), len(frames))
    for row, tx_id in enumerate(tx_ids):
        for col, frame in enumerate(frames):
            ax = axes_arr[row, col]
            ax.imshow(mpimg.imread(by_key[(tx_id, frame)]))
            ax.set_title(f"{tx_id} frame {frame}")
            ax.set_axis_off()
    fig.suptitle(f"Delta from static RSS, fixed scale [{vmin_db:.1f}, {vmax_db:.1f}] dB", fontsize=13)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path)
    plt.close(fig)


if __name__ == "__main__":
    raise SystemExit(main())
