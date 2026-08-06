# td_blender_sender - paste this into a Text DAT named 'td_blender_sender'
#
# Streams cameras, transforms, CHOP params and SOP geometry from TouchDesigner
# to the "TouchDesigner Bridge" Blender add-on (UDP 9500 / TCP 9501).
#
# Wiring:
#   1. Paste this file into a Text DAT named  td_blender_sender
#   2. Create an Execute DAT with:
#
#        def onFrameStart(frame):
#            mod('td_blender_sender').tick()
#            return
#
#      and turn its 'Frame Start' toggle ON.
#   3. Edit the CONFIG section below to point at your operators.
#   4. In Blender: enable the add-on, press "Start Bridge" in the
#      3D viewport sidebar (N) > TD Bridge tab.
#
# Performance notes:
#   - For point clouds use a SOP to CHOP with channels tx ty tz (and
#     optionally cr cg cb / ca for color) and reference THAT chop in
#     POINT_OPS. numpyArray() is orders of magnitude faster than sop.points.
#   - For meshes, put a Convert SOP (to triangles) before referencing.
#     Topology is only re-sent when the point/prim count changes;
#     per-frame deformation goes through a SOP to CHOP referenced in
#     MESH_POS_CHOPS (same point order as the SOP, which SOPtoCHOP keeps).

import json
import socket
import struct
import math
import time
import zlib

# ----------------------------- CONFIG ---------------------------------------

HOST = '127.0.0.1'        # machine running Blender
UDP_PORT = 9500
TCP_PORT = 9501

# TD camera COMP -> Blender object name (created as a camera if missing)
CAMERAS = {
    'cam1': 'TD_Cam',
}

# TD COMPs whose world transform drives a Blender object (existing or empty)
XFORM_OBJS = {
    # 'geo1': 'Cube',
}

# CHOP -> per-channel mapping to Blender object datapaths
# channel name -> (blender object, datapath)
PARAM_CHOP = None         # e.g. 'params'
PARAM_MAP = {
    # 'energy':   ('Light', 'data.energy'),
    # 'cube_sx':  ('Cube',  'scale'),        # scalar broadcast to vectors
}

# point clouds: op -> Blender object name.
# op can be a SOP-to-CHOP (fast, recommended) or a SOP (slow python fallback).
# Recognized CHOP channels (all optional except tx ty tz):
#   tx ty tz               positions
#   cr cg cb [ca]          color        -> td_color attribute
#   vx vy vz               velocity     -> td_velocity (motion blur vectors)
#   sx sy sz               scale        -> td_scale    (instancing)
#   rx ry rz (degrees)     rotation     -> td_rot      (instancing)
POINT_OPS = {
    # 'sopto_points': 'TD_Points',
}

# instance transforms: SOP-to-CHOP (same channels as POINT_OPS) -> Blender
# object. Arrives as points with td_scale/td_rot; Blender auto-adds a
# "TDB Instances" GN modifier - pick any object to instance in its UI.
INSTANCE_OPS = {
    # 'sopto_instances': 'TD_Instances',
}

# textures: TOP -> Blender image datablock (referenced by name in shaders
# via an Image Texture node). Heavy for large TOPs - one CPU readback per
# frame; keep control textures small.
TOP_OPS = {
    # 'noise1': 'TD_Tex',
}

# protocol version: 2 adds per-point vel/scale/rot, mesh normals/UVs,
# textures and compression. Set to 1 when talking to a v0.2 Blender add-on.
PROTOCOL = 2
# zlib-compress (level 1) geometry bodies larger than this; None disables.
COMPRESS_MIN = 512 * 1024

# meshes: SOP (triangulate with a Convert SOP first) -> Blender object name
MESH_SOPS = {
    # 'convert1': 'TD_Mesh',
}
# optional fast path for mesh deformation: SOP name -> SOP-to-CHOP with tx ty tz
MESH_POS_CHOPS = {
    # 'convert1': 'sopto_mesh',
}

# If your camera/objects come through mirrored or twisted, flip this.
TRANSPOSE_MATRIX = False

# ----------------------------- internals -------------------------------------

