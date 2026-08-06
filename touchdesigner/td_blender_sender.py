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
# op can be a SOP-to-CHOP (fast, recommended: channels tx ty tz [cr cg cb ca])
# or a SOP (slow python fallback).
POINT_OPS = {
    # 'sopto_points': 'TD_Points',
}

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
    def __init__(self):
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tcp = None

    def send_json(self, obj):
        try:
            self.udp.sendto(json.dumps(obj).encode(), (HOST, UDP_PORT))
        except OSError:
            pass

    def send_frame(self, kind, name, body):
        nm = name.encode()
        payload = b'TDBG' + bytes([1, kind]) + struct.pack('<H', len(nm)) + nm + body
        data = struct.pack('<I', len(payload)) + payload
        for attempt in range(2):
            if self.tcp is None:
                try:
                    self.tcp = socket.create_connection((HOST, TCP_PORT), timeout=0.25)
                    self.tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    self.tcp = None
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


def _positions_from_chop(chop):
    """(N,3) float32 positions + optional (N,4) colors from a SOP-to-CHOP."""
    import numpy as np
    arr = chop.numpyArray()   # (numChans, numSamples) float32
    names = [c.name for c in chop.chans()]
    def idx(n):
        return names.index(n) if n in names else None
    ix, iy, iz = idx('tx'), idx('ty'), idx('tz')
    if ix is None:
        return None, None
    pos = np.ascontiguousarray(arr[[ix, iy, iz]].T.astype(np.float32))
    col = None
    ir, ig, ib = idx('cr'), idx('cg'), idx('cb')
    if ir is not None:
        ia = idx('ca')
        a = arr[ia] if ia is not None else np.ones(arr.shape[1], np.float32)
        col = np.ascontiguousarray(
            np.stack([arr[ir], arr[ig], arr[ib], a], axis=1).astype(np.float32))
    return pos, col


def send_points(td_path, bl_name):
    o = op(td_path)
    if o is None:
        return
    if o.family == 'CHOP':
        pos, col = _positions_from_chop(o)
        if pos is None:
            return
        n = pos.shape[0]
        if col is not None:
            body = struct.pack('<IB', n, 1) + pos.tobytes() + col.tobytes()
        else:
            body = struct.pack('<IB', n, 0) + pos.tobytes()
    else:  # SOP fallback (slow for large counts)
        pts = o.points
        n = len(pts)
        buf = bytearray()
        for p in pts:
            buf += struct.pack('<fff', p.x, p.y, p.z)
        body = struct.pack('<IB', n, 0) + bytes(buf)
    _net.send_frame(1, bl_name, body)


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
    pos = None
    if pos_chop_path:
        chop = op(pos_chop_path)
        if chop is not None and chop.family == 'CHOP':
            pos, _ = _positions_from_chop(chop)
    if pos is None:
        import numpy as np
        pos = np.empty((npts, 3), np.float32)
        for i, p in enumerate(sop.points):
            pos[i, 0], pos[i, 1], pos[i, 2] = p.x, p.y, p.z

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
    body = (struct.pack('<II', pos.shape[0], len(tri_arr) // 3)
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
    for td_path, bl_name in CAMERAS.items():
        _safe(send_camera, td_path, bl_name)
    for td_path, bl_name in XFORM_OBJS.items():
        _safe(send_xform, td_path, bl_name)
    _safe(send_params)
    for td_path, bl_name in POINT_OPS.items():
        _safe(send_points, td_path, bl_name)
    for td_path, bl_name in MESH_SOPS.items():
        _safe(send_mesh, td_path, bl_name)
