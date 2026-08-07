# Settings & parameter reference

Complete reference for the TDBridge component (TouchDesigner) and the
TouchDesigner Bridge add-on (Blender). Wire format: [protocol.md](protocol.md).

---

## TouchDesigner — TDBridge component (Bridge page)

### Connection

| Parameter | Default | Notes |
|---|---|---|
| **Blender Host** | `127.0.0.1` | IP of the machine running Blender. Set a LAN IP for multi-machine (drives the UDP stream, TCP geometry *and* the frame receiver). |
| **UDP Port (xforms/params)** | 9500 | Camera/light/object transforms, CHOP params, TD clock. |
| **TCP Port (geometry)** | 9501 | Points, instances, meshes, textures. |
| **Frame Port (Blender→TD)** | 9502 | TCP frame stream (Frame Source `tcp`). |
| **Active** | off | Master switch — nothing streams while off. |
| **Protocol Version** | 2 | Set 1 only when talking to a v0.2 Blender add-on. |

### Sources

| Parameter | Example | What streams |
|---|---|---|
| **Camera COMP** + Blender Name | `/project1/cam1` → `TD_Cam` | World transform, FOV, near/far. Blender creates a camera on first arrival; set it as the scene camera. `lookat` is baked into the transform. |
| **Light COMP** + Blender Name | `/project1/light1` → `TD_Light` | Transform, type (point/cone/distant → point/spot/sun), `wcolor`, `dimmer` (→ watts ×1000, ×3 sun), cone angle/delta (→ spot size/blend). |
| **Points CHOP** + Blender Name | `/project1/sopto1` → `TD_Points` | A `SOP to CHOP` or `POP to CHOP`. Blender auto-adds sphere instancing (≤250k points) with the streamed colors. |
| **Instances CHOP** + Blender Name | `/project1/popto1` → `TD_Instances` | Same channels as Points; Blender adds a "TDB Instances" GN modifier — pick any object in its UI and enable *Use Object*. |
| **Mesh SOP** + Blender Name | `/project1/geo1/convert1` → `TD_Mesh` | Triangulated SOP (put a **Convert SOP** before it). Topology re-sends only on count changes. |
| **Mesh Positions CHOP** | `/project1/sopto1` | `SOP to CHOP` of the same SOP — the fast per-frame deform path. Same point order as the SOP. |
| **Texture TOP** + Blender Image Name | `/project1/noise1` → `TD_Tex` | Any TOP → Blender image datablock; reference it in an Image Texture node. Keep control textures small (full transfer per frame). |
| **Params CHOP** | `/project1/math1` | Every channel is looked up in `param_map`. |

### Recognized point/instance channels

| SOP-to-CHOP | POP-to-CHOP | Lands as |
|---|---|---|
| `tx ty tz` | `P_0 P_1 P_2` | positions (required) |
| `cr cg cb [ca]` | `Color_0..3` | `td_color` (drives the sphere material) |
| `vx vy vz` | `v_0..2` / `V_0..2` | `td_velocity` |
| `sx sy sz` | `Scale_0..2` or uniform `pscale` | `td_scale` (instance scale) |
| `rx ry rz` (deg) | `Rot_0..2` | `td_rot` quaternion (instance rotation) |
| `nx ny nz` | `N_0..2` / `N(0..2)` | mesh custom normals |
| `u v` | `UV_0/1` / `uv(0/1)` | mesh `td_uv` layer |

### param_map table (channel → Blender property)

Rows: `channel │ object │ datapath`. Any animatable property works;
integer segments index into collections; scalars broadcast into vectors.

```
energy   │ Light    │ data.energy
sunrot   │ Sun      │ rotation_euler.2
roughn   │ Suzanne  │ active_material.node_tree.nodes["Principled BSDF"].inputs[2].default_value
strength │ World    │ (use a param on a world-driving object, or drive TD_Tex instead)
mix      │ Cube     │ scale
```

