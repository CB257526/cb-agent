#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
from pathlib import Path
from typing import Any

try:
    from PIL import Image
except Exception:  # pragma: no cover - optional convenience dependency
    Image = None  # type: ignore[assignment]


DEFAULT_STATES = {
    "idle": {"row": 0, "frames": 6, "frameDurations": [280, 110, 110, 140, 140, 320]},
    "running-right": {"row": 1, "frames": 8, "frameDurations": [120, 120, 120, 120, 120, 120, 120, 220]},
    "running-left": {"row": 2, "frames": 8, "frameDurations": [120, 120, 120, 120, 120, 120, 120, 220]},
    "waving": {"row": 3, "frames": 4, "frameDurations": [140, 140, 140, 280]},
    "jumping": {"row": 4, "frames": 5, "frameDurations": [140, 140, 140, 140, 280]},
    "failed": {"row": 5, "frames": 8, "frameDurations": [140, 140, 140, 140, 140, 140, 140, 240]},
    "waiting": {"row": 6, "frames": 6, "frameDurations": [150, 150, 150, 150, 150, 260]},
    "running": {"row": 7, "frames": 6, "frameDurations": [120, 120, 120, 120, 120, 220]},
    "review": {"row": 8, "frames": 6, "frameDurations": [150, 150, 150, 150, 150, 280]},
}


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", value.strip()).strip("-._")
    if not cleaned:
        raise ValueError("pet id must contain at least one ASCII letter or number")
    return cleaned[:96]


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", header[16:24])


def parse_duration_list(value: str) -> list[int]:
    durations = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not durations:
        raise argparse.ArgumentTypeError("frameDurations must contain at least one integer")
    return durations


def parse_state(value: str) -> tuple[str, dict[str, Any]]:
    parts = value.split(":")
    if len(parts) not in {4, 5}:
        raise argparse.ArgumentTypeError("state must be name:row:frames:durationMs or name:row:frames:durationMs:dur1,dur2,...")
    name, row, frames, duration = parts[:4]
    state: dict[str, Any] = {
        "row": int(row),
        "frames": int(frames),
        "durationMs": int(duration),
    }
    if len(parts) == 5:
        state["frameDurations"] = parse_duration_list(parts[4])
    return name, state


def build_manifest(args: argparse.Namespace, atlas_name: str) -> dict[str, Any]:
    states = {name: dict(state) for name, state in DEFAULT_STATES.items()}
    for name, state in args.state:
        states[name] = state

    width = args.width
    height = args.height
    detected = png_size(args.spritesheet)
    if detected:
        width = width or detected[0]
        height = height or detected[1]

    width = width or args.columns * args.cell_width
    height = height or args.rows * args.cell_height

    return {
        "id": args.pet_id,
        "displayName": args.display_name,
        "description": args.description or "",
        "renderer": "spritesheet",
        "spritesheetPath": atlas_name,
        "atlas": {
            "width": width,
            "height": height,
            "columns": args.columns,
            "rows": args.rows,
            "cellWidth": args.cell_width,
            "cellHeight": args.cell_height,
        },
        "states": states,
    }


def write_cover_from_idle_cell(atlas_path: Path, output: Path, cell_width: int, cell_height: int) -> bool:
    if Image is None:
        return False
    try:
        with Image.open(atlas_path) as opened:
            cover = opened.convert("RGBA").crop((0, 0, cell_width, cell_height))
        output.parent.mkdir(exist_ok=True)
        cover.save(output)
        return True
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package a portable spritesheet desktop pet.")
    parser.add_argument("--pet-id", required=True, help="portable package id")
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--spritesheet", required=True, type=Path)
    parser.add_argument("--cover", type=Path)
    parser.add_argument("--background", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("spritesheet-pets"))
    parser.add_argument("--columns", type=int, default=8)
    parser.add_argument("--rows", type=int, default=9)
    parser.add_argument("--cell-width", type=int, default=192)
    parser.add_argument("--cell-height", type=int, default=208)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument(
        "--state",
        type=parse_state,
        action="append",
        default=[],
        help="override/add a state as name:row:frames:durationMs or name:row:frames:durationMs:dur1,dur2,...",
    )
    args = parser.parse_args(argv)

    args.pet_id = safe_id(args.pet_id)
    args.spritesheet = args.spritesheet.expanduser().resolve()
    if not args.spritesheet.is_file():
        raise SystemExit(f"spritesheet not found: {args.spritesheet}")

    package_dir = args.output_dir.expanduser().resolve() / args.pet_id
    package_dir.mkdir(parents=True, exist_ok=True)

    atlas_name = args.spritesheet.name
    shutil.copy2(args.spritesheet, package_dir / atlas_name)

    resources_dir = package_dir / "resources"
    if args.cover:
        resources_dir.mkdir(exist_ok=True)
        shutil.copy2(args.cover.expanduser().resolve(), resources_dir / "cover.png")
    else:
        write_cover_from_idle_cell(args.spritesheet, resources_dir / "cover.png", args.cell_width, args.cell_height)
    if args.background:
        resources_dir.mkdir(exist_ok=True)
        shutil.copy2(args.background.expanduser().resolve(), resources_dir / "background.png")

    manifest = build_manifest(args, atlas_name)
    (package_dir / "pet.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(package_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
