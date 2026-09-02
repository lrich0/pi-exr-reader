#!/usr/bin/env python3
"""exr_probe.py — EXR file reader for the pi `exr_read` tool.

Reads an EXR file (any compression, scanline or tiled, multi-part,
half/float/int channels) via the OpenEXR 3.x Python API and prints a
single JSON object to stdout. Exit code 0 on success (including
"ok": false results), non-zero on hard failure.

Coordinate convention (matches the preview PNG orientation):
  (x, y) = pixel coordinates with origin at the TOP-LEFT of the part's
  data window, x increasing to the right, y increasing DOWN.
  Array index is [y, x]. Preview PNG top row = array row 0 = y=0.

Usage:
  python exr_probe.py FILE.exr [--pixel X Y] [--rect X0 Y0 X1 Y1]
                               [--channel NAME] [--preview-out PATH]
                               [--max-preview N] [--tone-map MODE] [--no-preview]

--tone-map MODE selects the preview mapping per component: "auto" (default),
"linear", or "tonemap".
  linear  = full [min,max] -> full range (any data; UV/ST maps, P/Z AOVs)
  tonemap = viewer look: 1.0 = white, sRGB gamma, clip above (what Nuke/
            Photoshop/AE show for an EXR)
  auto    = linear when the data fits ~[0, 1.05] (maps/LDR), tonemap for
            image-like HDR (min ~>= 0, max > 1.05), linear full-range for
            data with a large negative span (P/Z-like). The per-component
            mapping used is reported in the preview JSON.

--channel NAME selects which channels get pixel values / rect stats /
the preview. Matches a channel name case-insensitively (e.g. "R", "U",
"st"); a GROUP name also selects all its components ("uv" matches
"uv.R", "uv.G", "uv.B"), and an exact component name ("uv.r") still
works. May be given multiple times. A filter that matches nothing is
an error that lists the available channel names. Omitted: stats cover
every channel; pixel queries report every channel; preview composes
the first R,G,B channels (or the first up-to-3 channels if no R/G/B
names exist).
"""

import argparse
import json
import math
import os
import re
import sys
import tempfile

try:
    import numpy as np
    import OpenEXR
except ImportError as _e:  # surfaced via fail() in main()
    np = None  # type: ignore[assignment]
    OpenEXR = None  # type: ignore[assignment]
    _IMPORT_ERROR = _e
else:
    _IMPORT_ERROR = None


def fail(msg, **extra):
    out = {"ok": False, "error": msg}
    out.update(extra)
    print(json.dumps(sanitize(out)))
    sys.exit(0)