class _Net:
    # After a failed connect, wait this long before trying again. Without the
    # backoff a dead Blender stalls TD's whole frame loop: each geometry send
    # retries connect() and a refused loopback connect burns the full 0.25 s
    # timeout on Windows (~0.5 s/frame with points + mesh configured).
    RETRY_S = 2.0

    def __init__(self):
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tcp = None
        self.next_try = 0.0

    def send_json(self, obj):
        obj.update(_STAMP)   # TD master clock: timeline frame + absTime
        try:
            self.udp.sendto(json.dumps(obj).encode(), (HOST, UDP_PORT))
        except OSError:
            pass

    def send_frame(self, kind, name, body):
        if (PROTOCOL >= 2 and COMPRESS_MIN is not None
                and len(body) >= COMPRESS_MIN):
            body = zlib.compress(body, 1)
            kind |= 0x80
        nm = name.encode()
        payload = (b'TDBG' + bytes([PROTOCOL, kind])
                   + struct.pack('<H', len(nm)) + nm + body)
        data = struct.pack('<I', len(payload)) + payload
        for attempt in range(2):
            if self.tcp is None:
                if time.time() < self.next_try:
                    return
                try:
                    self.tcp = socket.create_connection((HOST, TCP_PORT), timeout=0.25)
                    self.tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self.next_try = 0.0
                except OSError:
                    self.tcp = None
                    self.next_try = time.time() + self.RETRY_S
                    return
            try:
                self.tcp.sendall(data)
                return
            except OSError:
                try:
                    self.tcp.close()
                except OSError:
                    pass
                self.tcp = None


_net = _Net()
_topo_cache = {}   # sop path -> (npoints, nprims)
_STAMP = {}        # refreshed once per tick(); merged into every UDP message


def _update_stamp():
    """fr = component timeline frame (loops with the project timeline, drives
    Blender's 'Slave timeline to TD'); tm = absTime.seconds (monotonic, used
    for frame-accurate recording timestamps)."""
    try:
        _STAMP['fr'] = float(me.time.frame)
    except Exception:
        _STAMP['fr'] = float(absTime.frame)
    _STAMP['tm'] = float(absTime.seconds)


def _matrix16(m):
    """tdu.Matrix -> 16 floats, row-major."""
    if TRANSPOSE_MATRIX:
        return [m[c, r] for r in range(4) for c in range(4)]
    return [m[r, c] for r in range(4) for c in range(4)]


def _cam_fov(cam):
    """Horizontal FOV in degrees from a TD Camera COMP."""
    try:
        if cam.par.projection.eval() == 'perspective':
            focal = cam.par.focal.eval()
            aperture = cam.par.aperture.eval()
            return math.degrees(2.0 * math.atan((aperture * 0.5) / focal))
    except Exception:
        pass
    return 45.0


def send_camera(td_path, bl_name):
    cam = op(td_path)
    if cam is None:
        return
    _net.send_json({
        't': 'xform', 'n': bl_name,
        'm': _matrix16(cam.worldTransform),
        'cam': {'fov': _cam_fov(cam),
                'near': cam.par.near.eval(),
                'far': cam.par.far.eval()},
    })


def send_xform(td_path, bl_name):
    comp = op(td_path)
    if comp is None:
        return
    _net.send_json({'t': 'xform', 'n': bl_name, 'm': _matrix16(comp.worldTransform)})


def send_params():
    if not PARAM_CHOP:
        return
    chop = op(PARAM_CHOP)
    if chop is None:
        return
    batch = []
    for ch in chop.chans():
        target = PARAM_MAP.get(ch.name)
        if target:
            batch.append([target[0], target[1], float(ch[0])])
    if batch:
        _net.send_json({'t': 'params', 'd': batch})


def _euler_deg_to_quat(e):
    """(N,3) XYZ euler degrees (TD default Rx Ry Rz order) -> (N,4) quats
    (w,x,y,z), vectorized. R = Rz @ Ry @ Rx."""
    import numpy as np
    h = np.radians(e.astype(np.float64)) * 0.5
    cx, sx = np.cos(h[:, 0]), np.sin(h[:, 0])
    cy, sy = np.cos(h[:, 1]), np.sin(h[:, 1])
    cz, sz = np.cos(h[:, 2]), np.sin(h[:, 2])
    w1, x1, y1, z1 = cy * cx, cy * sx, sy * cx, -sy * sx   # qy * qx
    return np.ascontiguousarray(np.stack([
        cz * w1 - sz * z1,
        cz * x1 - sz * y1,
        cz * y1 + sz * x1,
        cz * z1 + sz * w1,
    ], axis=1).astype(np.float32))


