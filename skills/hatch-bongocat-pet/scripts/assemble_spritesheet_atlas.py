#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image


COLUMNS = 8
ROWS = 9
CELL_WIDTH = 192
CELL_HEIGHT = 208
ATLAS_WIDTH = COLUMNS * CELL_WIDTH
ATLAS_HEIGHT = ROWS * CELL_HEIGHT

DEFAULT_ROWS = [
    ("idle", 0, 6),
    ("running-right", 1, 8),
    ("running-left", 2, 8),
    ("waving", 3, 4),
    ("jumping", 4, 5),
    ("failed", 5, 8),
    ("waiting", 6, 6),
    ("running", 7, 6),
    ("review", 8, 6),
]


def parse_hex(value: str) -> tuple[int, int, int]:
    value = value.strip()
    if len(value) != 7 or not value.startswith("#"):
        raise SystemExit(f"invalid chroma key {value}; expected #RRGGBB")
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))


def color_distance(red: int, green: int, blue: int, key: tuple[int, int, int]) -> float:
    return math.sqrt((red - key[0]) ** 2 + (green - key[1]) ** 2 + (blue - key[2]) ** 2)


def load_request(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "pet_request.json"
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def rows_from_request(request: dict[str, Any]) -> list[tuple[str, int, int]]:
    states = request.get("states")
    if not isinstance(states, dict):
        return DEFAULT_ROWS
    rows: list[tuple[str, int, int]] = []
    for name, state in states.items():
        if not isinstance(state, dict):
            continue
        try:
            rows.append((str(name), int(state["row"]), int(state["frames"])))
        except Exception:
            continue
    return sorted(rows or DEFAULT_ROWS, key=lambda item: item[1])


def chroma_from_request(request: dict[str, Any], override: str | None) -> tuple[int, int, int]:
    if override:
        return parse_hex(override)
    value = request.get("chromaKey") or request.get("chroma_key")
    if isinstance(value, dict) and isinstance(value.get("hex"), str):
        return parse_hex(value["hex"])
    return parse_hex("#FF00FF")


def remove_chroma(image: Image.Image, key: tuple[int, int, int], threshold: float) -> Image.Image:
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    for y in range(rgba.height):
        for x in range(rgba.width):
            red, green, blue, alpha = pixels[x, y]
            if alpha <= 16 or color_distance(red, green, blue, key) <= threshold:
                pixels[x, y] = (0, 0, 0, 0)
    return rgba


def fit_to_cell(image: Image.Image) -> Image.Image:
    target = Image.new("RGBA", (CELL_WIDTH, CELL_HEIGHT), (0, 0, 0, 0))
    bbox = image.getbbox()
    if bbox is None:
        return target
    sprite = image.crop(bbox)
    scale = min((CELL_WIDTH - 10) / sprite.width, (CELL_HEIGHT - 10) / sprite.height, 1.0)
    if scale != 1.0:
        sprite = sprite.resize(
            (max(1, round(sprite.width * scale)), max(1, round(sprite.height * scale))),
            Image.Resampling.LANCZOS,
        )
    target.alpha_composite(sprite, ((CELL_WIDTH - sprite.width) // 2, (CELL_HEIGHT - sprite.height) // 2))
    return target


def clear_transparent_rgb(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    data = bytearray(rgba.tobytes())
    for index in range(0, len(data), 4):
        if data[index + 3] == 0:
            data[index] = 0
            data[index + 1] = 0
            data[index + 2] = 0
    return Image.frombytes("RGBA", rgba.size, bytes(data))


def row_strip_path(decoded_dir: Path, state: str) -> Path:
    for suffix in (".png", ".webp", ".jpg", ".jpeg"):
        path = decoded_dir / f"{state}{suffix}"
        if path.is_file():
            return path
    raise SystemExit(f"missing decoded row strip: {decoded_dir / (state + '.png')}")


def paste_row(atlas: Image.Image, strip_path: Path, row: int, frames: int, key: tuple[int, int, int], threshold: float) -> None:
    with Image.open(strip_path) as opened:
        strip = remove_chroma(opened, key, threshold)
    slot_width = strip.width / frames
    for column in range(frames):
        left = round(column * slot_width)
        right = round((column + 1) * slot_width)
        frame = fit_to_cell(strip.crop((left, 0, right, strip.height)))
        atlas.alpha_composite(frame, (column * CELL_WIDTH, row * CELL_HEIGHT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble generated row strips into a transparent spritesheet atlas.")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--decoded-dir", type=Path, help="defaults to <run-dir>/decoded")
    parser.add_argument("--output", type=Path, help="defaults to <run-dir>/final/atlas.png")
    parser.add_argument("--chroma-key")
    parser.add_argument("--key-threshold", type=float, default=96.0)
    args = parser.parse_args(argv)

    run_dir = args.run_dir.expanduser().resolve()
    request = load_request(run_dir)
    decoded_dir = args.decoded_dir.expanduser().resolve() if args.decoded_dir else run_dir / "decoded"
    output = args.output.expanduser().resolve() if args.output else run_dir / "final" / "atlas.png"
    key = chroma_from_request(request, args.chroma_key)

    atlas = Image.new("RGBA", (ATLAS_WIDTH, ATLAS_HEIGHT), (0, 0, 0, 0))
    for state, row, frames in rows_from_request(request):
        if row < 0 or row >= ROWS or frames <= 0 or frames > COLUMNS:
            raise SystemExit(f"invalid state geometry for {state}: row={row} frames={frames}")
        paste_row(atlas, row_strip_path(decoded_dir, state), row, frames, key, args.key_threshold)

    atlas = clear_transparent_rgb(atlas)
    output.parent.mkdir(parents=True, exist_ok=True)
    atlas.save(output)
    print(json.dumps({"ok": True, "atlas": str(output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
