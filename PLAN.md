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

## Phase 2 — Geometry & scale

- Per-point attributes beyond color (velocity → motion blur vectors, scale,
  rotation for instancing)
- Instance streaming: N transforms → GN instances (cheaper than meshes)
- Normals/UVs on meshes (optional channels in the TDBG frame)
- Chunked/partial mesh updates; >1M-point stress test; zstd option
- Auto Geometry Nodes setup on first arrival of a points object

## Phase 3 — Frames back to TD, properly

- Validate the TCP RGBA path live (code shipped in v0.2.0)
- Latency budget & measurement (target < 3 frames end-to-end)
- Spout backend on Windows (zero-copy GPU) with TCP as portable fallback
- Optional depth/AOV passes for TD-side compositing

## Phase 4 — Packaging & DX

- Blender: extension-format packaging (blender_manifest.toml), prefs UI,
  auto-start option
- TD: single .tox component (custom parameters for ports/mappings instead of
  editing CONFIG in a DAT)
- Example .toe + .blend pair; CI (ruff + headless Blender smoke test)

## Workflow from here

1. Push this repo to GitHub (see README; `git init` history is in the
   delivered bundle)
2. Build Phase 1 with Claude Code against the repo, one PR per phase item
3. Live-test each PR with the real TD+Blender pair before merge
