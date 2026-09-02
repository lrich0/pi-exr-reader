# exr_loader — shared project context

Pi extension that lets any pi session read `.exr` files: full-precision
channel statistics, exact pixel values, rect statistics, and a
linear-mapped 8-bit PNG preview. Built for the LR_UVWarp
texture-mapping investigation (UV/ST map examination), but generic —
use it on any EXR.

## Why this exists

Pi's `read` tool cannot open EXR (only text + jpg/png/gif/webp/bmp), and
even where an image *can* reach a vision model, a VLM sees pixels — it
cannot report that pixel (540,540) has R=0.02978353202342987. This
extension gives the model both: exact float values (text, full precision)
and a view of the image structure (PNG preview, attached to the tool
result when the active model accepts image input).

## What's here

```
exr_loader/
├── AGENTS.md            — this file
├── docs/
│   ├── handoffs/        — dated session records
│   └── prompts/         — ready-to-paste prompts

# The tool itself is a GLOBAL pi extension (installed 2026-08-25):
~/.pi/agent/extensions/exr_reader/
├── index.ts             — the pi extension (registers the `exr_read` tool)
└── exr_probe.py         — the reader: OpenEXR 3.x + numpy + Pillow → JSON
```

This folder keeps the *docs* about the tool; the *code* lives in the
user-level pi extensions dir (auto-discovered for every project, no
per-project registration needed). Edit the installed copy — there is no
second source to keep in sync (the original in this folder was moved
2026-08-25, see Installation below).

## The tool: `exr_read`

```
exr_read(path, pixel?, rect?, channel?, preview?, previewToneMap?, previewOut?)
```

- `path` — the .exr file (absolute or relative to the session cwd)
- `pixel: [x, y]` — exact values at one pixel, every channel
- `rect: [x0, y0, x1, y1]` — per-channel min/max/mean/nonzero-fraction
  over an inclusive rect (part 0)
- `channel: ["R", "UV_Box", ...]` — case-insensitive filter for
  stats/pixel/preview. A **group name selects all its components**
  (`"uv_box"` → `UV_Box.R/.G/.B`); an exact component name
  (`"UV_Box.R"`) also works; the match is dot-anchored so `"uv"` does
  **not** match `UV_All.R`. A filter that matches nothing is an error
  that lists the available channel names (the old behaviour was a
  misleading "outside data window" error — fixed 2026-08-25).
- `preview: false` — skip the PNG (default: produced)
- `previewToneMap: "auto" | "linear" | "tonemap"` — per-component
  preview mapping (default `auto`). **linear** = full [min,max] →
  full range (maps/LDR, honest full-range view). **tonemap** = the
  **viewer look**: 1.0 = white, sRGB gamma, clip above — i.e. what
  Nuke/Photoshop/AE show for an EXR, a display-referred image vision
  encoders are trained on. (First attempt was Reinhard max→white;
  rejected 2026-08-25 — it read dark/flat, not the viewer look.)
  **auto**: linear when data fits ~[0, 1.05]; tonemap for image-like
  HDR (min ≈ 0, max > 1.05); linear full-range for data with large
  negative spans (P/Z AOVs — e.g. P.B min −40: everything shown,
  not clipped). The per-component mapping used is reported in the
  preview JSON's `mapping`.
- `previewOut` — where the preview PNG lands. Default:
  **`<cwd>/images/previews/<filename>[_<channel-tag>]_preview.png`**
  (project-local; the channel tag — e.g.
  `Box-on-alpha_uv_aovs_uv_all_preview.png` — keeps previews of
  different channel selections of the same file from overwriting each
  other; stable names mean re-runs refresh the same file). The path is
  reported in the tool result.

Always returns (on success):
- part metadata: width/height, data window, storage (scanline/tiled),
  line order, compression — for **every** part of a multilayer EXR
- per-channel stats (min/max/mean/nonzero fraction, per component for
  multi-component channels like `st` or `RGBA`)
- if `preview` and the model has image input: an 8-bit PNG, one channel
  per RGB slot — **named R/G/B channels are matched by name and sorted
  into R,G,B order regardless of file channel order** (this file lists
  its channels A,B,G,R; file order put B in the red slot — verified
  bug 2026-08-25); otherwise falls back to the first up-to-3 channels.
  The JSON reports `slots` (which source channel landed in which color
  slot), `mapping` (per-component linear/tonemap), and each
  component's source [min,max] — **structure visible, absolute values
  not** (the JSON carries the scales so values can be read off). Each
  component is mapped per `previewToneMap` (auto: linear for ~[0,1]
  data, Reinhard+sRGB for HDR/negative). Preview is capped at 2048 px
  on the long edge (1080² and 960×540 pass through unresized).
  Note: the preview writes into `<cwd>/images/previews/` by default —
  the tool runs through pi's `withFileMutationQueue` so parallel calls
  on the same file can't race on the same preview path.

