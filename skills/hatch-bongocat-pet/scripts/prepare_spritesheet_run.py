#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw


ATLAS = {
    "width": 1536,
    "height": 1872,
    "columns": 8,
    "rows": 9,
    "cellWidth": 192,
    "cellHeight": 208,
}

ROWS = [
    ("idle", 0, 6, [280, 110, 110, 140, 140, 320], "quiet breathing, blinking, and subtle body bob"),
    ("running-right", 1, 8, [120, 120, 120, 120, 120, 120, 120, 220], "rightward drag movement"),
    ("running-left", 2, 8, [120, 120, 120, 120, 120, 120, 120, 220], "leftward drag movement"),
    ("waving", 3, 4, [140, 140, 140, 280], "friendly greeting gesture"),
    ("jumping", 4, 5, [140, 140, 140, 140, 280], "anticipation, lift, peak, descent, settle"),
    ("failed", 5, 8, [140, 140, 140, 140, 140, 140, 140, 240], "sad or blocked reaction"),
    ("waiting", 6, 6, [150, 150, 150, 150, 150, 260], "expectant user-input pose"),
    ("running", 7, 6, [120, 120, 120, 120, 120, 220], "focused working or processing loop"),
    ("review", 8, 6, [150, 150, 150, 150, 150, 280], "focused review or inspection loop"),
]

STATE_RULES = {
    "idle": "Keep motion calm and low-distraction. Do not wave, jump, run, work, or add props.",
    "running-right": "Face and move right through body and limb pose only. No speed lines, dust, shadows, or motion trails.",
    "running-left": "Face and move left through body and limb pose only. No speed lines, dust, shadows, or motion trails.",
    "waving": "Show the wave only through a hand, paw, wing, or limb pose. No wave marks or floating effects.",
    "jumping": "Show vertical motion through body position only. No floor shadow, dust, bounce pad, or landing mark.",
    "failed": "Use slumped pose, sad eyes, or deflated body language. Effects must be attached to the silhouette.",
    "waiting": "Make it clearly expectant or asking for input, distinct from idle and review.",
    "running": "Show active task work or processing, not literal foot-running or directional travel.",
    "review": "Show focus through lean, eyes, head tilt, or hand/paw position. Avoid new UI/text props.",
}

CHROMA_CANDIDATES = [
    ("magenta", "#FF00FF"),
    ("cyan", "#00FFFF"),
    ("yellow", "#FFFF00"),
    ("blue", "#0000FF"),
    ("orange", "#FF7F00"),
    ("green", "#00FF00"),
]


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", value.strip()).strip("-._")
    return cleaned[:96] or "pet"


