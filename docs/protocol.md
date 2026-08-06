# Wire protocol

Three channels, all little-endian. Defaults: UDP 9500, TCP 9501, TCP 9502.

## UDP :9500 — control (TD → Blender)

One JSON object per datagram. Latest value wins; loss is acceptable by design
(the next frame replaces it).

Every message additionally carries the TD master clock (optional — old
senders without them still work):

```json
{"fr": 245.0, "tm": 1234.567, ...}
```

- `fr` — TD component timeline frame (loops with the project timeline).
  Drives Blender's optional **Slave timeline to TD** mode
  (`scene.frame_current` follows it).
- `tm` — TD `absTime.seconds` (monotonic). Used for frame-accurate
  recording timestamps instead of Blender-side arrival time.

### Transform / camera

```json
{"t": "xform", "n": "TD_Cam",
 "m": [16 floats, row-major TD world matrix],
 "cam": {"fov": 45.0, "near": 0.1, "far": 1000.0}}
```

- `cam` present ⇒ the object is created/treated as a camera and the matrix is
  converted with the **view-axis convention** (`M_b = C·M_td`, preserving
  local −Z as view direction). `fov` is horizontal, degrees.
- `cam` absent ⇒ generic object, conjugated conversion (`M_b = C·M_td·C⁻¹`).

### Parameters

```json
{"t": "param",  "n": "Light", "p": "data.energy", "v": 1500.0}
{"t": "params", "d": [["Light", "data.energy", 1500.0],
                       ["Cube", "scale", 0.5]]}
```

`p` is a dotted datapath resolved from the object (`data.energy`,
`data.materials.0...`; integer segments index into collections). Assigning a
scalar to a vector property broadcasts it.

## TCP :9501 — geometry (TD → Blender)

Length-prefixed frames on a persistent connection:

```
[u32 payload_len][payload]
payload := "TDBG" u8 version u8 kind u16 name_len name(utf8) body
```

Version 2 (current). Bit 7 of `kind` set ⇒ `body` is zlib-compressed
(deflate); low 7 bits are the kind.

| kind | body |
|---|---|
| 1 points | `u32 count, u8 flags, count*3 f32 xyz, [blocks]` |
| 2 mesh | `u32 nverts, u32 ntris, u8 flags(v2 only), verts, tris, [blocks]` |
| 3 instances | identical to kind 1; Blender adds a "TDB Instances" GN modifier (instance any object on the transforms) |
| 4 image | `u8 fmt(1=RGBA8), u16 w, u16 h, w*h*4 pixels (bottom-up)` → Blender image datablock named after the stream (use in any Image Texture node) |

Points/instances flag bits, optional blocks in this order:

| bit | block | Blender attribute |
|---|---|---|
| 1 | `count*4 f32 rgba` | `td_color` FLOAT_COLOR |
| 2 | `count*3 f32` velocity | `td_velocity` FLOAT_VECTOR |
| 4 | `count*3 f32` scale | `td_scale` FLOAT_VECTOR |
| 8 | `count*4 f32` quaternions (w,x,y,z) | `td_rot` QUATERNION |

Mesh flag bits: 1 ⇒ `nverts*3 f32` per-vertex normals (custom split
normals), 2 ⇒ `nverts*2 f32` UVs (`td_uv` layer, per-corner from vertex).

v1 compatibility: points' `has_color` byte is flag bit 1, so v1 points
parse as v2; v1 meshes have no flags byte (keyed on the version field).

Positions/velocities/normals are in TD space (y-up, converted Blender-side);
scale swaps y/z; quaternions swizzle `(w,x,y,z) → (w,x,-z,y)`.
Mesh topology is rebuilt only when vert/tri counts change; otherwise only
positions are updated (fast path).

## TCP :9502 — frames (Blender → TD)

Same length-prefix framing, pushed by Blender at the configured rate:

```
payload := "TDBF" u8 version u8 fmt u16 width u16 height pixels
fmt 1 = RGBA8, rows bottom-up (GL order), width*height*4 bytes
```

Per-client frame dropping: if a client hasn't finished reading the previous
frame, new frames are skipped for it; clients more than ~32 frames behind are
disconnected.
