# pi-exr-reader

Pi extension that lets any pi session read `.exr` files — something pi's
`read` tool can't do.

The `exr_read` tool reports:

- **Part metadata** — size, data window, storage, line order, compression,
  for every part of a multilayer EXR
- **Per-channel statistics** — min / max / mean / nonzero fraction, per
  component (`UV_Box.R`, `st.G`, ...)
- **Exact float pixel values** at a point, full precision
- **Rect statistics** over an inclusive region
- **PNG preview** — 8-bit linear-mapped (or viewer-look tone-mapped) image
  attached to the tool result when the active model accepts image input

Built for Arnold multi-AOV renders and UV/ST map inspection, but works on
any EXR: scanline or tiled, any compression, multilayer or single-layer.

## Install

```
pi install npm:pi-exr-reader
```

or from a local clone:

```
pi install /path/to/this/folder
```

## Requirements

The extension shells out to a Python probe
(`exr_reader/exr_probe.py`) using OpenEXR 3.x, numpy, and Pillow:

```
pip install OpenEXR numpy Pillow
```

The extension probes `python` / `python3` and fails with an install hint
if the packages are missing.

## Usage

Ask your model to inspect an EXR, e.g. *"what are the UV values at pixel
(540, 540) in render.exr"*. Coordinates are `(x, y)` with origin at the
top-left of the data window, y increasing down — matching the preview
PNG orientation.

Channel names are case-insensitive; a group name selects all its
components (`"uv_box"` → `UV_Box.R/.G/.B`).

## Development

Source lives in `exr_reader/` (`index.ts` + `exr_probe.py`). No build
step — pi loads the TypeScript directly. The production global install on
the author's machine is a copy at `~/.pi/agent/extensions/exr_reader/`;
re-copy after changes (or `pi -e <this folder>` to test from source).