## Coordinate convention (verified, don't re-derive)

`(x, y)` with origin at the **top-left** of the part's data window,
x increasing right, y increasing **down**. This matches the preview PNG
orientation (verified 2026-08-25 with a ramp test file through a
round-trip). So a point seen at (px, py) in the preview is queryable as
`pixel [px, py]` — after scaling by `preview.size / part size` if the
preview was downscaled (long-edge cap 2048 px).

## Arnold AOV files (the real use case)

A real multi-AOV render for this work:
`D:\_WDrive\Jobs\_INTERNAL\LR_UVWarp\Box-on-alpha_uv_aovs.exr`
(960×540, single part, ZIP; channels: beauty `R,G,B,A`, `UV_All`,
`UV_Box`, `P`, `TextureGrid`, `Z`). Arnold puts AOVs in **one part as
extra channels**, not as separate parts. Extraction status (verified
2026-08-25 against that file):

- **stats**: every part, every channel ✓
- **pixel**: every part; an unfiltered pixel query returns *all* AOVs
  at that point (beauty + UV + P + Z in one call) ✓
- **rect + preview**: **part 0 only** (no `part:` parameter yet — a
  known gap if a UV AOV ever lives in part 1 of a true multi-part
  file; single-part Arnold AOVs are unaffected) ⚠️
- **channel filter**: group names work (`UV_Box` → its .R/.G/.B) ✓
- **UV AOV preview**: channel-group + R/G/B slot ordering compose
  U→red, V→green — the map reads correctly as a red-green ramp (the
  same slot ordering that fixed the R/B swap on beauty files) ✓

## Gotchas (verified on this machine, 2026-08-25)

1. **OpenEXR 3.x Python API is a method/property mix.** On `File`:
   `header()`/`channels()` are *methods*; on `Part`: `name/width/height/
   type/compression` are *methods* but `channels`/`header` are *dict
   properties*. The probe handles both via `call_or_val()`.
