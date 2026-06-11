---
name: hatch-bongocat-pet
description: Generate, assemble, validate, and package lightweight spritesheet desktop pets from reference images, character concepts, generated row strips, or finished atlas art. Use when a user wants an agent to create a cb-agent-compatible animated pet with pet.json, transparent atlas art, state rows, frame timing, layout guides, and QA-friendly packaging.
---

# Hatch Spritesheet Pet

## Core Contract

Create portable spritesheet desktop pet packages. The package folder is the deliverable and must not depend on the agent that created it, a fixed home directory, a fixed app name, or a specific image provider.

This skill targets the `pet.json` spritesheet protocol:

- `pet.json`
- one transparent atlas image such as `atlas.png`
- optional `resources/cover.png`

This is not unmodified upstream BongoCat's native Live2D format. It does not create `.moc3`, Cubism rigging, or layered PSD files. If the user needs upstream BongoCat native import, they need existing Live2D Cubism assets.

Read `references/bongocat-pet-contract.md` when you need exact manifest fields or row layout.

## Learned Production Pattern

Follow the practical hatch-pet idea: do not rely on one giant AI-generated atlas unless the user already has one that validates. Prefer:

1. Generate a canonical base pet image.
2. Generate one horizontal row strip per animation state, grounded by the base image and a layout guide.
3. Use flat chroma-key backgrounds in generated rows.
4. Assemble row strips deterministically into a transparent atlas.
5. Validate and package the atlas with `pet.json`.

This separates creative generation from geometry, transparency, and packaging. It makes the result much more reliable than asking an image model to produce perfect `1536x1872` atlas geometry in one shot.

## Workflow

1. Prepare the run folder.
   - Use `scripts/prepare_spritesheet_run.py --pet-id <id> --display-name <name> [--reference <image>] [--output-dir <dir>]`.
   - It creates `pet_request.json`, `imagegen-jobs.json`, row prompts, and layout guide images.

2. Generate images.
   - Generate `base` first from `prompts/base.md`.
   - Copy the selected base output to `decoded/base.png`.
   - Generate each row strip from `prompts/rows/<state>.md`, using `decoded/base.png` and `references/layout-guides/<state>.png` as inputs.
   - Copy selected row outputs to `decoded/<state>.png`.
   - Use the current agent's image tool. If no image tool is available, provide the prompts and wait for the user to supply the images.

3. Assemble the atlas.
   - Run `scripts/assemble_spritesheet_atlas.py --run-dir <run-dir>`.
   - The script removes the chroma-key background, slices row slots, fits each frame into `192x208`, clears transparent RGB residue, and writes `final/atlas.png`.

4. Package.
   - Run `scripts/package_spritesheet_pet.py --pet-id <id> --display-name <name> --spritesheet <atlas.png> [--output-dir <dir>]`.
   - The script writes `pet.json`, copies the atlas, and creates `resources/cover.png` from the first idle cell when no cover is supplied.

5. Validate.
   - Run `scripts/validate_bongocat_pet.py <package-folder>`.
   - Fix missing files, invalid manifest fields, atlas dimension mismatch, frame range issues, and transparency warnings before reporting completion.

6. Report.
   - Give the package folder path, renderer type, and validation status.
   - State that the package targets runtimes implementing this spritesheet protocol, including cb-agent.
   - Do not claim it is an upstream BongoCat native Live2D package.

## Default Layout

- Atlas: `1536x1872`
- Grid: `8` columns by `9` rows
- Cell: `192x208`
- Background: transparent after assembly

Rows:

| Row | State | Frames | Purpose |
|---:|---|---:|---|
| 0 | `idle` | 6 | calm breathing/blinking baseline |
| 1 | `running-right` | 8 | directional drag movement right |
| 2 | `running-left` | 8 | directional drag movement left |
| 3 | `waving` | 4 | greeting gesture |
| 4 | `jumping` | 5 | anticipation, lift, peak, descent, settle |
| 5 | `failed` | 8 | sad or blocked reaction |
| 6 | `waiting` | 6 | needs user input |
| 7 | `running` | 6 | active task work or processing |
| 8 | `review` | 6 | focused review/thinking |

The runtime may use `frameDurations` for held final frames. Use uniform `durationMs` only when exact per-frame timing is unnecessary.

## Image Guidance

Preserve the reference character's silhouette, palette, outfit, hairstyle, face shape, proportions, and signature props. Keep the pet compact, full-body, centered, readable at `192x208`, and consistent across rows.

Generated rows should use:

- one complete pose per invisible slot
- stable scale and anchor point
- flat chroma-key background from `pet_request.json`
- no text, labels, watermarks, visible guide marks, frame borders, scenery, shadows, glows, speed lines, dust, or detached effects

If a row fails visually, regenerate the smallest failing row rather than restarting the whole pet.

## Scripts

- `scripts/prepare_spritesheet_run.py`
  Create prompts, layout guides, `pet_request.json`, and `imagegen-jobs.json`.
- `scripts/assemble_spritesheet_atlas.py`
  Convert generated chroma-key row strips under `decoded/` into transparent `final/atlas.png`.
- `scripts/package_spritesheet_pet.py`
  Create the final portable package with `pet.json`, atlas image, and optional cover/background resources.
- `scripts/validate_bongocat_pet.py`
  Validate spritesheet packages and print a structured report.

Use scripts for deterministic geometry, transparency, and packaging. Use visual inspection for identity, motion quality, and style consistency.
