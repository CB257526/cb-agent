# Spritesheet Desktop Pet Package Contract

This contract is app-neutral. A valid package directory can be loaded by any runtime that implements the `pet.json` spritesheet protocol.

## Compatibility Targets

| Target | Renderer | Expectation |
|---|---|---|
| cb-agent desktop pet runtime | `spritesheet` | Works with `pet.json` plus a transparent atlas. |
| Other compatible runtimes | `spritesheet` | Works if they implement this contract. |
| Unmodified upstream BongoCat | `live2d` | Not a target. Upstream BongoCat needs `.model3.json`, `.moc3`, and textures. |

Agents may generate spritesheet packages directly from reference images. Agents should not promise reference-image-to-Live2D `.moc3` generation unless a separate Cubism rigging pipeline is available.

## Required Files

- `pet.json`
- atlas image referenced by `pet.json.spritesheetPath`

Recommended:

- `resources/cover.png`
- `resources/background.png`

## Manifest

```json
{
  "id": "demo-pet",
  "displayName": "Demo Pet",
  "description": "One short sentence.",
  "renderer": "spritesheet",
  "spritesheetPath": "atlas.png",
  "atlas": {
    "width": 1536,
    "height": 1872,
    "columns": 8,
    "rows": 9,
    "cellWidth": 192,
    "cellHeight": 208
  },
  "states": {
    "idle": { "row": 0, "frames": 6, "frameDurations": [280, 110, 110, 140, 140, 320] },
    "running-right": { "row": 1, "frames": 8, "frameDurations": [120, 120, 120, 120, 120, 120, 120, 220] },
    "running-left": { "row": 2, "frames": 8, "frameDurations": [120, 120, 120, 120, 120, 120, 120, 220] },
    "waving": { "row": 3, "frames": 4, "frameDurations": [140, 140, 140, 280] },
    "jumping": { "row": 4, "frames": 5, "frameDurations": [140, 140, 140, 140, 280] },
    "failed": { "row": 5, "frames": 8, "frameDurations": [140, 140, 140, 140, 140, 140, 140, 240] },
    "waiting": { "row": 6, "frames": 6, "frameDurations": [150, 150, 150, 150, 150, 260] },
    "running": { "row": 7, "frames": 6, "frameDurations": [120, 120, 120, 120, 120, 220] },
    "review": { "row": 8, "frames": 6, "frameDurations": [150, 150, 150, 150, 150, 280] }
  }
}
```

## Atlas Rules

- Default size: `1536x1872`.
- Default grid: `8x9`.
- Default cell: `192x208`.
- Transparent PNG or WebP is preferred.
- Used cells must contain the pet.
- Unused cells after a state's frame count should remain fully transparent.
- Preserve consistent character identity, scale, and anchor point across rows.
- Avoid baked-in labels, UI text, watermarks, visible guide marks, frame borders, cropped body parts, and opaque cell backgrounds.

## Generation Pipeline

For reference-image generation, prefer:

1. canonical base image
2. one chroma-key row strip per state
3. deterministic chroma-key removal and slot slicing
4. atlas assembly
5. packaging and validation

This avoids relying on an image model for exact atlas geometry.

## Validation Expectations

Validation should fail for missing required files, invalid JSON, missing atlas image, atlas dimension mismatch, invalid state rows, frame ranges that exceed atlas columns, missing frame timing, or mismatched `frameDurations` length.

Validation should warn for missing optional cover/background, images without alpha, unknown states, or IDs that are not portable across filesystems.