def _chop_arrays(chop):
    """Extract recognized channel groups from a SOP-to-CHOP as float32 arrays."""
    import numpy as np
    arr = chop.numpyArray()   # (numChans, numSamples) float32
    names = [c.name for c in chop.chans()]

    def idx(n):
        return names.index(n) if n in names else None

    def vec(*alternatives):
        """First alternative whose channels all exist. Accepts both SOP-to-
        CHOP names (tx ty tz, cr cg cb...) and POP-to-CHOP names (P_0 P_1
        P_2, Color_0...)."""
        for chans in alternatives:
            ids = [idx(c) for c in chans]
            if not any(i is None for i in ids):
                return np.ascontiguousarray(arr[ids].T.astype(np.float32))
        return None

    pos = vec(('tx', 'ty', 'tz'), ('P_0', 'P_1', 'P_2'))
    if pos is None:
        return None
    out = {'pos': pos}
    rgb = vec(('cr', 'cg', 'cb'), ('Color_0', 'Color_1', 'Color_2'))
    if rgb is not None:
        ia = idx('ca')
        if ia is None:
            ia = idx('Color_3')
        a = (arr[ia].astype(np.float32) if ia is not None
             else np.ones(arr.shape[1], np.float32))
        out['col'] = np.ascontiguousarray(
            np.concatenate([rgb, a[:, None]], axis=1).astype(np.float32))
    out['vel'] = vec(('vx', 'vy', 'vz'), ('v_0', 'v_1', 'v_2'),
                     ('V_0', 'V_1', 'V_2'))
    scale = vec(('sx', 'sy', 'sz'), ('Scale_0', 'Scale_1', 'Scale_2'))
    if scale is None:
        ps = idx('pscale')          # uniform scale broadcast to xyz
        if ps is not None:
            s1 = arr[ps].astype(np.float32)
            scale = np.ascontiguousarray(np.repeat(s1[:, None], 3, axis=1))
    out['scale'] = scale
    rot = vec(('rx', 'ry', 'rz'), ('Rot_0', 'Rot_1', 'Rot_2'))
    out['rot'] = _euler_deg_to_quat(rot) if rot is not None else None
    out['nrm'] = vec(('nx', 'ny', 'nz'), ('N(0)', 'N(1)', 'N(2)'),
                     ('N_0', 'N_1', 'N_2'))
    out['uv'] = vec(('u', 'v'), ('uv(0)', 'uv(1)'), ('UV_0', 'UV_1'))
    return out


def _pack_points(data):
    pos = data['pos']
    n = pos.shape[0]
    flags = 0
    blocks = [pos.tobytes()]
    if data.get('col') is not None:
        flags |= 1
        blocks.append(data['col'].tobytes())
    if PROTOCOL >= 2:
        if data.get('vel') is not None:
            flags |= 2
            blocks.append(data['vel'].tobytes())
        if data.get('scale') is not None:
            flags |= 4
            blocks.append(data['scale'].tobytes())
        if data.get('rot') is not None:
            flags |= 8
            blocks.append(data['rot'].tobytes())
    return struct.pack('<IB', n, flags) + b''.join(blocks)


def send_points(td_path, bl_name, kind=1):
    o = op(td_path)
    if o is None:
        return
    if o.family == 'CHOP':
        data = _chop_arrays(o)
        if data is None:
            return
        body = _pack_points(data)
    else:  # SOP fallback (slow for large counts)
        pts = o.points
        n = len(pts)
        buf = bytearray()
        for p in pts:
            buf += struct.pack('<fff', p.x, p.y, p.z)
        body = struct.pack('<IB', n, 0) + bytes(buf)
    _net.send_frame(kind, bl_name, body)


def send_instances(td_path, bl_name):
    send_points(td_path, bl_name, kind=3)


