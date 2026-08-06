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

| kind | body |
|---|---|
| 1 points | `u32 count, u8 has_color, count*3 f32 xyz [, count*4 f32 rgba]` |
| 2 mesh | `u32 nverts, u32 ntris, nverts*3 f32 xyz, ntris*3 u32 indices` |

Positions are in TD space (converted on the Blender side with numpy).
Colors land in a `td_color` FLOAT_COLOR point attribute.
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