2. **`separate_channels=True`** gives one 2-D array per channel
   (R, G, B, A, st, ...). Without it the reader returns a single
   combined `RGBA` (or `st`) 3-D array with components in
   **file chlist order** — this Arnold file lists its channels
   **A, B, G, R** (the observed dict order), so the combined array's
   component 0 is A, component 1 is B, etc. Never assume component
   order without `separate_channels=True` — and never assign channels
   to color slots by iteration order (see #6).
3. **OpenCV on this machine has its EXR codec disabled** — `cv2.imread`
   on an EXR throws. Do not use cv2 for EXR; use the probe (or OpenEXR
   directly).
4. **Tiled + RANDOM_Y files are common** (Arnold outputs
   `Storage.tiledimage`, `LineOrder.RANDOM_Y`). The OpenEXR Python API
   reads them transparently — no special handling, but expect it.
5. **Pillow cannot encode 16-bit RGB PNGs — and it will not tell you.**
   `Image.fromarray(uint16_array, "RGB")` silently reinterprets the raw
   2-byte values as 8-bit byte pairs: the saved file is 8-bit and the
   image is scrambled (each source pixel's 6 bytes become two output
   pixels — a low-byte pixel and a black high-byte pixel; rows
   alternate left-half/right-half; the buffer is 2× too long so only
   the first half of the image survives). This exact bug shipped
   2026-08-25: the first preview looked like "random hued dots on
   black, head split in two, zoomed to the head only" — the user caught
   it and the bytes confirmed it (wrote a known uint16 array through the
   same path, got back the byte pairs). Always convert to uint8
   explicitly before `fromarray`. The preview is now a plain 8-bit
   linear remap; don't sample values from it — use `pixel`/`rect`.
6. **EXR channel order is file order, not RGB order.** This Arnold file
   lists `A, B, G, R`, so assigning channels to RGB slots by iteration
   order silently puts B in the red slot and R in the blue slot (R/B
   swap) — a second preview bug, caught the same day. The probe matches
   named R/G/B channels by name and sorts them into R,G,B order, and
   reports the mapping in the preview JSON's `slots`.
7. The preview's per-component remap hides absolute values
   deliberately (a UV ramp and a sky render both span 0..1; the data,
   not the pixels, is what's being examined).

## Python environment (this machine)

- Python 3.10.2 at `C:\Program Files\Python\python.exe` (`python` on
  PATH)
- `OpenEXR 3.4.15` (pip), `numpy 1.26.4`, `Pillow 11.0.0` — all present
- The extension probes `python`/`python3` with
  `import OpenEXR, numpy` and fails with an install hint otherwise

## Installation

**Global (done 2026-08-25 — the live install):**
`~/.pi/agent/extensions/exr_reader/` — auto-discovered by pi for
*every* project with no per-project registration. Global extensions
load before project-trust resolution, so no trust prompt is involved.
`index.ts` finds `exr_probe.py` relative to itself (`__dirname`), so
the folder can be moved/renamed freely as long as both files stay
inside it.

- **One-off test from elsewhere:**
  `pi -e <path-to>/exr_reader`
- **Uninstall:** delete the `~/.pi/agent/extensions/exr_reader/` folder.

History: first installed 2026-08-25 by absolute path in
LR_UVWarp's `.pi/settings.json` (commit `0fa0745`), moved to the
global extensions dir the same day (LR_UVWarp commit `91aa553`
removed the registration — paired change).

## Verification state

- 2026-08-25 (later): AOV channel-group matching + honest no-match
  error added; verified against the real
  `Box-on-alpha_uv_aovs.exr` (group `UV_Box` pixel query, typo filter
  error, case-insensitivity, dot-anchoring, UV preview U→red/V→green
  byte-identical to an independent rebuild, end-to-end pi run).
  Remaining gap: `rect`/`preview` are part 0 only.
- 2026-08-25 (later): preview tone-mapping (`previewToneMap`, auto
  linear/tonemap per component) + project-local preview default
  (`<cwd>/images/previews/<file>[_<ch>]_preview.png`) +
  `withFileMutationQueue` around the preview write. Verified:
  auto kept LDR UV previews byte-identical to the previous linear
  output (max diff 0), forced linear on HDR reproduced the old linear
  preview (max diff 0), default naming verified in the project folder,
  end-to-end pi run OK. **Curve revision (later same day):** first
  tonemap was Reinhard max→white — user reported the alexi preview
  still read dark vs. the original; the curve is now the **viewer
  look** (1.0 = white, sRGB gamma, clip). Also, the file on disk was
  a forced-linear one from a smoke test (smoke tests can leave
  artifacts in the project — noted). Re-verified: new alexi preview
  byte-identical to an independent rebuild (max diff 0), mean 24.0
  (was 14.2 Reinhard, ~2–5 linear); P AOV auto-maps per component
  (P.G [0,1.99] → tonemap, P.R/P.B large negative spans → linear
  full-range); UV preview still byte-identical (max diff 0).
  LR_UVWarp `.gitignore` now ignores `images/previews/` (regenerable
  artifacts; source .exr files live outside the project).
- 2026-08-25: probe verified against a real Arnold EXR
  (1080×1080, tiled, ZIP, RGBA) — stats, pixel, rect, channel filter,
  error paths all exercised; orientation verified with a ramp
  round-trip; end-to-end pi runs verified twice (via `-e` and via
  project `.pi/settings.json`) — model returned exact values.
  **The preview was NOT visually verified that day** (the session model
  has no vision), so "preview verified" in the handoff really meant
  "a file was produced." The user opened the PNG and it was scrambled —
  two bugs, both fixed and re-verified by byte-level comparison against
  an independent reconstruction (max diff 0): (1) uint16→PNG byte
  reinterpretation, (2) R/B swapped via file channel order. Lesson:
  a preview no one can see is not a verified preview — say so.
  See `docs/handoffs/2026-08-25_exr-loader-built-verified.md`.

## Precedence

Code (`~/.pi/agent/extensions/exr_reader/`) > this file >
docs/handoffs. If a document disagrees with the code, the code wins.

## Version control

Unversioned by choice (same reasoning as the other `python/` work in
this workspace) — it's a tool, not a project to publish. The installed
copy under `~/.pi/agent/extensions/` is therefore not in any repo;
the docs here are the only durable record. If it grows a user base
beyond this machine, it gets its own repo (and the `~/.pi` install
becomes a copy step from it).
