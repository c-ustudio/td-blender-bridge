# td-blender-bridge

**Use Blender EEVEE as a realtime render engine for TouchDesigner** — the way
you'd use Unreal, but with Blender.

TouchDesigner streams cameras, transforms, parameters, point clouds and
deforming meshes into a live Blender scene; EEVEE renders it; frames stream
back into a TouchDesigner Script TOP. Everything runs over plain sockets —
no plugins to compile, just Python on both ends.

```
┌──────────────────┐   UDP 9500  xforms/cams/params (JSON)    ┌─────────────────┐
│   TouchDesigner  │ ───────────────────────────────────────► │     Blender     │
│                  │   TCP 9501  points/meshes (binary)       │   (EEVEE live)  │
│  td_blender_     │ ───────────────────────────────────────► │                 │
│  sender.py       │                                          │  td_blender_    │
│                  │   TCP 9502  RGBA frames back             │  bridge.py      │
│  blender_        │ ◄─────────────────────────────────────── │  (add-on)       │
│  receiver.py     │                                          │                 │
└──────────────────┘                                          └─────────────────┘
```

## Features

- **Camera sync** — TD Camera COMP world transform, FOV, near/far → Blender
  camera, with correct y-up → z-up and view-axis conversion (lookat supported)
- **Object transforms** — drive any Blender object from a TD COMP's world matrix
- **Parameters** — CHOP channels → arbitrary Blender datapaths
  (`data.energy`, `scale`, material node values, …)
- **Point clouds** — ~100k points @ 60 fps via `SOP to CHOP` + `numpyArray()`,
  optional per-point RGBA into a `td_color` attribute
- **Meshes** — triangulated SOPs with topology caching; deform-only updates are
  a fast `foreach_set` path
- **Frames back** — EEVEE viewport rendered offscreen through the scene camera,
  pushed as RGBA8 over TCP into a Script TOP (default 960×540 @ 30 fps)
- **Record & bake** — capture a live TD performance, bake camera/params to
  keyframes, then render the sequence offline in EEVEE (or Cycles)

## Quick start

### Blender

1. `Edit > Preferences > Add-ons > Install from Disk…` → `blender/td_blender_bridge.py`
2. 3D viewport → press **N** → **TD Bridge** tab → **Start Bridge**
3. Optional (frames back to TD): **Start Frame Server**
4. Set viewport shading to **Rendered** — that's your game view

### TouchDesigner

1. Text DAT `td_blender_sender` ← `touchdesigner/td_blender_sender.py`
2. Text DAT `blender_receiver` ← `touchdesigner/blender_receiver.py` (optional, for frames back)
3. Script TOP `blender_frame` with callbacks:

   ```python
   def onCook(scriptOp):
       arr = mod('blender_receiver').latest_array()
       if arr is not None:
           scriptOp.copyNumpyArray(arr)
       return
   ```

4. Execute DAT (*Frame Start* on):

   ```python
   def onFrameStart(frame):
       mod('td_blender_sender').tick()
       mod('blender_receiver').start()
       op('blender_frame').cook(force=True)
       return
   ```

5. Edit the CONFIG block at the top of `td_blender_sender`:

   ```python
   CAMERAS    = {'/project1/cam1': 'TD_Cam'}
   XFORM_OBJS = {'/project1/geo1': 'Cube'}
   PARAM_CHOP = '/project1/params'
   PARAM_MAP  = {'energy': ('Light', 'data.energy')}
   POINT_OPS  = {'/project1/sopto1': 'TD_Points'}
   MESH_SOPS  = {'/project1/geo1/convert1': 'TD_Mesh'}
   MESH_POS_CHOPS = {'/project1/geo1/convert1': '/project1/sopto1'}
   ```

Objects are auto-created in Blender when an unknown name arrives (cameras for
camera messages, empties for transforms, meshes for geometry). If the object
already exists, the stream drives it — that's the intended workflow: **build
the look in Blender, perform it from TD.**

## Performance recipes

- **Points**: reference a `SOP to CHOP` (channels `tx ty tz`, optional
  `cr cg cb ca`) in `POINT_OPS`, not the SOP itself — `numpyArray()` is
  orders of magnitude faster than Python point loops.
- **Meshes**: grids/spheres output a single `Mesh` prim that can't be
  indexed — put a **Convert SOP** (to Polygons) in front. Topology re-sends
  only when point/prim counts change; per-frame deformation goes through the
  CHOP listed in `MESH_POS_CHOPS`.
- **Point cloud rendering**: add a Geometry Nodes modifier
  (`Mesh to Points → Set Material`) to the points object; the streamed colors
  are in the `td_color` point attribute.
- **Frames back**: 960×540 @ 30 fps is comfortable on localhost. For
  production-grade zero-copy sharing on Windows, a Spout add-on for Blender +
  Spout In TOP is the upgrade path; the built-in TCP stream needs no installs.
- Debug a silent sender with `mod('td_blender_sender').LAST_ERROR[0]`.

## Record & bake (final quality)

1. TD Bridge panel → **Start Recording**, perform in TD, **Stop Recording**
2. **Bake Recording to Keyframes** — camera transform + lens, object
   transforms and params become keyframes at the scene frame rate
3. Render the sequence properly in EEVEE — or switch the scene to Cycles

Geometry streams aren't recorded (data volume); export Alembic from TD for
final geometry passes.

## Coordinates & conventions

- TD is y-up / −z-forward; Blender is z-up. Positions map
  `(x, y, z)td → (x, −z, y)blender`, matrices are converted accordingly —
  including the subtlety that cameras must keep local −Z as the view axis
  (plain change-of-basis conjugation silently points cameras at the floor;
  see `td_matrix_to_blender` in the add-on).
- Camera FOV: TD focal/aperture → horizontal FOV → Blender lens with
  horizontal sensor fit.
- If transforms arrive mirrored/twisted on your TD build, set
  `TRANSPOSE_MATRIX = True` in the sender (tdu.Matrix ordering).

See [docs/protocol.md](docs/protocol.md) for the wire format.

## Requirements

- Blender 4.2+ (tested on 5.2 LTS), any OS
- TouchDesigner 2023+ (tested on 2025.33070), Python 3.11 with numpy (bundled)
- Same machine or LAN (set `HOST` in the TD scripts; open ports 9500–9502)

## License

MIT
