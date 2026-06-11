#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any

try:
    import json5  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dependency
    json5 = None  # type: ignore[assignment]


PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            if json5 is None:
                raise
            value = json5.loads(text)
    except Exception as exc:
        return None, str(exc)
    if not isinstance(value, dict):
        return None, "JSON root must be an object"
    return value, None


def png_info(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(33)
    except OSError:
        return None
    if len(header) < 33 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", header[16:24])
    color_type = header[25]
    return {
        "width": width,
        "height": height,
        "has_alpha": color_type in {4, 6},
        "color_type": color_type,
    }


def check_referenced_file(root: Path, rel: Any, issues: list[str], label: str) -> None:
    if not rel:
        issues.append(f"missing {label} reference")
        return
    if not (root / str(rel)).is_file():
        issues.append(f"missing {label}: {rel}")


def validate_live2d(root: Path) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    model_files = sorted(root.glob("*.model3.json"))

    if not model_files:
        return {
            "path": str(root),
            "ok": False,
            "renderer": "unknown",
            "issues": ["missing *.model3.json"],
            "warnings": warnings,
        }

    model_file = model_files[0]
    model, error = read_json(model_file)
    if error:
        issues.append(f"invalid {model_file.name}: {error}")
        model = {}

    refs = model.get("FileReferences") if isinstance(model.get("FileReferences"), dict) else {}
    check_referenced_file(root, refs.get("Moc"), issues, "Moc")

    textures = refs.get("Textures")
    if not isinstance(textures, list) or not textures:
        issues.append("missing FileReferences.Textures")
    else:
        for texture in textures:
            check_referenced_file(root, texture, issues, "texture")

    for key in ("Physics", "DisplayInfo"):
        value = refs.get(key)
        if value and not (root / str(value)).is_file():
            issues.append(f"missing {key}: {value}")

    motions = refs.get("Motions")
    if isinstance(motions, dict):
        for group, items in motions.items():
            if not isinstance(items, list):
                warnings.append(f"Motions.{group} is not a list")
                continue
            for item in items:
                if isinstance(item, dict) and item.get("File"):
                    check_referenced_file(root, item.get("File"), issues, f"motion {group}")

    expressions = refs.get("Expressions")
    if isinstance(expressions, list):
        for item in expressions:
            if isinstance(item, dict) and item.get("File"):
                check_referenced_file(root, item.get("File"), issues, "expression")

    if not (root / "resources" / "cover.png").is_file():
        issues.append("missing resources/cover.png")
    if not (root / "resources" / "background.png").is_file():
        warnings.append("missing optional resources/background.png")

    right_keys = root / "resources" / "right-keys"
    right_key_files = list(right_keys.glob("*.png")) if right_keys.is_dir() else []
    if any("East" in item.stem for item in right_key_files):
        mode = "gamepad"
    elif right_key_files:
        mode = "keyboard"
    else:
        mode = "standard"

    return {
        "path": str(root),
        "ok": not issues,
        "renderer": "live2d",
        "mode": mode,
        "modelFile": model_file.name,
        "issues": issues,
        "warnings": warnings,
    }


def validate_spritesheet(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []

    pet_id = str(manifest.get("id") or "")
    if not pet_id:
        issues.append("missing id")
    elif not PORTABLE_ID.match(pet_id):
        warnings.append("id is not a portable ASCII filesystem id")

    if not manifest.get("displayName"):
        warnings.append("missing displayName")

    sprite_rel = manifest.get("spritesheetPath") or manifest.get("spritesheet")
    if not sprite_rel:
        issues.append("missing spritesheetPath")
        sprite_path = None
    else:
        sprite_path = root / str(sprite_rel)
        if not sprite_path.is_file():
            issues.append(f"missing spritesheet: {sprite_rel}")

    atlas = manifest.get("atlas")
    if not isinstance(atlas, dict):
        issues.append("missing atlas object")
        atlas = {}

    columns = int(atlas.get("columns") or 0)
    rows = int(atlas.get("rows") or 0)
    width = int(atlas.get("width") or 0)
    height = int(atlas.get("height") or 0)
    cell_width = int(atlas.get("cellWidth") or (width / columns if columns else 0))
    cell_height = int(atlas.get("cellHeight") or (height / rows if rows else 0))

    for key, value in {
        "atlas.width": width,
        "atlas.height": height,
        "atlas.columns": columns,
        "atlas.rows": rows,
        "atlas.cellWidth": cell_width,
        "atlas.cellHeight": cell_height,
    }.items():
        if value <= 0:
            issues.append(f"{key} must be positive")

    if width and columns and cell_width and width != columns * cell_width:
        issues.append("atlas.width does not match columns * cellWidth")
    if height and rows and cell_height and height != rows * cell_height:
        issues.append("atlas.height does not match rows * cellHeight")

    states = manifest.get("states")
    if not isinstance(states, dict) or not states:
        issues.append("missing states object")
    else:
        for name, state in states.items():
            if not isinstance(state, dict):
                issues.append(f"state {name} must be an object")
                continue
            row = int(state.get("row") or 0)
            column = int(state.get("column") or 0)
            frames = int(state.get("frames") or state.get("frameCount") or 0)
            duration = int(state.get("durationMs") or state.get("frameDurationMs") or 0)
            frame_durations = state.get("frameDurations") or state.get("durations")
            if frames <= 0:
                issues.append(f"state {name} frames must be positive")
            if duration <= 0 and not frame_durations:
                issues.append(f"state {name} durationMs or frameDurations required")
            if frame_durations is not None:
                if not isinstance(frame_durations, list):
                    issues.append(f"state {name} frameDurations must be a list")
                else:
                    if frames > 0 and len(frame_durations) != frames:
                        issues.append(f"state {name} frameDurations length must match frames")
                    for index, item in enumerate(frame_durations):
                        try:
                            value = int(item)
                        except (TypeError, ValueError):
                            issues.append(f"state {name} frameDurations[{index}] must be an integer")
                            continue
                        if value <= 0:
                            issues.append(f"state {name} frameDurations[{index}] must be positive")
            if rows and row >= rows:
                issues.append(f"state {name} row outside atlas")
            if columns and column + max(frames, 1) > columns:
                issues.append(f"state {name} frames exceed atlas columns")

    if sprite_path is not None and sprite_path.is_file():
        info = png_info(sprite_path)
        if info:
            if width and info["width"] != width:
                issues.append(f"PNG width {info['width']} does not match atlas.width {width}")
            if height and info["height"] != height:
                issues.append(f"PNG height {info['height']} does not match atlas.height {height}")
            if not info["has_alpha"]:
                warnings.append("PNG does not declare an alpha channel")
        else:
            warnings.append("could not inspect image dimensions; PNG is recommended")

    if not (root / "resources" / "cover.png").is_file():
        warnings.append("missing optional resources/cover.png")

    return {
        "path": str(root),
        "ok": not issues,
        "renderer": "spritesheet",
        "id": pet_id or None,
        "issues": issues,
        "warnings": warnings,
    }


def validate_package(path: Path) -> dict[str, Any]:
    root = path.expanduser().resolve()
    if not root.is_dir():
        return {
            "path": str(root),
            "ok": False,
            "renderer": "unknown",
            "issues": ["path is not a directory"],
            "warnings": [],
        }

    manifest_path = root / "pet.json"
    if manifest_path.is_file():
        manifest, error = read_json(manifest_path)
        if error:
            return {
                "path": str(root),
                "ok": False,
                "renderer": "spritesheet",
                "issues": [f"invalid pet.json: {error}"],
                "warnings": [],
            }
        if str(manifest.get("renderer") or "").lower() == "spritesheet":
            return validate_spritesheet(root, manifest)

    return validate_live2d(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate BongoCat-compatible pet packages.")
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true", help="print JSON only")
    args = parser.parse_args(argv)

    results = [validate_package(path) for path in args.paths]
    ok = all(item["ok"] for item in results)

    if args.json:
        print(json.dumps(results if len(results) > 1 else results[0], ensure_ascii=False, indent=2))
    else:
        for item in results:
            status = "ok" if item["ok"] else "failed"
            print(f"{status}: {item['path']} ({item['renderer']})")
            for issue in item.get("issues", []):
                print(f"  issue: {issue}")
            for warning in item.get("warnings", []):
                print(f"  warning: {warning}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
