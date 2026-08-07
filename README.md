# td-blender-bridge

**Use Blender EEVEE as a realtime render engine for TouchDesigner** — the way
you'd use Unreal, but with Blender.

TouchDesigner streams cameras, transforms, CHOP parameters, point clouds,
instance transforms, deforming meshes and textures into a live Blender scene;
EEVEE renders it; frames stream back into TD over **Spout** (zero TCP,
GPU-shared) or TCP. Plain Python on both ends — no plugins to compile.

```
┌──────────────────┐  UDP 9500  xforms/cams/params + TD clock   ┌─────────────────┐
│   TouchDesigner  │ ─────────────────────────────────────────► │     Blender     │
│                  │  TCP 9501  points/instances/meshes/        │   (EEVEE live)  │
│  TDBridge.tox    │            textures (binary, v2)           │                 │
│  (component)     │ ─────────────────────────────────────────► │  td_blender_    │
│                  │  Spout "TDBridge" (+ TCP 9502 fallback)    │  bridge.py      │
│  out1 = frames   │ ◄───────────────────────────────────────── │  (add-on)       │
│  out2 = depth    │                                            │                 │
└──────────────────┘                                            └─────────────────┘
```

Measured on Windows / RTX 4090 / Blender 5.2 / TD 2025.3x: TD holds 60 fps
with the full loop running; end-to-end latency (TD transform → EEVEE render →
frame back in TD) **17–39 ms** at 3150×1898 over TCP — Spout at or below that.

## Features

**TD → Blender**
- **Camera sync** — Camera COMP world transform, FOV, near/far → Blender
  camera (y-up → z-up and view-axis conversion handled; `lookat` supported)
- **Object transforms** — any Blender object driven by a COMP's world matrix
- **Lights** — TD Light COMPs stream as Blender lights: type (point/cone/
  distant → point/spot/sun), color, dimmer, cone angle/blend, all live
- **Parameters** — CHOP channels → arbitrary Blender datapaths
  (`data.energy`, `scale`, material node values, …) via a mapping table;
  live values are shown in the Blender panel
- **Point clouds** — positions + per-point color/velocity/scale/rotation
  attributes; auto Geometry Nodes setup renders them as shaded spheres
  (skipped above 250k points)
- **Instances** — N transforms → GN instances of any object you pick
- **Meshes** — triangulated SOPs with topology caching, optional normals/UVs;
  deform-only updates take a fast `foreach_set` path
- **POPs** — wire any POP chain through a `POP to CHOP`; the sender
  understands POP channel names (`P_0/P_1/P_2`, `Color_0..3`, `N_0..`,
  `pscale`, …) natively
- **Textures** — any TOP → a Blender image datablock (use it in any
  Image Texture node)
- **TD master clock** — every message carries TD's timeline frame + time;
  optional **Slave timeline to TD** locks Blender's playhead to TD
  (frame-accurate recording, deterministic playback)
- **Record & bake** — capture a live performance, bake camera/params to
  keyframes on the TD clock, render offline in EEVEE or Cycles

**Blender → TD**
- **Spout transport** (default) — frames shared as a GPU texture into the
  component's Spout In TOP; no TCP, no TD-side upload. TCP remains as the
  portable/cross-machine fallback (carries a TD timestamp for latency
  measurement)
- **Two capture modes** — *Viewport (fast)*: the already-rendered viewport
  image, no second render, one frame latency; with the viewport in camera
  view (Numpad 0) the frame auto-crops to exactly the camera framing.
  *Scene camera*: offscreen render at an exact resolution
- **Depth pass** (experimental, Scene-camera mode) — linear camera-space
  depth via a material-override view layer, delivered as a second Spout
  sender `<name>_depth` / TCP frames → component `out2`

## Quick start

### Blender

Two install options:

- **Extension (recommended)**: `python tools/build_extension.py`, then
  `Preferences > Get Extensions > ⌄ > Install from Disk…` →
  `dist/td_blender_bridge-<ver>.zip`
- **Legacy add-on**: `Preferences > Add-ons > Install from Disk…` →
  `blender/td_blender_bridge.py`

Then:

