# Handoff — 2026-08-25: exr_loader built and verified

**Session:** pi (this folder). **Work landed:** all files in this folder
written and tested before this handoff was written.

## What happened

The 2026-08-24 LR_UVWarp session established that pi cannot inspect
EXR files (read tool: no EXR; vision model: no EXR input, and a VLM
can't report channel values anyway) and agreed on this design: a pi
extension (`exr_read` tool) backed by a Python probe (OpenEXR 3.x +
numpy + Pillow), living in `D:\_claude\python\pi\exr_loader` as its own
mini-project with AGENTS.md + docs. That session then died on
`Error: 400: Failed to tokenize prompt` (llama-server side) before the
files were written. This session rebuilt the project from the recovered
session record and completed + verified it.

## Built

- **`exr_reader/exr_probe.py`** — the reader. Opens an EXR with
  `OpenEXR.File(path, separate_channels=True)` (handles multilayer,
  tiled, all compressions), emits one JSON object on stdout:
  per-part metadata (size, data window, storage, line order,
  compression), per-channel stats (min/max/mean/nonzero_frac, per
  component), optional `--pixel x y` (exact float values, all
  channels), optional `--rect x0 y0 x1 y1` (stats over rect, part 0),
  optional `--channel NAME` filter (repeatable, case-insensitive),
  and a 16-bit linear-mapped PNG preview (`--preview-out`,
  `--max-preview`, `--no-preview`). Non-finite floats are stringified
  (`"NaN"`/`"inf"`) so the output is strict JSON. Exit 0 on success
  including `{"ok": false}` results (errors carry structured detail);
  non-zero only for hard crashes.
- **`exr_reader/index.ts`** — the pi extension. Registers `exr_read`
  (TypeBox schema: `path`, `pixel`, `rect`, `channel`, `preview`,
  `previewOut`). Locates a working python (`python`/`python3` probed
  with `import OpenEXR, numpy`), runs the probe via
  `node:child_process.execFile` (16 MB maxBuffer, 300 s timeout,
  abort-signal aware), parses the JSON, and returns: the preview PNG
  as a base64 image block **only when the active model declares image
  input** (`ctx.model.input` includes `"image"`), plus the JSON text
  (truncated at 12 k chars with a hint) and a short note on
  coordinates + preview semantics. Tool text is kept deliberately
  compact so the 27B local model's context isn't bloated.

  ⚠️ **Post-session correction (added after the user caught it):**
  the preview PNG was produced but **scrambled by two bugs** (uint16
  byte reinterpretation + R/B swap). Everything below that says
  "preview verified" is wrong — see the addendum at the bottom of
  this file. Stats/pixel/rect/error paths are unaffected by those
  bugs and remain verified.

## Key facts verified (the re-derivation cost)

1. **Orientation:** array row 0 = image top. Confirmed with a ramp
   test file (rows 0..5 carrying 0..50) written via
   `OpenEXR.File({...}, {"R": arr}); f.write(path)` and round-tripped
   through a PNG — top row of the PNG = row 0 of the array. So
   `(x, y)` top-left origin, y down, matches the preview.
2. **Component order:** with `separate_channels=True` each channel is
   its own named 2-D array — no ambiguity. Without it, the combined
   array's components follow the **file chlist order**; an Arnold
   file lists `A, R, G, B` (verified by parsing the header chlist of
   `alexi_shitRender_v01__10001.exr`), which happens to match the
   RGB+alpha layout the code expects *for that file* — hence the
   probe always uses `separate_channels=True`.
3. **The Part API mix:** `File.header()/channels()` are methods;
   `Part.channels/header` are dict properties while
   `Part.name/width/height/type/compression` are methods.
4. **`execCommand` is not exported** from `@earendil-works/pi-coding-agent`
   (it lives in `dist/core/exec.js` but the package index doesn't
   re-export it) — first end-to-end run failed with
   `(0, _piCodingAgent.execCommand) is not a function`; switched to
   `node:child_process.execFile` (docs confirm Node built-ins are
   available to extensions).
5. **Real-file sanity:** `C:\Users\Luke - WS\Desktop\ff_alexi_model\
   images\alexi_shitRender_v01__10001.exr` — 1080×1080,
   `Storage.tiledimage`, `LineOrder.RANDOM_Y`, ZIP, channels A/B/G/R,
   R max 5.444 (HDR), alpha nonzero fraction 0.326. Pixel (540,540):
   R=0.02978353202342987, G=0.031442657113075256,
   B=0.04693956300616264, A=1.0.

## Verified (end-to-end, local Qwen3.8-27B)

- `pi -p "…call exr_read…" -e D:/_claude/python/pi/exr_loader/exr_reader`
  → model called the tool and reported the exact pixel values above.
- From the LR_UVWarp project cwd **without** `-e` (i.e. loaded via
  `D:\_claude\aftereffects\LR_UVWarp\.pi\settings.json` →
  `"extensions": ["D:/_claude/python/pi/exr_loader/exr_reader"]`) →
  same result. Project trust: `D:\_claude\aftereffects` is trusted in
  `~/.pi/agent/trust.json`, covering LR_UVWarp.
- Probe error paths: missing file, out-of-range pixel (returns the
  data window), channel filter — all exercised.
- Preview: 1024×1024 16-bit PNG produced; per-component scales
  reported in the JSON.

## Changed outside this folder

- `D:\_claude\aftereffects\LR_UVWarp\.pi\settings.json` — added
  `"extensions": [...]` (tracked file; commit in the LR_UVWarp repo:
  "wire exr_read tool into project pi settings").

## Addendum — 2026-08-25 (later, same day): preview bugs found and fixed

The user opened `test_preview.png` (recreated from the same code) and
it looked like **random hued dots on black, in the shape of the head
split in two, zoomed to the head only, R/B hues wrong**. Root causes,
both in `exr_probe.py`'s preview path (stats/pixel/rect were never
affected):