def parse_hex(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise SystemExit(f"invalid color {value}; expected #RRGGBB")
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def color_distance(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    return math.sqrt(sum((left[index] - right[index]) ** 2 for index in range(3)))


def sampled_reference_pixels(paths: list[Path]) -> list[tuple[int, int, int]]:
    pixels: list[tuple[int, int, int]] = []
    for path in paths:
        with Image.open(path) as opened:
            image = opened.convert("RGBA")
            image.thumbnail((128, 128), Image.Resampling.LANCZOS)
            for red, green, blue, alpha in image.getdata():
                if alpha > 16 and not (red > 244 and green > 244 and blue > 244):
                    pixels.append((red, green, blue))
    return pixels


def choose_chroma_key(paths: list[Path], requested: str) -> dict[str, Any]:
    if requested.lower() != "auto":
        rgb = parse_hex(requested)
        return {"hex": rgb_to_hex(rgb), "rgb": list(rgb), "name": "manual", "selection": "manual"}
    pixels = sampled_reference_pixels(paths)
    if not pixels:
        rgb = parse_hex("#FF00FF")
        return {"hex": "#FF00FF", "rgb": list(rgb), "name": "magenta", "selection": "fallback"}

    best: tuple[float, int, str, tuple[int, int, int]] | None = None
    for index, (name, value) in enumerate(CHROMA_CANDIDATES):
        rgb = parse_hex(value)
        distances = sorted(color_distance(rgb, pixel) for pixel in pixels)
        sample = distances[max(0, min(len(distances) - 1, int(len(distances) * 0.01)))]
        candidate = (sample, -index, name, rgb)
        if best is None or candidate > best:
            best = candidate
    assert best is not None
    score, _order, name, rgb = best
    return {"hex": rgb_to_hex(rgb), "rgb": list(rgb), "name": name, "selection": "auto", "score": round(score, 2)}


def draw_dashed_line(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int], fill: str) -> None:
    dash = 8
    gap = 6
    x1, y1 = start
    x2, y2 = end
    if x1 == x2:
        for y in range(min(y1, y2), max(y1, y2), dash + gap):
            draw.line((x1, y, x2, min(y + dash, max(y1, y2))), fill=fill)
    elif y1 == y2:
        for x in range(min(x1, x2), max(x1, x2), dash + gap):
            draw.line((x, y1, min(x + dash, max(x1, x2)), y2), fill=fill)


def create_layout_guide(path: Path, state: str, frames: int) -> dict[str, Any]:
    cell_width = ATLAS["cellWidth"]
    cell_height = ATLAS["cellHeight"]
    width = cell_width * frames
    image = Image.new("RGB", (width, cell_height), "#f7f7f7")
    draw = ImageDraw.Draw(image)
    margin_x = 18
    margin_y = 16
    for index in range(frames):
        left = index * cell_width
        right = left + cell_width - 1
        draw.rectangle((left, 0, right, cell_height - 1), outline="#111111", width=2)
        safe = (left + margin_x, margin_y, right - margin_x, cell_height - 1 - margin_y)
        draw.rectangle(safe, outline="#2f80ed", width=2)
        center_x = left + cell_width // 2
        center_y = cell_height // 2
        draw_dashed_line(draw, (center_x, safe[1]), (center_x, safe[3]), "#b8b8b8")
        draw_dashed_line(draw, (safe[0], center_y), (safe[2], center_y), "#b8b8b8")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return {
        "state": state,
        "path": str(path),
        "width": width,
        "height": cell_height,
        "frames": frames,
        "usage": "layout-only input; generated art must not copy guide lines",
    }


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def base_prompt(args: argparse.Namespace, chroma: dict[str, Any]) -> str:
    notes = args.pet_notes or args.description or "the pet in the reference images"
    return f"""
Create one clean full-body reference sprite for desktop pet {args.display_name}.

Pet identity: {notes}.
Style: {args.style_notes or "compact readable desktop-pet mascot, consistent silhouette, simple face, crisp edges"}.

Place a single centered pose on a perfectly flat pure {chroma["name"]} {chroma["hex"]} chroma-key background.
Keep the full pet visible, readable in a 192x208 cell, and easy to animate.
No scenery, text, frame borders, checkerboard transparency, shadows, glows, detached effects, or extra unrequested props.
Keep the chroma-key color and nearby colors out of the pet, props, highlights, and effects.
"""


def row_prompt(args: argparse.Namespace, state: str, frames: int, purpose: str, chroma: dict[str, Any]) -> str:
    notes = args.pet_notes or args.description or "the same pet from the approved base reference"
    return f"""
Create one horizontal animation strip for desktop pet `{args.pet_id}`, state `{state}`.

Use the attached canonical base image for identity. Use the attached layout guide only for slot count, spacing, centering, and padding; do not draw the guide.

Output exactly {frames} full-body frames in one left-to-right row on flat pure {chroma["name"]} {chroma["hex"]}.
Treat the row as {frames} invisible equal-width slots: one centered complete pose per slot, evenly spaced, with no overlap, clipping, empty slots, labels, or borders.

Identity lock: same pet in every frame: {notes}. Preserve silhouette, face, proportions, markings, palette, material, style, and props.
Animation purpose: {purpose}.
State rule: {STATE_RULES[state]}

Keep apparent scale and baseline stable within the row unless the state intentionally changes vertical position, such as jumping.
Clean extraction: crisp opaque edges, safe padding, no scenery, text, guide marks, checkerboard, shadows, glow, blur, speed lines, dust, detached effects, stray pixels, or chroma-key colors inside the pet.
"""


def rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a spritesheet pet generation run folder.")
    parser.add_argument("--pet-id", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--reference", action="append", default=[], type=Path)
    parser.add_argument("--pet-notes", default="")
    parser.add_argument("--style-notes", default="")
    parser.add_argument("--chroma-key", default="auto")
    parser.add_argument("--output-dir", type=Path, default=Path("spritesheet-runs"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    args.pet_id = slugify(args.pet_id)
    run_dir = args.output_dir.expanduser().resolve() / args.pet_id
    if run_dir.exists() and any(run_dir.iterdir()) and not args.force:
        raise SystemExit(f"{run_dir} already exists and is not empty; pass --force to reuse it")
    run_dir.mkdir(parents=True, exist_ok=True)

    reference_dir = run_dir / "references"
    copied_refs: list[dict[str, str]] = []
    copied_paths: list[Path] = []
    for index, source in enumerate(args.reference, start=1):
        source = source.expanduser().resolve()
        if not source.is_file():
            raise SystemExit(f"reference not found: {source}")
        target = reference_dir / f"reference-{index:02d}{source.suffix or '.png'}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied_refs.append({"path": rel(target, run_dir), "source": str(source), "role": "pet reference"})
        copied_paths.append(target)

    chroma = choose_chroma_key(copied_paths, args.chroma_key)
    layout_guides = [
        create_layout_guide(reference_dir / "layout-guides" / f"{state}.png", state, frames)
        for state, _row, frames, _durations, _purpose in ROWS
    ]

    write(run_dir / "prompts" / "base.md", base_prompt(args, chroma))
    for state, _row, frames, _durations, purpose in ROWS:
        write(run_dir / "prompts" / "rows" / f"{state}.md", row_prompt(args, state, frames, purpose, chroma))

    states = {
        state: {"row": row, "frames": frames, "frameDurations": durations}
        for state, row, frames, durations, _purpose in ROWS
    }
    request = {
        "schemaVersion": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "petId": args.pet_id,
        "displayName": args.display_name,
        "description": args.description,
        "renderer": "spritesheet",
        "atlas": ATLAS,
        "states": states,
        "references": copied_refs,
        "layoutGuides": [{**guide, "path": rel(Path(str(guide["path"])), run_dir)} for guide in layout_guides],
        "chromaKey": chroma,
        "petNotes": args.pet_notes,
        "styleNotes": args.style_notes,
    }
    (run_dir / "pet_request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    jobs: list[dict[str, Any]] = [{
        "id": "base",
        "kind": "base",
        "status": "pending",
        "promptFile": "prompts/base.md",
        "inputImages": copied_refs,
        "outputPath": "decoded/base.png",
        "dependsOn": [],
    }]
    for state, _row, frames, _durations, _purpose in ROWS:
        jobs.append({
            "id": state,
            "kind": "row-strip",
            "status": "pending",
            "promptFile": f"prompts/rows/{state}.md",
            "inputImages": [
                *copied_refs,
                {"path": "decoded/base.png", "role": "canonical base identity"},
                {"path": f"references/layout-guides/{state}.png", "role": f"layout guide for {frames} slots; do not copy guide lines"},
            ],
            "outputPath": f"decoded/{state}.png",
            "dependsOn": ["base"],
        })
    (run_dir / "imagegen-jobs.json").write_text(json.dumps({"schemaVersion": 1, "jobs": jobs}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"ok": True, "runDir": str(run_dir), "request": str(run_dir / "pet_request.json"), "jobs": str(run_dir / "imagegen-jobs.json")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