Values apply live (see them in Blender's panel) and bake to keyframes
with Record.

### Send toggles

**Send Camera / Send Geometry / Send Textures / Send Params** — per-category
kill switches. A CHOP/params-only rig: everything off except Send Params.
(Send Camera also gates lights.)

### Frames back

| Parameter | Notes |
|---|---|
| **Receive Frames** | Starts the receiver + cooks the frame TOPs each frame. |
| **Frame Source** | `spout` (same machine, zero-copy) or `tcp` (any machine). Switches `out1`/`out2`. |
| **Spout Sender Name** | Must match Blender's sender name (`TDBridge`). Depth arrives as `<name>_depth`. |

Outputs: **out1** = color frames, **out2** = depth pass (when enabled in
Blender).

---

## Blender — TD Bridge panel (N-sidebar)

| Setting | Default | Notes |
|---|---|---|
| **UDP / TCP port** | 9500 / 9501 | Must match the component. The bridge listens on all interfaces. |
| **Live overrides baked actions** | on | Parks actions on streamed objects while live (baked keys evaluate after the stream and would win); restored on Stop Bridge. |
| **Slave timeline to TD** | off | Blender's playhead follows TD's timeline frame — frame-accurate recording, deterministic playback. Cancels Blender's own player while enabled. Match scene fps to TD (60). |
| **Smoothing** | 0 | Per-object exponential smoothing of streamed transforms (0 = off, 0.95 = very smooth/laggy). Position/scale lerp, rotation slerp. |
| **Start/Stop Bridge** | — | Panel shows msg/s, parked actions, incoming TD frame, and live param values. |
| **Start Recording / Bake** | — | Samples timestamp against the TD clock; Bake writes keyframes at scene fps. |

### EEVEE → TD frames

| Setting | Default | Notes |
|---|---|---|
| **Transport** | Spout | `Spout (GPU sharing)`: same machine + same GPU + OpenGL backend, near-zero cost. `TCP (portable)`: any machine/OS. |
| **Capture** | Viewport | `Viewport (fast)`: the already-rendered viewport, no second render, 1 frame latency; in camera view (Numpad 0) it auto-crops to the camera frame. `Scene camera (offscreen)`: exact resolution/framing, costs a second EEVEE render. |
| **Sender name** | TDBridge | Spout only. |
| **Frame port** | 9502 | TCP only. |
| **Compress frames (LAN)** | off | TCP only: zlib the frame stream for gigabit networks (see multi-machine below). |
| **W / H** | 960×540 | Scene-camera mode only. |
| **FPS** | 30 | Capture/send rate cap. |
| **Depth pass** | off | Experimental; Scene-camera mode only. Renders linear depth via the `TDB_Depth` material-override layer → `out2` / `<sender>_depth`. Re-range on the material's Map Range node; keep viewport overlays off. |

### Add-on preferences (Edit → Preferences → Add-ons)

| Setting | Notes |
|---|---|
| **Start bridge automatically** | Bridge comes up on Blender start / file load using the scene's ports. |
| **Also start the frame server** | Frame server too — a render node needs zero clicks after boot. |

---

## Multi-machine setup

TD on the performance machine, Blender on a render node:

1. Blender machine: install add-on, enable both auto-start preferences,
   allow inbound TCP/UDP **9500–9502** in the firewall (private networks).
2. TD machine: set the component's **Blender Host** to the render node's IP,
   **Frame Source** = `tcp`.
3. Blender: **Transport** = TCP. For gigabit links enable
   **Compress frames (LAN)** and prefer Scene-camera capture at a fixed
   resolution.

Bandwidth budget (raw RGBA8 → gigabit fits?):

| Frames | Raw | zlib (typ. 3–5×) |
|---|---|---|
| 960×540 @ 30 | 500 Mb/s | ✅ comfortably |
| 1280×720 @ 30 | 880 Mb/s | ✅ |
| 1920×1080 @ 30 | 2.0 Gb/s | ✅ borderline — test your content |
| 1920×1080 @ 60 | 4.0 Gb/s | needs 10 GbE (skip compression there) |

Geometry is already compressed over 512 KB; a 100k-point animated cloud is
a few hundred Mb/s raw and compresses well. Latency measurement keeps
working across machines with no clock sync (the timestamp rides TD's clock
both ways). The bridge has no authentication — use it on trusted/private
networks only.

---

## Recipe: audio-reactive scene

1. `audiofilein` / `audiodevin` → `audioanalyze` (or `analyze`+`math`) →
   channels like `low mid high kick`.
2. `null` after them → component **Params CHOP**; `param_map` rows:
   `kick │ Light │ data.energy`, `low │ TD_Points_scale…` — or drive GN
   inputs via object properties.
3. Geometry: your POP/SOP chain reacting to the same analysis →
   **Points CHOP**; add `cr cg cb` for spectrum-mapped color.
4. Camera: animate `cam1` (lookat + LFOs) → streamed live; enable
   **Slave timeline to TD** and Record in Blender to bake the performance
   for an offline Cycles render afterwards.
5. Frames: Spout on one machine, TCP+compression across two.