def sanitize(o):
    """Replace non-finite floats with strings so the output is strict JSON."""
    if isinstance(o, float):
        if math.isnan(o):
            return "NaN"
        if math.isinf(o):
            return "inf" if o > 0 else "-inf"
        return o
    if isinstance(o, dict):
        return {k: sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [sanitize(v) for v in o]
    return o


def call_or_val(v):
    """The OpenEXR 3.x Part API mixes methods (name/width/height/type/
    compression) and dict properties (channels/header) — normalise both."""
    return v() if callable(v) else v


def preview_default_name(path, selected_args):
    """Stable preview filename: <stem>[_<channel-tag>].png, always .png
    (the probe only ever writes PNG). The channel tag keeps previews of
    different channel selections of the same file from overwriting each
    other (e.g. Box-on-alpha_uv_aovs_UV_Box_preview.png)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    if selected_args:
        tag = "_".join(
            re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") for s in selected_args
        )
        if tag:
            stem += "_" + tag
    return stem + "_preview.png"


def comp_name(ch_name, arr, i):
    """Component label: bare channel name for 2-D, 'name.0'/'name.1' for n-D."""
    if arr.ndim == 3:
        return f"{ch_name}.{i}"
    return ch_name


def _auto_mode(lo, hi):
    """auto decision for one component:
    - ~[0, 1.05] data -> linear min->max (maps/LDR: honest full-range view)
    - image-like HDR (min ~>= 0, max > 1.05) -> tonemap (the viewer look)
    - large negative span (P/Z-like) -> linear full-range (show everything)
    """
    if hi <= 1.05 and lo >= -0.01:
        return "linear"
    if lo >= -0.01:
        return "tonemap"
    return "linear"


def _srgb_gamma(y):
    y = np.clip(y, 0.0, 1.0)
    return np.where(y <= 0.0031308, 12.92 * y, 1.055 * np.power(y, 1.0 / 2.4) - 0.055)


def map_component(a, lo, hi, mode):
    """Map one component (finite min lo, max hi) to [0, 65535] as float32.
    linear:  full [lo, hi] -> [0, 65535] (any data range)
    tonemap: viewer look — 1.0 = white, sRGB gamma, clip above 1 / below 0.
             This is what EXR viewers (Nuke/Photoshop/AE) show, so a vision
             model sees a display-referred image it was trained on."""
    if mode == "linear":
        a = a - lo
        return a / (hi - lo) * 65535.0
    return _srgb_gamma(a) * 65535.0


def channel_matches(cname, selected):
    """Case-insensitive match of a channel name against the --channel set.
    An exact name match works ("uv", "uv.r"); a GROUP name also selects
    all of a multi-component channel's components ("uv" matches
    "uv.R", "uv.G", "uv.B"). The dot-anchored prefix prevents "uv" from
    matching an unrelated channel named "uvbox"."""
    cl = cname.lower()
    if cl in selected:
        return True
    return any(cl.startswith(s + ".") for s in selected)


def stats_of(a):
    a = a.astype("float32")
    finite = a[np.isfinite(a)]
    if finite.size == 0:
        mn = mx = None
        mean = "NaN"
    else:
        mn, mx = float(finite.min()), float(finite.max())
        mean = float(a.mean()) if a.size else "NaN"
    nz = float(np.count_nonzero(a)) / a.size if a.size else 0.0
    return {"min": mn, "max": mx, "mean": mean, "nonzero_frac": round(nz, 6)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--pixel", nargs=2, type=int, metavar=("X", "Y"))
    ap.add_argument("--rect", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"))
    ap.add_argument(
        "--channel",
        action="append",
        default=None,
        metavar="NAME",
        help="channel name (case-insensitive); a group name like 'uv' also "
        "selects its components ('uv.R','uv.G','uv.B'); may be repeated",
    )
    ap.add_argument("--preview-out", default=None)
    ap.add_argument("--max-preview", type=int, default=1024)
    ap.add_argument(
        "--tone-map",
        choices=["auto", "linear", "tonemap"],
        default="auto",
        help="preview mapping per component (default auto: linear for ~[0,1] data, "
        "Reinhard+sRGB for HDR/negative data)",
    )
    ap.add_argument("--no-preview", action="store_true")
    args = ap.parse_args()

    path = os.path.abspath(args.path)
    if not os.path.isfile(path):
        fail(f"file not found: {path}")

    if OpenEXR is None:
        fail(f"missing python dependency: {_IMPORT_ERROR!r}. Install with: pip install OpenEXR")

    try:
        exr = OpenEXR.File(path, separate_channels=True)
    except Exception as e:
        fail(f"cannot open EXR: {e!r}")

    selected = {c.lower() for c in args.channel} if args.channel else None

    # ---------- parts + per-channel stats ----------
    parts_out = []
    all_channel_names = set()
    for pi, part in enumerate(exr.parts):
        chdict = call_or_val(part.channels)
        all_channel_names.update(chdict.keys())
        if not chdict:
            fail(f"part {pi} has no channels", parts=parts_out)
        dw = call_or_val(part.header).get("dataWindow")
        dw_out = None
        if dw is not None:
            try:
                lo, hi = dw
                dw_out = [[int(lo[0]), int(lo[1])], [int(hi[0]), int(hi[1])]]
            except Exception:
                dw_out = str(dw)
        channels_out = {}
        for cname, ch in chdict.items():
            arr = np.array(ch.pixels)
            if selected is not None and not channel_matches(cname, selected):
                continue
            comps = {}
            n = arr.shape[2] if arr.ndim == 3 else 1
            for i in range(n):
                a = arr[..., i] if arr.ndim == 3 else arr
                comps[comp_name(cname, arr, i)] = {
                    "shape": list(a.shape),
                    **stats_of(a),
                }
            channels_out[cname] = comps
        parts_out.append({
            "index": pi,
            "name": str(call_or_val(part.name)),
            "width": int(call_or_val(part.width)),
            "height": int(call_or_val(part.height)),
            "dataWindow": dw_out,
            "storage": str(call_or_val(part.type)),
            "lineOrder": str(call_or_val(part.header).get("lineOrder")),
            "compression": str(call_or_val(part.compression)),
            "channels": channels_out,
        })

    result = {
        "ok": True,
        "path": path,
        "bytes": os.path.getsize(path),
        "numParts": len(parts_out),
        "coordinateConvention": "origin top-left of data window, x right, y down; array index [y, x]",
        "parts": parts_out,
    }

    # ---------- channel filter: honest no-match error ----------
    # Runs BEFORE pixel/rect/preview: without it, a filter that matches
    # nothing (a typo, or an old call that assumed exact-match semantics)
    # produced empty results and, worse, a misleading "outside data
    # window" error from the pixel path — verified on a real Arnold AOV
    # file 2026-08-25.
    if selected is not None:
        if not any(channel_matches(c, selected) for c in all_channel_names):
            avail = sorted(all_channel_names)
            fail(
                f"no channel matches {sorted(selected)!r} — available channel names: {avail!r} "
                "(a group name like 'uv' selects its components 'uv.R'/'uv.G'/'uv.B'; "
                "an exact component name also works)",
                available=avail,
            )

    # ---------- pixel query ----------
    if args.pixel:
        x, y = args.pixel
        pixel_out = {"x": x, "y": y, "parts": {}}
        found = False
        for p in parts_out:
            if x < 0 or x >= p["width"] or y < 0 or y >= p["height"]:
                continue
            part = exr.parts[p["index"]]
            chdict = call_or_val(part.channels)
            vals = {}
            for cname, ch in chdict.items():
                if selected is not None and not channel_matches(cname, selected):
                    continue
                arr = np.array(ch.pixels)
                if arr.ndim == 3:
                    for i in range(arr.shape[2]):
                        vals[comp_name(cname, arr, i)] = float(arr[y, x, i])
                else:
                    vals[cname] = float(arr[y, x])
            if vals:
                pixel_out["parts"][str(p["index"])] = vals
                found = True
        if not found:
            dw = parts_out[0]["dataWindow"]
            fail(
                f"pixel ({x},{y}) outside data window(s) of all parts",
                part0_dataWindow=dw,
                part0_size=[parts_out[0]["width"], parts_out[0]["height"]],
            )
        result["pixel"] = pixel_out

    # ---------- rect stats ----------
    if args.rect:
        x0, y0, x1, y1 = args.rect
        if x0 > x1 or y0 > y1:
            fail("invalid rect: x0>x1 or y0>y1")
        p0 = parts_out[0]
        part = exr.parts[0]
        chdict = call_or_val(part.channels)
        rect_out = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "size": [x1 - x0 + 1, y1 - y0 + 1], "channels": {}}
        if x0 < 0 or y0 < 0 or x1 >= p0["width"] or y1 >= p0["height"]:
            fail(
                f"rect ({x0},{y0})-({x1},{y1}) outside part 0 data window "
                f"(0,0)-({p0['width']-1},{p0['height']-1})"
            )
        for cname, ch in chdict.items():
            if selected is not None and not channel_matches(cname, selected):
                continue
            arr = np.array(ch.pixels)[y0 : y1 + 1, x0 : x1 + 1]
            n = arr.shape[2] if arr.ndim == 3 else 1
            rect_out["channels"][cname] = {}
            for i in range(n):
                a = arr[..., i] if arr.ndim == 3 else arr
                rect_out["channels"][cname][comp_name(cname, arr, i)] = stats_of(a)
        result["rect"] = rect_out

    # ---------- preview ----------
    if not args.no_preview:
        try:
            from PIL import Image
        except ImportError:
            result["preview"] = {
                "skipped": "PIL not installed (pip install Pillow); no preview produced"
            }
        else:
            p0 = parts_out[0]
            part = exr.parts[0]
            chdict = call_or_val(part.channels)
            if selected is not None:
                names = [c for c in chdict if channel_matches(c, selected)]
            else:
                # Named R/G/B channels first; file order is NOT a safe
                # default (this Arnold file lists A, B, G, R), otherwise
                # fall back to the first up-to-3 channels.
                rgb = [c for c in chdict if c.lower() in ("r", "g", "b")]
                names = rgb if rgb else list(chdict.keys())[:3]
            # R,G,B-aware slot ordering for whatever was picked: a channel
            # whose name (or .suffix) is r/g/b goes to the matching color
            # slot in R,G,B order regardless of file channel order — file
            # order put B in the red slot for a group-selected UV AOV, the
            # same swap as the 2026-08-25 preview bug. Non-RGB channels
            # keep file order (stable). This is what makes a UV AOV preview
            # read U->red, V->green.
            _order = {c: i for i, c in enumerate(chdict.keys())}

            def _slot_key(c):
                lc = c.lower()
                for i, s in enumerate(("r", "g", "b")):
                    if lc == s or lc.endswith("." + s):
                        return (0, i, 0)
                return (1, 0, _order[c])

            names.sort(key=_slot_key)
            if not names:
                result["preview"] = {"skipped": "no channels available for preview"}
            else:
                out_path = (
                    os.path.abspath(args.preview_out)
                    if args.preview_out
                    else os.path.join(os.getcwd(), "images", "previews", preview_default_name(path, args.channel))
                )
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                arrs = {c: np.array(chdict[c].pixels).astype("float32") for c in names}
                w, h = arrs[names[0]].shape[1], arrs[names[0]].shape[0]
                scales = {}
                modes = {}

                def _mode_for(lo, hi):
                    return args.tone_map if args.tone_map != "auto" else _auto_mode(lo, hi)

                if names[0] in arrs and arrs[names[0]].ndim == 3:
                    # one multi-component channel: comp i -> channel i of RGB
                    comp_arrays, labels = [], []
                    for i in range(min(3, arrs[names[0]].shape[2])):
                        a = arrs[names[0]][..., i]
                        f = a[np.isfinite(a)]
                        lo, hi = (float(f.min()), float(f.max())) if f.size else (0.0, 1.0)
                        if hi <= lo:
                            hi = lo + 1.0
                        m = _mode_for(lo, hi)
                        scales[f"{names[0]}.{i}"] = [lo, hi]
                        modes[f"{names[0]}.{i}"] = m
                        comp_arrays.append(map_component(a, lo, hi, m))
                        labels.append(f"{names[0]}.{i}")
                    rgb = np.stack(
                        [np.clip(comp_arrays[0], 0, 65535)]
                        + ([np.clip(comp_arrays[1], 0, 65535)] if len(comp_arrays) > 1 else [np.zeros_like(comp_arrays[0])])
                        + ([np.clip(comp_arrays[2], 0, 65535)] if len(comp_arrays) > 2 else [np.zeros_like(comp_arrays[0])]),
                        axis=-1,
                    )  # keep float in [0, 65535]; 8-bit conversion happens below
                else:
                    comp_arrays, labels = [], []
                    for c in names[:3]:
                        a = arrs[c] if arrs[c].ndim == 2 else arrs[c][..., 0]
                        f = a[np.isfinite(a)]
                        lo, hi = (float(f.min()), float(f.max())) if f.size else (0.0, 1.0)
                        if hi <= lo:
                            hi = lo + 1.0
                        m = _mode_for(lo, hi)
                        scales[c] = [lo, hi]
                        modes[c] = m
                        comp_arrays.append(map_component(a, lo, hi, m))
                        labels.append(c)
                    rgb = np.stack(
                        comp_arrays[0:1]
                        + (comp_arrays[1:2] if len(comp_arrays) > 1 else [np.zeros_like(comp_arrays[0])])
                        + (comp_arrays[2:3] if len(comp_arrays) > 2 else [np.zeros_like(comp_arrays[0])]),
                        axis=-1,
                    )  # keep float in [0, 65535]; 8-bit conversion happens below
                # Convert to 8-bit HERE, explicitly. Pillow cannot encode 16-bit
                # RGB PNGs, and Image.fromarray(uint16_array, "RGB") does NOT
                # downscale — it reinterprets the raw 2-byte values as 8-bit
                # byte pairs (verified 2026-08-25: produced a scrambled image).
                rgb = (rgb / 257.0).round().clip(0, 255).astype("uint8")
                im = Image.fromarray(rgb, mode="RGB")
                max_edge = max(args.max_preview, 2)
                if max(h, w) > max_edge:
                    scale = max_edge / float(max(h, w))
                    im = im.resize((max(2, int(w * scale)), max(2, int(h * scale))), Image.LANCZOS)
                im.save(out_path, "PNG")
                result["preview"] = {
                    "path": out_path,
                    "size": [im.width, im.height],
                    "bitDepth": "8-bit RGB",
                    "slots": {s: labels[i] for i, s in enumerate(("R", "G", "B")) if i < len(labels)},
                    "mapping": {k: modes.get(k, "linear") for k in scales},
                    "componentScales": {
                        k: {"from": v, "to": [0, 255]} for k, v in scales.items()
                    },
                    "note": (
                        "per-component mapping: 'linear' = full [min,max] -> full range (LDR/maps); "
                        "'tonemap' = shift+Reinhard+sRGB gamma (HDR/negative data, for viewing). "
                        "Structure shown, absolute values NOT — use --pixel/--rect for values. "
                        f"(requested --tone-map: {args.tone_map})"
                    ),
                }

    print(json.dumps(sanitize(result)))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        fail(f"unexpected error: {e!r}")