1. **Pillow cannot encode 16-bit RGB PNGs** — and
   `Image.fromarray(uint16_array, "RGB")` does not downscale: it
   reinterprets the raw little-endian 2-byte values as 8-bit pairs.
   Proof: a known uint16 pixel `(1000, 2000, 3000)` round-tripped to
   `(232, 3, 208), (7, 184, 11)` = the bytes `E8 03 D0 07 B8 0B` read
   in stride 1. Consequences, exactly as observed: each source pixel's
   6 bytes become two output pixels (low-bytes = real but dark color,
   high-bytes = black) → "dots interspersed with black"; each output
   row is half a source row (even rows = left half, odd = right half)
   → "split in two"; the 2×-too-long buffer meant only the first half
   of the image survived → "zoomed to the head"; and the file's IHDR
   confirmed 8-bit, not the 16-bit the code claimed.
2. **R/B swapped.** Preview channel selection iterated the file's
   channel dict in file order; this Arnold file lists channels
   `A, B, G, R`, so B landed in the red slot and R in the blue slot.

**Fixes:** explicit uint16→uint8 conversion (divide by 257, round,
clip) before `fromarray`; R/G/B channels matched by name and sorted
into R,G,B order; preview JSON now reports `slots` (source channel →
color slot) and the honest `bitDepth`/`to` ranges; the 1024 px cap
raised to 2048 (1080² passes through unresized).

**Re-verification (byte-level, since the session model has no vision):**
independent reconstruction (EXR → remap → uint8 → 1024² LANCZOS) vs the
regenerated file: **max per-pixel diff 0**; IHDR 1024×1024 8-bit RGB;
`slots: {R:R, G:G, B:B}`. The regenerated file is at
`images/test_preview.png`.

**Process failure, honestly stated:** this handoff's "Preview: …
verified" meant "a PNG file was produced". No one could see it (no
vision in the session), and I wrote it as if it had been checked. The
user's eyes did the verification that was missing. Lesson recorded in
`AGENTS.md` (Verification state + Gotcha #5): a preview no one can see
is not a verified preview — flag the gap instead of claiming it.

## Open items / next steps

- **Not committed anywhere** — this folder is unversioned by choice
  (see AGENTS.md); the one cross-folder change is the LR_UVWarp
  settings.json (commit it there).
- The original *reason* for this tool — debugging LR_UVWarp's open
  texture-mapping issue (handoff
  `2026-08-24_warp-pipeline-built-verified-mapping-open.md`, top item)
  — is now actionable: a UV/ST map EXR can be loaded and its exact
  (U,V) values examined at any pixel, compared against the warp's
  expected Source-sample coordinate.
- If the 400 tokenize errors recur in pi sessions, that's a
  llama-server-side issue (prompt/tokenizer), unrelated to this tool —
  the 2026-08-24 session chased it as far as confirming
  `/props` reports `modalities.vision: true` on the running server.

## Addendum — 2026-08-25 (end of day): AOV work, tone-mapping, global install

Same-day follow-on work after the above (full verification record in
`AGENTS.md` → Verification state):

1. **Channel-group matching + honest no-match error** — `--channel UV_Box`
   now selects `UV_Box.R/.G/.B` (dot-anchored prefix; `uv` does NOT
   match `UV_All.R`); a filter matching nothing errors with the list of
   available channel names instead of the misleading
   "outside data window" failure. Verified on the real multi-AOV file
   `D:\_WDrive\Jobs\_INTERNAL\LR_UVWarp\Box-on-alpha_uv_aovs.exr`.
   Remaining gap: `rect`/`preview` are part 0 only.
2. **Preview tone-mapping** (`previewToneMap`, default `auto`) —
   per-component: linear for ~[0,1] data (UV maps, LDR — unchanged,
   byte-identical), viewer-look (1.0 = white, sRGB gamma, clip) for
   image-like HDR, linear full-range for data with large negative
   spans (P/Z). The first implementation used Reinhard max→white;
   the user found the result still read dark and it was replaced by
   the viewer look the same day. Note: a forced-`linear` smoke-test
   run left that artifact as the project preview file — smoke tests
   can leave artifacts in the project folder; regenerate or check
   which variant a preview file is before trusting it.
3. **Preview location/naming** — default is now
   `<cwd>/images/previews/<file>[_<channel-tag>]_preview.png`
   (project-local, stable, idempotent on re-run); the extension wraps
   the write in pi's `withFileMutationQueue`. LR_UVWarp's
   `.gitignore` ignores `images/previews/` (commit `10e76cf`).
4. **Global install (this addendum's reason)** — `exr_reader/` moved
   from this folder to `~/.pi/agent/extensions/exr_reader/`; the
   per-project registration in LR_UVWarp's `.pi/settings.json` was
   removed (commit `91aa553`, paired with the move). Verified: tool
   present in a never-registered directory, and in LR_UVWarp after
   registration removal. Consequence: previews now land in the
   *active project's* `images/previews/` in whatever directory pi is
   running — fine for project work, but ad-hoc runs from a bare cwd
   will create `images/previews/` there.
5. `D:\_claude\CLAUDE.md` gained a Process Lesson the same day
   (commit `6974e59`): "'verified' means the check happened" —
   originating from the preview-bug episode above.
