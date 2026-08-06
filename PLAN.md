# td-blender-bridge — Integration Plan

Goal: **Blender EEVEE as a seamless realtime renderer for TouchDesigner** —
TD is the master clock and performance surface; Blender plays along with no
flicker, whether its timeline is paused or playing; frames return to TD.

## Status: what the prototype proved (v0.2.0, this repo)

Validated live on Blender 5.2 LTS + TD 2025.33070 (Windows, RTX 3090):

- 60 fps sustained: camera + 3600-point cloud + ~7k-tri deforming mesh,
  ~180 msg/s, zero packet loss, zero errors (UDP 9500 / TCP 9501)
- Coordinate + camera view-axis conversion correct (y-up → z-up; cameras need
  `M_b = C·M_td`, NOT the conjugation used for generic objects)
- TD `lookat` is baked into `worldTransform` — no extra handling needed
- SOP→CHOP `numpyArray()` is the only viable fast path (Python point loops
  are 100× slower); Grid-style SOPs emit `Mesh` prims that must go through a
  Convert SOP before topology extraction
- Record → bake to keyframes works (camera + lens + params at scene fps)
- EEVEE frames back over TCP into a Script TOP: designed, code in repo,
  needs live validation (Phase 3)

### Gotchas discovered (already handled or documented)

- A baked action **overrides** live streamed transforms during playback/render
  → live mode must mute conflicting actions (Phase 1)
- Scene `use_nodes=True` with an empty compositor group ⇒ blank F12 renders
- `bpy.app.timers` racing timeline playback causes **flicker** while playing
  → apply via frame handlers instead (Phase 1, the core fix)

## Phase 1 — Seamless playback (the flicker fix) — DONE (2026-08-06)

All four items implemented on `phase1-seamless-playback`; live-tested
end-to-end (TD 60 fps, full loop verified). Extras forced by live testing:
EEVEE offscreen capture moved into a View3D draw callback (timer-context
captures segfault in Blender 5.x), script re-runs tear down the previous
instance, and the TD sender backs off reconnects so a dead Blender no longer
stalls TD's frame loop (~2 fps -> 60). Remaining before merge: visual flicker
acceptance (paused/playing/scrubbing, RENDERED viewport).


The current timer applies data ~60×/s independent of Blender's own frame
loop. During playback, animation evaluation and the timer both write state →
visible fighting/flicker.

1. **Apply-on-frame architecture**
   - Keep the socket reader thread (latest-value store, unchanged).
   - Apply once per Blender frame via `bpy.app.handlers.frame_change_pre`
     (fires during playback/render) **plus** a low-rate timer that only runs
     when the timeline is *not* playing (`screen.is_animation_playing` check)
     so paused viewports still update live.
   - Result: exactly one apply per displayed frame, in sync with evaluation —
     no double-writes, no flicker.
2. **Live/bake arbitration**
   - "Live" toggle per bridge: while live, streamed channels temporarily mute
     their objects' actions (`animation_data.action = None`, restored on
     stop) so bakes never fight the stream.
   - Baking implies pausing live-apply for the baked channels.
3. **TD as master clock (timeline sync)**
   - Add `time`/`frame` field to every UDP message (TD `absTime`).
   - Optional "Slave timeline to TD" mode: Blender's `frame_current` follows
     TD's frame → frame-accurate recording and deterministic playback.
4. **Jitter handling**: optional 1-frame latest-value hold (already implicit)
   and exponential smoothing per channel (off by default).

Acceptance: TD playing at 60 fps, Blender viewport RENDERED — no flicker
with timeline paused, playing, or scrubbing; camera path identical in all
three states.

## Phase 2 — Geometry & scale — DONE (2026-08-06)

Implemented on `phase2-geometry` as protocol v2 (v1 still parses):

- ✅ Per-point attributes: velocity/scale/rotation → `td_velocity`,
  `td_scale`, `td_rot` (quaternion) point attributes
- ✅ Instance streaming: kind-3 frames → "TDB Instances" GN modifier
  (instance any object on N streamed transforms, sphere fallback)
- ✅ Normals/UVs on meshes (optional flags in the TDBG frame)
- ✅ Compression: zlib level 1 (stdlib on both sides — no zstd dependency)
  auto-applied over 512 KB; `tools/stress_test.py` for the 1M-point test
- ✅ Auto Geometry Nodes setup for points (spheres + td_color material)
- Deferred: chunked/partial mesh updates — revisit if a real bottleneck
  shows; TCP framing + compression cover current sizes

## Phase 3 — Frames back to TD, properly ← NEXT

- ~~Validate the TCP RGBA path live~~ done 2026-08-06 (~24-27 fps at
  960x540/30; measured: the double EEVEE render + readback also drags the
  interactive viewport - users disable the frame server to get 60 fps back)
- Stop rendering twice: grab the viewport's already-rendered framebuffer
  (overlays off = clean EEVEE) instead of a second offscreen render
- Spout backend on Windows (zero-copy GPU, same-GPU only) with TCP as
  portable/cross-machine fallback; requires the OpenGL backend in Blender
- Latency budget & measurement (target < 3 frames end-to-end)
- ~~TOP -> Blender textures~~ implemented 2026-08-06 (kind-4 frames,
  `TOP_OPS` / component Textop par -> Blender image datablock; pending
  live validation)
- ~~POP support~~ resolved 2026-08-06: POP attribute `.vals()` is Python
  lists (no bulk numpy on POPs), so `poptoCHOP` + `numpyArray()` IS the
  fast path (measured sub-ms). Sender now accepts POP-style channel
  names (`P_0/P_1/P_2`, `Color_0..3`, `N_0..`, `v_0..`, `pscale`,
  `UV_0/1`) alongside SOP-to-CHOP names - wire POP -> poptoCHOP ->
  Points/Instances par, no renaming
- Optional depth/AOV passes for TD-side compositing

## Phase 4 — Packaging & DX

- Blender: extension-format packaging (blender_manifest.toml), prefs UI,
  auto-start option
- ~~TD: single .tox component~~ done early (2026-08-06):
  `touchdesigner/TDBridge.tox` - Bridge parameter page (host/ports/sources/
  Active/Receive Frames toggles), param_map table for CHOP-channel ->
  Blender-datapath mapping, out1 TOP carrying returned frames
- Example .toe + .blend pair; CI (ruff + headless Blender smoke test)

## Workflow from here

1. Push this repo to GitHub (see README; `git init` history is in the
   delivered bundle)
2. Build Phase 1 with Claude Code against the repo, one PR per phase item
3. Live-test each PR with the real TD+Blender pair before merge
