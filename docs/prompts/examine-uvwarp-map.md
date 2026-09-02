# Prompt — LR_UVWarp session: examine the UV/ST map EXR with exr_read

Paste this into a pi (or claude.ai, adapted) session whose cwd is
`D:\_claude\aftereffects\LR_UVWarp`. The `exr_read` tool must be
available (project `.pi/settings.json` wires it in — see
`D:\_claude\python\pi\exr_loader\AGENTS.md`).

---

We are debugging the LR_UVWarp open issue: **the warp mechanism works
but is not mapping correctly with the textures** (see
`docs/handoffs/2026-08-24_warp-pipeline-built-verified-mapping-open.md`,
top item).

I have an EXR file to examine: `<PASTE EXR PATH HERE>`
(it is `<a UV/ST map render / a textured render / ...>`).

Use the `exr_read` tool on it. Work through, in order:

1. **Load it** (`exr_read` on the path, no pixel/rect yet). Report:
   size, data window, channel names, storage, compression, and
   per-channel min/max/mean. Tell me which channel(s) look like a UV/ST
   map (typically values in 0..1, or a channel literally named `U`/`V`
   or `st`), and which look like colour.

2. **Sample a grid.** Query `pixel` at the four corners (1,1),
   (W-2,1), (1,H-2), (W-2,H-2), the centre, and the midpoints of each
   edge (use the reported size). Report the UV/ST values at each in a
   table. From that, tell me: is the map a full-frame ramp? Which axis
   increases left→right, which top→bottom? Any V-flip vs what I'd
   expect (AE image space: y down)?

3. **Cross-check one point against the warp math.** The plugin's warp
   converts a Map UV to Source pixel-centre coordinates with
   `x_src = U * srcW - 0.5`, `y_src = V * srcH - 0.5` (after
   Flip/Tile/Offset/Rotate/Wrap in UV space). Pick one sampled point and
   show me the expected Source coordinate for a Source of
   `<srcW> x <srcH>` — I'll compare it to what AE actually shows.

4. **Preview check.** With the preview PNG from step 1 (path is in the
   tool result), confirm what you see matches the sampled values
   (e.g. a ramp should look like a ramp; if the preview shows a
   structure the values don't, say so — that would indicate a channel
   order or orientation bug in the tool).

Constraints:
- Coordinates are (x, y), origin TOP-LEFT, y down — the preview PNG is
  oriented the same way, so a point you see at (px, py) in the preview
  is `pixel [px, py]` scaled by preview.size / part size.
- Don't sample values off the preview PNG (it's a linear remap, not
  true values) — use `pixel`/`rect`.
- If the file has multiple parts, cover each part's channels in step 1.