def send_top(td_path, bl_name):
    """Stream a TOP into a Blender image datablock (kind 4, RGBA8)."""
    if PROTOCOL < 2:
        return
    o = op(td_path)
    if o is None or o.family != 'TOP':
        return
    import numpy as np
    arr = o.numpyArray(delayed=True)   # (h, w, 4) float32, bottom-up
    if arr is None:
        return
    h, w = arr.shape[0], arr.shape[1]
    px = np.clip(arr * 255.0, 0.0, 255.0).astype(np.uint8)
    body = struct.pack('<BHH', 1, w, h) + px.tobytes()
    _net.send_frame(4, bl_name, body)


def _sop_triangles(sop):
    tris = []
    for prim in sop.prims:
        if type(prim).__name__ == 'Mesh':
            # Grid/Sphere etc. output a single Mesh prim that can't be
            # vertex-indexed -- put a Convert SOP (to Polygons) before this.
            raise TypeError(
                "SOP %s outputs Mesh prims; add a Convert SOP first" % sop.path)
        nv = len(prim)
        for i in range(1, nv - 1):   # fan triangulation
            tris.append(prim[0].point.index)
            tris.append(prim[i].point.index)
            tris.append(prim[i + 1].point.index)
    return tris


def send_mesh(td_path, bl_name):
    sop = op(td_path)
    if sop is None:
        return
    npts, nprims = len(sop.points), len(sop.prims)

    pos_chop_path = MESH_POS_CHOPS.get(td_path)
    data = None
    if pos_chop_path:
        chop = op(pos_chop_path)
        if chop is not None and chop.family == 'CHOP':
            data = _chop_arrays(chop)
    if data is None:
        import numpy as np
        pos = np.empty((npts, 3), np.float32)
        for i, p in enumerate(sop.points):
            pos[i, 0], pos[i, 1], pos[i, 2] = p.x, p.y, p.z
        data = {'pos': pos}
    pos = data['pos']

    key = td_path
    tris = None
    if _topo_cache.get(key) != (npts, nprims):
        tris = _sop_triangles(sop)
        _topo_cache[key] = (npts, nprims)
    else:
        tris = _topo_cache.get(key + '/tris')
    if tris is None:
        tris = _sop_triangles(sop)
    _topo_cache[key + '/tris'] = tris

    import numpy as np
    tri_arr = np.asarray(tris, np.uint32)
    nv, ntri = pos.shape[0], len(tri_arr) // 3
    if PROTOCOL >= 2:
        flags = 0
        extra = b''
        if data.get('nrm') is not None and data['nrm'].shape[0] == nv:
            flags |= 1
            extra += data['nrm'].tobytes()
        if data.get('uv') is not None and data['uv'].shape[0] == nv:
            flags |= 2
            extra += data['uv'].tobytes()
        body = (struct.pack('<IIB', nv, ntri, flags)
                + pos.astype(np.float32).tobytes() + tri_arr.tobytes() + extra)
    else:
        body = (struct.pack('<II', nv, ntri)
                + pos.astype(np.float32).tobytes() + tri_arr.tobytes())
    _net.send_frame(2, bl_name, body)


LAST_ERROR = [None]   # inspect via: mod('td_blender_sender').LAST_ERROR[0]


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception:
        import traceback
        LAST_ERROR[0] = traceback.format_exc()[-500:]


def tick():
    """Call once per frame from an Execute DAT onFrameStart.

    Each send is isolated so one bad source never stops the others;
    the most recent failure is kept in LAST_ERROR[0].
    """
    _safe(_update_stamp)
    for td_path, bl_name in CAMERAS.items():
        _safe(send_camera, td_path, bl_name)
    for td_path, bl_name in XFORM_OBJS.items():
        _safe(send_xform, td_path, bl_name)
    _safe(send_params)
    for td_path, bl_name in POINT_OPS.items():
        _safe(send_points, td_path, bl_name)
    for td_path, bl_name in INSTANCE_OPS.items():
        _safe(send_instances, td_path, bl_name)
    for td_path, bl_name in MESH_SOPS.items():
        _safe(send_mesh, td_path, bl_name)
    for td_path, bl_name in TOP_OPS.items():
        _safe(send_top, td_path, bl_name)
