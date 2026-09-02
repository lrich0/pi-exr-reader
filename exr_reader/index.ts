/**
 * exr_read — EXR inspection tool for pi.
 *
 * Loads .exr files (Arnold/RenderMan/Blender/USD renders, UV/ST maps,
 * AOVs, multilayer EXRs) and reports them to the LLM as:
 *   - part metadata (size, data window, storage, compression, line order)
 *   - per-channel statistics (min/max/mean/nonzero fraction, per component)
 *   - exact float pixel values at a point
 *   - per-channel statistics over a rect
 *   - an 8-bit linear-mapped PNG preview (attached to the tool result,
 *     so a vision model can see the structure)
 *
 * The heavy lifting is done by exr_reader/exr_probe.py (OpenEXR 3.x
 * Python API + numpy + Pillow). This extension only: locates the script,
 * builds the command, runs it, and packages JSON + preview image.
 *
 * Coordinate convention: (x, y) with origin at the TOP-LEFT of the part's
 * data window, x right, y DOWN — matching how the preview PNG is oriented.
 *
 * Project-local install: <project>/.pi/extensions/exr_read/index.ts
 *   (add { "extensions": ["../../<abs path>/exr_reader/index.ts"] } to
 *    <project>/.pi/settings.json if you keep the code elsewhere)
 * Global install: copy to ~/.pi/agent/extensions/exr_read/index.ts
 */

