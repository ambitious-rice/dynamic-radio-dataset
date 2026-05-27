from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path
from typing import Sequence, Tuple

import numpy as np

Color = Tuple[int, int, int]

FONT = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01111", "10000", "10000", "10011", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "J": ["00111", "00010", "00010", "00010", "10010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "10000", "11110", "00001", "00001", "11110"],
    "6": ["01110", "10000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "_": ["00000", "00000", "00000", "00000", "00000", "00000", "11111"],
    ":": ["00000", "00100", "00100", "00000", "00100", "00100", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    ",": ["00000", "00000", "00000", "00000", "00100", "00100", "01000"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    "(": ["00010", "00100", "01000", "01000", "01000", "00100", "00010"],
    ")": ["01000", "00100", "00010", "00010", "00010", "00100", "01000"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
}


def blank(width: int, height: int, color: Color = (250, 250, 246)) -> np.ndarray:
    img = np.zeros((int(height), int(width), 3), dtype=np.uint8)
    img[:, :] = np.asarray(color, dtype=np.uint8)
    return img


def write_png(path: Path, image: np.ndarray) -> None:
    image = np.asarray(image, dtype=np.uint8)
    h, w = image.shape[:2]
    raw = b"".join(b"\x00" + image[y].tobytes() for y in range(h))
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack("!IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, level=6))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def draw_line(img: np.ndarray, p0: Sequence[float], p1: Sequence[float], color: Color, width: int = 1) -> None:
    x0, y0 = int(round(p0[0])), int(round(p0[1]))
    x1, y1 = int(round(p1[0])), int(round(p1[1]))
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    while True:
        draw_disc(img, (x0, y0), max(0, width // 2), color)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x0 += sx
        if e2 < dx:
            err += dx
            y0 += sy


def draw_polyline(img: np.ndarray, points: Sequence[Sequence[float]], color: Color, width: int = 1) -> None:
    for a, b in zip(points[:-1], points[1:]):
        draw_line(img, a, b, color, width=width)


def draw_disc(img: np.ndarray, center: Sequence[float], radius: int, color: Color) -> None:
    cx, cy = int(round(center[0])), int(round(center[1]))
    r = int(max(0, radius))
    h, w = img.shape[:2]
    x0, x1 = max(0, cx - r), min(w - 1, cx + r)
    y0, y1 = max(0, cy - r), min(h - 1, cy + r)
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                img[y, x] = color


def draw_polygon(img: np.ndarray, points: Sequence[Sequence[float]], color: Color, width: int = 1) -> None:
    if len(points) < 2:
        return
    draw_polyline(img, list(points) + [points[0]], color, width=width)


def rectangle_corners(center: Sequence[float], width: float, height: float, yaw_deg: float) -> list[tuple[float, float]]:
    cx, cy = float(center[0]), float(center[1])
    yaw = math.radians(float(yaw_deg))
    c, s = math.cos(yaw), math.sin(yaw)
    corners = []
    for x, y in [(width / 2, height / 2), (-width / 2, height / 2), (-width / 2, -height / 2), (width / 2, -height / 2)]:
        corners.append((cx + x * c - y * s, cy + x * s + y * c))
    return corners


def draw_text(img: np.ndarray, xy: Sequence[int], text: str, color: Color = (20, 20, 20), scale: int = 2) -> None:
    x, y = int(xy[0]), int(xy[1])
    for ch in str(text).upper():
        glyph = FONT.get(ch, FONT[" "])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit != "1":
                    continue
                _fill_rect(img, x + gx * scale, y + gy * scale, scale, scale, color)
        x += 6 * scale


def paste(dst: np.ndarray, src: np.ndarray, xy: Sequence[int]) -> None:
    x, y = int(xy[0]), int(xy[1])
    h, w = src.shape[:2]
    dst_h, dst_w = dst.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(dst_w, x + w), min(dst_h, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    dst[y0:y1, x0:x1] = src[y0 - y : y1 - y, x0 - x : x1 - x]


def _fill_rect(img: np.ndarray, x: int, y: int, w: int, h: int, color: Color) -> None:
    img[max(0, y) : min(img.shape[0], y + h), max(0, x) : min(img.shape[1], x + w)] = color