1. 3D viewport → **N** → **TD Bridge** tab → **Start Bridge**
2. **Start Frame Server** (transport defaults to Spout)
3. Viewport shading **Rendered**, overlays off, camera view (Numpad 0) —
   that's your game view
4. Optional: enable **Start bridge automatically** in the add-on
   preferences and the bridge (and frame server) come up on their own
   every session — zero clicks

An example pair lives in `examples/` (`TDBridge_demo.toe` +
`TDBridge_demo.blend`) — open both, and the loop is running.

For Spout, install the `SpoutGL` wheel into Blender's user modules once:

```
"<blender>\python\bin\python.exe" -m pip install --target ^
  "%APPDATA%\Blender Foundation\Blender\5.2\scripts\modules" SpoutGL
```

Blender must run the **OpenGL** backend (Preferences → System → GPU Backend)
for Spout; both apps must be on the same GPU.

### TouchDesigner

Drop **`touchdesigner/TDBridge.tox`** into your project and fill in the
Bridge parameter page:

| Parameter | What it does |
|---|---|
| Host / ports | Where Blender listens (defaults match the add-on) |
| Active | Master on/off for all streaming |
| Camera COMP | Streams as the Blender camera (name par beside it) |
| Points / Instances CHOP | `SOP to CHOP` or `POP to CHOP` with `tx ty tz` or `P_0 P_1 P_2` (+ color/velocity/scale/rotation channels) |
| Mesh SOP (+ positions CHOP) | Triangulated SOP; the CHOP is the fast deform path |
| Texture TOP | Streamed into a Blender image datablock |
| Params CHOP + `param_map` table | Each row: `channel │ object │ datapath` |
| Send Camera / Geometry / Textures / Params | Per-category kill switches |
| Receive Frames, Frame Source | Blender frames via `spout` or `tcp` → `out1` (color), `out2` (depth) |

The loose-script setup (Text DATs + Execute DAT) still works — see
`touchdesigner/td_blender_sender.py` / `blender_receiver.py` headers.

Objects auto-create in Blender when an unknown name arrives; if the object
exists, the stream drives it. **Build the look in Blender, perform it from
TD.** While live, baked actions on streamed objects are parked automatically
and restored on Stop Bridge.

## Performance recipes

- **Points/meshes**: always reference a `SOP to CHOP` / `POP to CHOP`, not
  the SOP — `numpyArray()` is orders of magnitude faster. Grid-style SOPs
  need a **Convert SOP** (to Polygons) before the topology read.
- **Viewport cost**: the returned frame is whatever the viewport renders —
  subsurf viewport levels and EEVEE samples are your frame-rate knobs.
  Keep one 3D viewport visible; capture rides its draw loop.
- **Frames**: Spout + Viewport capture adds almost nothing on top of the
  viewport render. The TCP transport scales with resolution (readback +
  socket + upload); shrink the viewport or use Scene-camera mode at a fixed
  size if you need cheap frames cross-machine.
- **Geometry size**: bodies over 512 KB are zlib-compressed automatically;
  `tools/stress_test.py` streams a 1M-point cloud for scale testing.
- Debug a silent sender with `mod('td_blender_sender').LAST_ERROR[0]`;
  the Blender panel shows the last apply/capture error.

## Coordinates & conventions

- TD is y-up / −z-forward; Blender is z-up. Positions and vectors map
  `(x, y, z)td → (x, −z, y)b`; scale swaps y/z; per-point rotations travel
  as quaternions and swizzle `(w,x,y,z) → (w,x,−z,y)`. Cameras keep local
  −Z as the view axis (see `td_matrix_to_blender`).
- Camera FOV: TD focal/aperture → horizontal FOV → Blender lens with
  horizontal sensor fit.
- If transforms arrive mirrored on your TD build, set
  `TRANSPOSE_MATRIX = True` in the sender.

See [docs/protocol.md](docs/protocol.md) for the wire format (v2) and
[PLAN.md](PLAN.md) for the roadmap and per-phase status.

## Requirements

- Blender 4.2+ (developed/tested on 5.2 LTS); OpenGL backend for Spout
- TouchDesigner 2023+ (tested 2025.33070); numpy bundled
- Spout path: Windows, both apps on the same GPU, `SpoutGL` wheel installed
- TCP path: any OS, same machine or LAN (open ports 9500–9502)

## License

MIT