import { withFileMutationQueue } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { existsSync, readFileSync, statSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import { execFile, spawnSync } from "node:child_process";

const SCRIPT_DIR = __dirname;
const SCRIPT = join(SCRIPT_DIR, "exr_probe.py");
const MAX_PREVIEW_PX = 2048; // 1080² originals pass through unresized; cap guards huge renders
const MAX_TEXT_CHARS = 12000;

// Mirrors preview_default_name() in exr_probe.py (kept in sync on purpose):
// <stem>[_<channel-tag>]_preview.png — the channel tag keeps previews of
// different channel selections of the same file from overwriting each
// other. The extension always passes --preview-out explicitly (user value
// or this default) so it knows the exact path for the mutation queue.
function previewDefaultName(exrPath: string, channels: string[] | undefined): string {
  let stem = basename(exrPath).replace(/\.[^.]*$/, "");
  if (channels?.length) {
    const tag = channels
      .map((s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, ""))
      .join("_");
    if (tag) stem += "_" + tag;
  }
  return stem + "_preview.png";
}

function findPython(): string | null {
  const candidates = process.platform === "win32" ? ["python", "python3"] : ["python3", "python"];
  for (const c of candidates) {
    try {
      const r = spawnSync(c, ["-c", "import OpenEXR, numpy; print(OpenEXR.__version__)"], {
        timeout: 15000,
        encoding: "utf8",
        windowsHide: true,
      });
      if (r.status === 0 && (r.stdout ?? "").trim()) return c;
    } catch {
      /* try next */
    }
  }
  return null;
}

const exrReadSchema = Type.Object({
  path: Type.String({ description: "Path to the .exr file (absolute or relative to the session cwd)." }),
  pixel: Type.Optional(
    Type.Tuple([Type.Number(), Type.Number()], {
      description:
        "Optional: query exact values at pixel (x, y). Origin is TOP-LEFT of the part's data window, x right, y DOWN (preview PNG orientation).",
    })
  ),
  rect: Type.Optional(
    Type.Tuple([Type.Number(), Type.Number(), Type.Number(), Type.Number()], {
      description: "Optional: per-channel stats over rect (x0, y0, x1, y1), inclusive corners.",
    })
  ),
  channel: Type.Optional(
    Type.Array(Type.String(), {
      description:
        'Optional: restrict values/stats/preview to these channels (case-insensitive). A group name selects all its components ("uv" matches "uv.R","uv.G","uv.B"; "UV_Box" matches "UV_Box.R/.G/.B"); an exact component name also works. Omitted = all channels for stats, RGB-like composition for preview. A filter that matches nothing is an error listing the available channel names.',
    })
  ),
  preview: Type.Optional(
    Type.Boolean({ description: "Optional: set false to skip the PNG preview (default true)." })
  ),
  previewToneMap: Type.Optional(
    Type.Union([Type.Literal("auto"), Type.Literal("linear"), Type.Literal("tonemap")], {
      description:
        "Optional: preview mapping per component (default 'auto'). 'linear' = min->max full-range remap; 'tonemap' = the viewer look (1.0 = white, sRGB gamma, clip above — what Nuke/Photoshop/AE show for an EXR); 'auto' = linear for map/LDR-like data (~[0,1]) and for data with large negatives (P/Z), tonemap for image-like HDR.",
    })
  ),
  previewOut: Type.Optional(
    Type.String({
      description:
        "Optional: where to save the preview PNG. Default: <cwd>/images/previews/<filename>[_<channels>]_preview.png (project-local, stable names, re-runs refresh the same file).",
    })
  ),
});

export type ExrReadInput = {
  path: string;
  pixel?: [number, number];
  rect?: [number, number, number, number];
  channel?: string[];
  preview?: boolean;
  previewToneMap?: "auto" | "linear" | "tonemap";
  previewOut?: string;
};

function previewNote(hasVision: boolean, mapping: Record<string, string> | undefined): string {
  const coord =
    "Coordinates: (x, y) with origin at the TOP-LEFT of the part's data window, x increasing right, y increasing DOWN — the preview PNG is oriented the same way, so a point you see at (px, py) in the preview is queryable as pixel [px, py] (scale by preview.size / part size first if the preview was downscaled).";
  const map = mapping && Object.values(mapping).length
    ? ` Mapping per component (see 'mapping' in JSON): ${JSON.stringify(mapping)} — 'tonemap' components are Reinhard+sRGB, for viewing only.`
    : "";
  return hasVision
    ? `Preview PNG: an 8-bit view of the image structure (LDR/map components are a linear min->max remap, HDR components are tone-mapped${map}) — structure is shown, absolute values are NOT; use pixel/rect for values. ${coord}`
    : `Preview: the current model has no image input, so no preview was attached — the JSON below is the full data (the preview PNG is still written for you). ${coord}`;
}

export default function (pi: any) {
  let pythonCache: string | null | undefined;

  pi.registerTool({
    name: "exr_read",
    label: "EXR Read",
    description:
      "Read an .exr image file (render passes, UV/ST maps, Arnold AOVs such as UV/P/Z/TextureGrid, multilayer EXR). Returns part metadata + per-channel float stats, exact pixel values at a point, per-channel stats over a rect, and a linear-mapped PNG preview. Use it whenever the user mentions or references an .exr file. AOVs: pass the group name (e.g. \"UV_Box\") as channel; pixel queries report every part of a multilayer file.",
    promptSnippet: "exr_read(path, pixel?, rect?, channel?, preview?) — inspect .exr files: metadata, per-channel stats, exact pixel values, rect stats, PNG preview",
    promptGuidelines: [
      "Use exr_read (not read/bash) for .exr files; it reports full-precision channel values.",
      "exr_read coordinates: origin top-left, y down — same orientation as its preview PNG.",
      "exr_read channel filter: a group name (e.g. 'uv_box') selects its components; a non-matching filter is an error that lists the available names.",
    ],
    parameters: exrReadSchema,
    async execute(
      _toolCallId: string,
      params: ExrReadInput,
      signal: AbortSignal | undefined,
      _onUpdate: unknown,
      ctx: any
    ) {
      if (pythonCache === undefined) pythonCache = findPython();
      const python = pythonCache;
      if (!python) {
        return {
          content: [
            {
              type: "text" as const,
              text: "exr_read unavailable: no python with OpenEXR + numpy found on PATH. Install with:  pip install OpenEXR numpy Pillow",
            },
          ],
          isError: true,
        };
      }

      const script = SCRIPT;
      if (!existsSync(script)) {
        return {
          content: [{ type: "text" as const, text: `exr_read: script missing: ${script}` }],
          isError: true,
        };
      }

      const absPath = resolve(ctx.cwd, params.path);
      if (!existsSync(absPath)) {
        return {
          content: [{ type: "text" as const, text: `exr_read: file not found: ${absPath}` }],
          isError: true,
        };
      }
      const args: string[] = [script, absPath];
      if (params.pixel) args.push("--pixel", String(params.pixel[0]), String(params.pixel[1]));
      if (params.rect)
        args.push(
          "--rect",
          String(params.rect[0]),
          String(params.rect[1]),
          String(params.rect[2]),
          String(params.rect[3])
        );
      if (params.channel) for (const c of params.channel) args.push("--channel", c);
      const wantPreview = params.preview !== false;
      let previewPath: string | undefined;
      if (!wantPreview) {
        args.push("--no-preview");
      } else {
        previewPath = params.previewOut
          ? resolve(ctx.cwd, params.previewOut)
          : join(ctx.cwd, "images", "previews", previewDefaultName(absPath, params.channel));
        args.push(
          "--preview-out", previewPath,
          "--max-preview", String(MAX_PREVIEW_PX),
          "--tone-map", params.previewToneMap ?? "auto"
        );
      }

      // The probe writes previewPath. Queue it per the extension docs so
      // parallel exr_read calls on the same file (same default preview
      // path) can't interleave their writes.
      const run = () => new Promise<{ stdout: string; stderr: string; code: number; spawnErr?: string }>((resolveP) => {
        execFile(
          python,
          args,
          {
            cwd: ctx.cwd,
            timeout: 300000,
            maxBuffer: 16 * 1024 * 1024,
            windowsHide: true,
            signal,
          },
          (error, stdout, stderr) => {
            let code = 0;
            let spawnErr: string | undefined;
            if (error) {
              if (typeof (error as any).code === "number") code = (error as any).code;
              else spawnErr = (error as any).code ?? String(error);
            }
            resolveP({ stdout: String(stdout ?? ""), stderr: String(stderr ?? ""), code, spawnErr });
          }
        );
      });

      const res = previewPath ? await withFileMutationQueue(previewPath, run) : await run();

      if (res.spawnErr) {
        return {
          content: [{ type: "text" as const, text: `exr_read: python spawn failed (${res.spawnErr})` }],
          isError: true,
        };
      }
      if (res.code !== 0 && !res.stdout.trim()) {
        return {
          content: [{ type: "text" as const, text: `exr_read failed (exit ${res.code}):\n${res.stderr}` }],
          isError: true,
        };
      }

      let json: any = null;
      try {
        json = JSON.parse(res.stdout.trim().split("\n").pop() ?? "");
      } catch (e: any) {
        return {
          content: [{ type: "text" as const, text: `exr_read: could not parse probe output: ${e?.message}\n${res.stdout.slice(0, 2000)}` }],
          isError: true,
        };
      }

      if (!json.ok) {
        return {
          content: [{ type: "text" as const, text: `exr_read: ${json.error}${json.part0_dataWindow ? ` (part 0 data window ${JSON.stringify(json.part0_dataWindow)}, size ${JSON.stringify(json.part0_size)})` : ""}` }],
          isError: true,
        };
      }

      const model: any = ctx.model;
      const hasVision = Array.isArray(model?.input) && model.input.includes("image");

      const content: Array<{ type: "text" | "image"; text?: string; data?: string; mimeType?: string }> = [];
      let text = JSON.stringify(json, null, 1);
      if (text.length > MAX_TEXT_CHARS) {
        text =
          text.slice(0, MAX_TEXT_CHARS) +
          `\n…[truncated ${text.length - MAX_TEXT_CHARS} chars — re-query with channel/pixel/rect to narrow]`;
      }
      if (hasVision && json.preview?.path && existsSync(json.preview.path) && statSync(json.preview.path).size > 0) {
        content.push({
          type: "image",
          data: readFileSync(json.preview.path).toString("base64"),
          mimeType: "image/png",
        });
      }
      content.push({ type: "text", text: text + "\n\n" + previewNote(hasVision, json.preview?.mapping) });

      return { content, details: { previewPath: json.preview?.path } };
    },
  });
}
