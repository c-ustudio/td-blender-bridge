bl_info = {
    "name": "TouchDesigner Bridge (TD -> Blender)",
    "author": "enric + Claude",
    "version": (0, 3, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > TD Bridge",
    "description": "Two-way realtime bridge with TouchDesigner: drive cameras, transforms, params and geometry from TD (UDP/TCP), stream EEVEE frames back to TD, record & bake to keyframes",
    "category": "Import-Export",
}

# ---------------------------------------------------------------------------
# Protocol
#
# UDP (default port 9500) - small, per-frame JSON datagrams:
#   {"t":"xform","n":"<object name>","m":[16 floats row-major TD world matrix],
#    "cam":{"fov":45.0}}            # "cam" key present => object is a camera
#   {"t":"param","n":"<object>","p":"data.energy","v":5.0}
#   {"t":"params","d":[["<object>","<datapath>",value], ...]}   # batch
#
# TCP (default port 9501) - length-prefixed binary geometry frames:
#   [u32 payload_len][payload]
#   payload: b"TDBG" u8 version u8 kind u16 name_len name(utf8) body
#     kind 1 = points: u32 count, u8 has_color,
#                      count*3 f32 positions [, count*4 f32 colors]
#     kind 2 = mesh:   u32 nverts, u32 ntris,
#                      nverts*3 f32 positions, ntris*3 u32 indices
#
# TCP (default port 9502) - frame server (Blender -> TD), push stream:
#   [u32 payload_len][payload]
#   payload: b"TDBF" u8 version u8 fmt(1=RGBA8) u16 w u16 h pixels
#   EEVEE viewport is rendered offscreen through the scene camera and pushed
#   to every connected client (TD's blender_receiver DAT + Script TOP).
#
# Coordinates: TD is y-up / -z forward, Blender is z-up. All incoming data is
# in TD space; converted here (x, y, z)td -> (x, -z, y)blender.
# ---------------------------------------------------------------------------

import bpy
import json
import math
import socket
import select
import struct
import threading
import time
import zlib
import numpy as np
from bpy.app.handlers import persistent
from mathutils import Matrix

# -- coordinate conversion ---------------------------------------------------

C4 = Matrix(((1, 0, 0, 0),
             (0, 0, -1, 0),
             (0, 1, 0, 0),
             (0, 0, 0, 1)))
C4i = C4.inverted()


def td_matrix_to_blender(v, view_convention=False):
    """Convert a TD world matrix (16 floats, row-major) to Blender.

    view_convention=True is for cameras/lights: preserves the object's local
    -Z as the view direction and +Y as up (TD and Blender share the OpenGL
    camera convention, but a plain change-of-basis conjugation would remap
    which local axis is 'forward').
    """
    m = Matrix((v[0:4], v[4:8], v[8:12], v[12:16]))
    if view_convention:
        return C4 @ m
    return C4 @ m @ C4i


def td_points_to_blender(pos):
    """pos: (N,3) float32 numpy array in TD space -> new array in Blender space."""
    out = np.empty_like(pos)
    out[:, 0] = pos[:, 0]
    out[:, 1] = -pos[:, 2]
    out[:, 2] = pos[:, 1]
    return out


# -- shared state ------------------------------------------------------------

class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.udp = None
        self.tcp = None
        self.conns = []
        self.bufs = {}
        # latest-value stores (reader thread writes, main thread drains)
        self.xforms = {}   # name -> (vals16, camdict|None)
        self.params = {}   # (name, path) -> value
        self.geo = {}      # name -> dict(kind=, pos=bytes, tris=bytes|None, n=, ncol=)
        self.tex = {}      # name -> dict(w=, h=, px=bytes)  (kind 4 images)
        self.dirty_x = set()
        self.dirty_p = set()
        self.dirty_g = set()
        self.dirty_t = set()
        # stats
        self.pkts = 0
        self.pps = 0.0
        self._pps_t = time.time()
        self._pps_n = 0
        self.last_error = ""
        # recording
        self.record = False
        self.rec_t0 = None
        self.rec = []      # (t, {name: (vals16, camdict)}, {(name,path): value})
        self.bake_active = False   # bake_recording() drives frame_set itself
        # live/bake arbitration: actions parked while their object is being
        # streamed (baked keyframes would override the stream every frame,
        # since animation evaluates after frame_change_pre)
        self.muted = {}    # object name -> {"obj": (action, slot), "data": ...}
        # TD master clock (stamped on every UDP message by the sender)
        self.td_frame = None   # TD timeline frame ("fr")
        self.td_time = None    # TD absTime.seconds ("tm")
        self.rec_td_t0 = None  # TD-time origin of the current recording
        # per-object exponential smoothing state: name -> (loc, quat, scale)
        self.smooth = {}
        # frame server (Blender -> TD)
        self.fs_sock = None
        self.fs_clients = []
        self.fs_pending = {}   # client -> bytearray still to flush
        self.fs_running = False
        self.fs_offscreen = None
        self.fs_size = (960, 540)
        self.fs_interval = 1.0 / 30.0
        self.fs_frames = 0
        self.fs_error = ""
        # capture happens in a View3D draw callback (EEVEE offscreen draws
        # crash outside a draw context); the timer only does socket I/O
        self.fs_draw_handle = None
        self.fs_latest = None      # last captured RGBA8 bytes
        self.fs_last_cap = 0.0
        self.fs_seq = 0            # bumped per capture
        self.fs_sent_seq = 0


S = _State()


# -- reader thread (sockets only, never touches bpy) -------------------------

def _handle_udp(data):
    try:
        msg = json.loads(data)
    except Exception:
        return
    t = msg.get("t")
    with S.lock:
        fr = msg.get("fr")
        if fr is not None:
            S.td_frame = fr
        tm = msg.get("tm")
        if tm is not None:
            S.td_time = tm
        if t == "xform":
            S.xforms[msg["n"]] = (msg["m"], msg.get("cam"))
            S.dirty_x.add(msg["n"])
        elif t == "param":
            S.params[(msg["n"], msg["p"])] = msg["v"]
            S.dirty_p.add((msg["n"], msg["p"]))
        elif t == "params":
            for n, p, v in msg.get("d", []):
                S.params[(n, p)] = v
                S.dirty_p.add((n, p))
        S.pkts += 1
        S._pps_n += 1


def _handle_tcp_frame(payload):
    if len(payload) < 8 or payload[0:4] != b"TDBG":
        return
    ver = payload[4]
    kind = payload[5]
    (name_len,) = struct.unpack_from("<H", payload, 6)
    name = payload[8:8 + name_len].decode("utf-8", "replace")
    body = payload[8 + name_len:]
    if kind & 0x80:            # v2: zlib-compressed body
        kind &= 0x7F
        try:
            body = zlib.decompress(body)
        except zlib.error:
            return
    try:
        if kind in (1, 3):     # 1 = points, 3 = instance transforms
            g = _parse_points(body, kind)
        elif kind == 2:        # mesh
            g = _parse_mesh(body, ver)
        elif kind == 4:        # image
            g = _parse_image(body)
        else:
            return
    except (struct.error, ValueError):
        return
    with S.lock:
        if kind == 4:
            S.tex[name] = g
            S.dirty_t.add(name)
        else:
            S.geo[name] = g
            S.dirty_g.add(name)
        S.pkts += 1
        S._pps_n += 1


def _parse_image(body):
    fmt, w, h = struct.unpack_from("<BHH", body, 0)
    if fmt != 1:
        raise ValueError("unsupported image format %d" % fmt)
    px = body[5:5 + w * h * 4]
    if len(px) < w * h * 4:
        raise ValueError("short frame")
    return {"w": w, "h": h, "px": px}


def _parse_points(body, kind):
    """v1's 'has_color' byte is bit 0 of the v2 flags byte, so both versions
    parse identically. flags: 1 color, 2 velocity, 4 scale, 8 rotation."""
    n, flags = struct.unpack_from("<IB", body, 0)
    off = 5

    def take(nbytes):
        nonlocal off
        chunk = body[off:off + nbytes]
        if len(chunk) < nbytes:
            raise ValueError("short frame")
        off += nbytes
        return chunk

    pos = take(n * 12)
    col = take(n * 16) if flags & 1 else None
    vel = take(n * 12) if flags & 2 else None
    scale = take(n * 12) if flags & 4 else None
    rot = take(n * 16) if flags & 8 else None    # quaternions (w,x,y,z)
    return {"kind": kind, "n": n, "pos": pos, "col": col, "vel": vel,
            "scale": scale, "rot": rot, "tris": None}


def _parse_mesh(body, ver):
    """v2 inserts a flags byte after the counts: 1 normals, 2 UVs."""
    nv, nt = struct.unpack_from("<II", body, 0)
    off = 8
    flags = 0
    if ver >= 2:
        flags = body[8]
        off = 9

    def take(nbytes):
        nonlocal off
        chunk = body[off:off + nbytes]
        if len(chunk) < nbytes:
            raise ValueError("short frame")
        off += nbytes
        return chunk

    pos = take(nv * 12)
    tris = take(nt * 12)
    nrm = take(nv * 12) if flags & 1 else None
    uv = take(nv * 8) if flags & 2 else None
    return {"kind": 2, "n": nv, "nt": nt, "pos": pos, "tris": tris,
            "col": None, "nrm": nrm, "uv": uv}


def _drain_conn(conn):
    buf = S.bufs[conn]
    while True:
        if len(buf) < 4:
            return
        (plen,) = struct.unpack_from("<I", buf, 0)
        if plen > 256 * 1024 * 1024:      # insane frame -> drop connection buffer
            buf.clear()
            return
        if len(buf) < 4 + plen:
            return
        _handle_tcp_frame(bytes(buf[4:4 + plen]))
        del buf[:4 + plen]


def _reader():
    while S.running:
        socks = [s for s in (S.udp, S.tcp) if s] + list(S.conns)
        try:
            r, _, _ = select.select(socks, [], [], 0.05)
        except OSError:
            break
        for s in r:
            if s is S.udp:
                try:
                    while True:
                        data, _ = S.udp.recvfrom(65535)
                        _handle_udp(data)
                except (BlockingIOError, OSError):
                    pass
            elif s is S.tcp:
                try:
                    c, _ = S.tcp.accept()
                    c.setblocking(False)
                    S.conns.append(c)
                    S.bufs[c] = bytearray()
                except OSError:
                    pass
            else:
                try:
                    chunk = s.recv(1 << 22)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        S.conns.remove(s)
                        del S.bufs[s]
                        s.close()
                    except (ValueError, KeyError, OSError):
                        pass
                    continue
                S.bufs[s] += chunk
                _drain_conn(s)


# -- main-thread apply (bpy work) --------------------------------------------

def _ensure_object(name, camdict):
    obj = bpy.data.objects.get(name)
    if obj is None:
        if camdict is not None:
            data = bpy.data.cameras.new(name)
            obj = bpy.data.objects.new(name, data)
        else:
            obj = bpy.data.objects.new(name, None)
            obj.empty_display_size = 0.3
        bpy.context.scene.collection.objects.link(obj)
    return obj


def _apply_camera_props(obj, camdict):
    if obj.type != 'CAMERA' or not camdict:
        return
    cam = obj.data
    fov = camdict.get("fov")
    if fov:
        cam.sensor_fit = 'HORIZONTAL'
        cam.lens = (cam.sensor_width / 2.0) / math.tan(math.radians(fov) / 2.0)
    if "near" in camdict:
        cam.clip_start = max(0.001, camdict["near"])
    if "far" in camdict:
        cam.clip_end = camdict["far"]


def _set_param(name, path, value):
    obj = bpy.data.objects.get(name)
    if obj is None:
        return
    holder = obj
    parts = path.split(".")
    try:
        for p in parts[:-1]:
            holder = holder[int(p)] if p.isdigit() else getattr(holder, p)
        last = parts[-1]
        if last.isdigit():
            holder[int(last)] = value
        else:
            cur = getattr(holder, last)
            # allow scalar broadcast into vectors/colors
            if hasattr(cur, "__len__") and not hasattr(value, "__len__"):
                setattr(holder, last, [value] * len(cur))
            else:
                setattr(holder, last, value)
    except Exception as e:
        S.last_error = f"param {name}.{path}: {e}"


def _ensure_points_material():
    mat = bpy.data.materials.get("TDB_Points")
    if mat is None:
        mat = bpy.data.materials.new("TDB_Points")
        mat.use_nodes = True
        nt = mat.node_tree
        bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is not None:
            attr = nt.nodes.new('ShaderNodeAttribute')
            attr.attribute_name = "td_color"
            attr.location = (bsdf.location.x - 300, bsdf.location.y)
            nt.links.new(attr.outputs['Color'], bsdf.inputs['Base Color'])
            try:  # a touch of emission so points read in dark scenes
                nt.links.new(attr.outputs['Color'],
                             bsdf.inputs['Emission Color'])
                bsdf.inputs['Emission Strength'].default_value = 0.5
            except KeyError:
                pass
    return mat


GN_VER = 2   # bump to rebuild the managed node groups on next arrival


def _managed_group(name, builder):
    ng = bpy.data.node_groups.get(name)
    if ng is not None and ng.get("tdb_ver") != GN_VER:
        bpy.data.node_groups.remove(ng)   # stale layout: rebuild
        ng = None
    if ng is None:
        ng = builder(name)
        ng["tdb_ver"] = GN_VER
    return ng


def _ensure_gn_modifier(obj, mod_name, group_name, builder):
    ng = _managed_group(group_name, builder)
    mod = obj.modifiers.get(mod_name)
    if mod is None:
        mod = obj.modifiers.new(mod_name, 'NODES')
    if mod is not None and mod.node_group is not ng:
        mod.node_group = ng


def _gn_wire_transforms(ng, iop):
    """Feed td_scale / td_rot named attributes into Instance on Points, with
    identity fallbacks when the stream doesn't carry them."""
    na_s = ng.nodes.new('GeometryNodeInputNamedAttribute')
    na_s.data_type = 'FLOAT_VECTOR'
    na_s.inputs['Name'].default_value = "td_scale"
    na_s.location = (iop.location.x - 240, -280)
    sw_s = ng.nodes.new('GeometryNodeSwitch')
    sw_s.input_type = 'VECTOR'
    sw_s.location = (iop.location.x - 40, -280)
    sw_s.inputs['False'].default_value = (1.0, 1.0, 1.0)
    ng.links.new(na_s.outputs['Exists'], sw_s.inputs['Switch'])
    ng.links.new(na_s.outputs['Attribute'], sw_s.inputs['True'])
    ng.links.new(sw_s.outputs['Output'], iop.inputs['Scale'])

    na_r = ng.nodes.new('GeometryNodeInputNamedAttribute')
    na_r.data_type = 'QUATERNION'
    na_r.inputs['Name'].default_value = "td_rot"
    na_r.location = (iop.location.x - 240, -460)
    sw_r = ng.nodes.new('GeometryNodeSwitch')
    sw_r.input_type = 'ROTATION'
    sw_r.location = (iop.location.x - 40, -460)
    ng.links.new(na_r.outputs['Exists'], sw_r.inputs['Switch'])
    ng.links.new(na_r.outputs['Attribute'], sw_r.inputs['True'])
    ng.links.new(sw_r.outputs['Output'], iop.inputs['Rotation'])


def _build_points_group(name):
    """Points -> shaded sphere instances (visible in EEVEE renders)."""
    ng = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    ng.interface.new_socket("Geometry", in_out='INPUT',
                            socket_type='NodeSocketGeometry')
    ng.interface.new_socket("Geometry", in_out='OUTPUT',
                            socket_type='NodeSocketGeometry')
    rad = ng.interface.new_socket("Radius", in_out='INPUT',
                                  socket_type='NodeSocketFloat')
    rad.default_value = 0.02
    rad.min_value = 0.0
    n_in = ng.nodes.new('NodeGroupInput')
    n_in.location = (-420, 0)
    ico = ng.nodes.new('GeometryNodeMeshIcoSphere')
    ico.location = (-420, -170)
    ico.inputs['Subdivisions'].default_value = 1
    iop = ng.nodes.new('GeometryNodeInstanceOnPoints')
    iop.location = (-180, 0)
    setmat = ng.nodes.new('GeometryNodeSetMaterial')
    setmat.location = (60, 0)
    setmat.inputs['Material'].default_value = _ensure_points_material()
    n_out = ng.nodes.new('NodeGroupOutput')
    n_out.location = (280, 0)
    ng.links.new(n_in.outputs['Geometry'], iop.inputs['Points'])
    ng.links.new(n_in.outputs['Radius'], ico.inputs['Radius'])
    ng.links.new(ico.outputs['Mesh'], iop.inputs['Instance'])
    ng.links.new(iop.outputs['Instances'], setmat.inputs['Geometry'])
    ng.links.new(setmat.outputs['Geometry'], n_out.inputs['Geometry'])
    _gn_wire_transforms(ng, iop)
    return ng


def _build_instances_group(name):
    """Streamed transforms -> instances of a user-chosen object. Until
    'Use Object' is enabled the points show as spheres (same as TDB_Points)
    so a fresh stream is never invisible."""
    ng = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    ng.interface.new_socket("Geometry", in_out='INPUT',
                            socket_type='NodeSocketGeometry')
    ng.interface.new_socket("Geometry", in_out='OUTPUT',
                            socket_type='NodeSocketGeometry')
    ng.interface.new_socket("Instance Object", in_out='INPUT',
                            socket_type='NodeSocketObject')
    ng.interface.new_socket("Use Object", in_out='INPUT',
                            socket_type='NodeSocketBool')
    rad = ng.interface.new_socket("Radius", in_out='INPUT',
                                  socket_type='NodeSocketFloat')
    rad.default_value = 0.05
    rad.min_value = 0.0
    n_in = ng.nodes.new('NodeGroupInput')
    n_in.location = (-680, 0)
    ico = ng.nodes.new('GeometryNodeMeshIcoSphere')
    ico.location = (-680, -200)
    ico.inputs['Subdivisions'].default_value = 1
    setmat = ng.nodes.new('GeometryNodeSetMaterial')
    setmat.location = (-500, -200)
    setmat.inputs['Material'].default_value = _ensure_points_material()
    info = ng.nodes.new('GeometryNodeObjectInfo')
    info.location = (-500, -380)
    info.transform_space = 'ORIGINAL'
    info.inputs['As Instance'].default_value = True
    sw = ng.nodes.new('GeometryNodeSwitch')
    sw.input_type = 'GEOMETRY'
    sw.location = (-320, -200)
    iop = ng.nodes.new('GeometryNodeInstanceOnPoints')
    iop.location = (-120, 0)
    n_out = ng.nodes.new('NodeGroupOutput')
    n_out.location = (120, 0)
    ng.links.new(n_in.outputs['Radius'], ico.inputs['Radius'])
    ng.links.new(ico.outputs['Mesh'], setmat.inputs['Geometry'])
    ng.links.new(n_in.outputs['Instance Object'], info.inputs['Object'])
    ng.links.new(n_in.outputs['Use Object'], sw.inputs['Switch'])
    ng.links.new(setmat.outputs['Geometry'], sw.inputs['False'])
    ng.links.new(info.outputs['Geometry'], sw.inputs['True'])
    ng.links.new(n_in.outputs['Geometry'], iop.inputs['Points'])
    ng.links.new(sw.outputs['Output'], iop.inputs['Instance'])
    ng.links.new(iop.outputs['Instances'], n_out.inputs['Geometry'])
    _gn_wire_transforms(ng, iop)
    return ng


def _update_geo(name):
    g = S.geo.get(name)
    if not g:
        return
    n = g["n"]
    me = bpy.data.meshes.get("TDB_" + name)
    if me is None:
        me = bpy.data.meshes.new("TDB_" + name)
    obj = bpy.data.objects.get(name)
    if obj is None:
        obj = bpy.data.objects.new(name, me)
        bpy.context.scene.collection.objects.link(obj)
    elif obj.data is not me:
        obj.data = me
    # instancing a sphere per point melts down on huge clouds (1M points ->
    # ~80M tris); past this size leave the raw vertices alone
    if n <= 250_000:
        if g["kind"] == 1:
            _ensure_gn_modifier(obj, "TDB Points", "TDB_Points",
                                _build_points_group)
        elif g["kind"] == 3:
            _ensure_gn_modifier(obj, "TDB Instances", "TDB_Instances",
                                _build_instances_group)

    pos = np.frombuffer(g["pos"], dtype=np.float32).reshape(-1, 3)[:n]
    pos = td_points_to_blender(pos).astype(np.float32)

    if g["kind"] in (1, 3):  # points / instance transforms
        if len(me.vertices) == n and len(me.polygons) == 0:
            me.vertices.foreach_set("co", pos.ravel())
            me.update()
        else:
            me.clear_geometry()
            me.from_pydata(pos.tolist(), [], [])
        if g.get("col") is not None:
            col = np.frombuffer(g["col"], dtype=np.float32).reshape(-1, 4)[:n]
            _set_point_attr(me, "td_color", 'FLOAT_COLOR', "color", n, col)
        if g.get("vel") is not None:
            vel = np.frombuffer(g["vel"], dtype=np.float32).reshape(-1, 3)[:n]
            _set_point_attr(me, "td_velocity", 'FLOAT_VECTOR', "vector", n,
                            td_points_to_blender(vel))
        if g.get("scale") is not None:
            sc = np.frombuffer(g["scale"], dtype=np.float32).reshape(-1, 3)[:n]
            _set_point_attr(me, "td_scale", 'FLOAT_VECTOR', "vector", n,
                            sc[:, (0, 2, 1)])       # y/z swap, no sign flip
        if g.get("rot") is not None:
            q = np.frombuffer(g["rot"], dtype=np.float32).reshape(-1, 4)[:n]
            qb = np.empty_like(q)                   # conjugation by the basis
            qb[:, 0] = q[:, 0]                      # change C (a +90deg X
            qb[:, 1] = q[:, 1]                      # rotation) permutes the
            qb[:, 2] = -q[:, 3]                     # vector part like a
            qb[:, 3] = q[:, 2]                      # vector: (x,-z,y)
            _set_point_attr(me, "td_rot", 'QUATERNION', "value", n, qb)
    else:  # mesh
        nt = g["nt"]
        tris = np.frombuffer(g["tris"], dtype=np.uint32).reshape(-1, 3)[:nt]
        same_topo = (len(me.vertices) == n and len(me.polygons) == nt
                     and me.get("tdb_topo") == int(n * 100003 + nt))
        if same_topo:
            me.vertices.foreach_set("co", pos.ravel())
            me.update()
        else:
            me.clear_geometry()
            me.from_pydata(pos.tolist(), [], tris.tolist())
            me["tdb_topo"] = int(n * 100003 + nt)
        me.update()
        if g.get("nrm") is not None:
            nrm = np.frombuffer(g["nrm"], dtype=np.float32).reshape(-1, 3)[:n]
            try:
                me.normals_split_custom_set_from_vertices(
                    td_points_to_blender(nrm).tolist())
            except (AttributeError, RuntimeError):
                pass   # API drift across versions; auto normals still work
        if g.get("uv") is not None:
            uv = np.frombuffer(g["uv"], dtype=np.float32).reshape(-1, 2)[:n]
            if not me.uv_layers:
                me.uv_layers.new(name="td_uv")
            layer = me.uv_layers[0]
            if len(layer.data) == tris.size:    # per-loop = per corner
                layer.data.foreach_set(
                    "uv", np.ascontiguousarray(uv[tris.ravel()]).ravel())


def _update_tex(name, t):
    """kind-4 image -> Blender image datablock (reference it by name in any
    Image Texture shader node)."""
    w, h = t["w"], t["h"]
    img = bpy.data.images.get(name)
    if img is None:
        img = bpy.data.images.new(name, w, h, alpha=True)
    if img.size[0] != w or img.size[1] != h:
        img.scale(w, h)
    px = np.frombuffer(t["px"], dtype=np.uint8).astype(np.float32)
    px *= 1.0 / 255.0
    img.pixels.foreach_set(px)
    img.update()


def _set_point_attr(me, name, dtype, key, n, data):
    attr = me.attributes.get(name)
    if attr is None or attr.data_type != dtype or len(attr.data) != n:
        if attr is not None:
            me.attributes.remove(attr)
        attr = me.attributes.new(name, dtype, 'POINT')
    attr.data.foreach_set(
        key, np.ascontiguousarray(data, dtype=np.float32).ravel())


def _redraw():
    wm = bpy.data.window_managers[0]
    for w in wm.windows:
        for a in w.screen.areas:
            if a.type == 'VIEW_3D':
                a.tag_redraw()


def _park_action(store, key, holder):
    ad = holder.animation_data
    if ad is not None and ad.action is not None:
        slot = getattr(ad, "action_slot", None)
        store[key] = (ad.action, slot)
        ad.action = None


def _mute_actions(obj):
    """Park a streamed object's action (and its camera data's action) so
    baked keyframes can't override the live stream. Restored on Stop Bridge."""
    entry = S.muted.setdefault(obj.name, {})
    _park_action(entry, "obj", obj)
    if obj.type == 'CAMERA':
        _park_action(entry, "data", obj.data)


def _restore_actions():
    for name, entry in S.muted.items():
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        for key, holder in (("obj", obj),
                            ("data", obj.data if obj.type == 'CAMERA' else None)):
            parked = entry.get(key)
            if parked is None or holder is None:
                continue
            action, slot = parked
            try:
                if holder.animation_data is None:
                    holder.animation_data_create()
                holder.animation_data.action = action
                if slot is not None:
                    holder.animation_data.action_slot = slot
            except Exception as e:
                S.last_error = f"restore action {name}: {e}"
    S.muted.clear()


def _apply_latest():
    """Drain the latest-value stores and write them into the scene.

    Main thread only. While the timeline is playing (or rendering/scrubbing)
    this runs from frame_change_pre, in sync with animation evaluation; while
    paused it runs from the idle timer. Never both at once, so evaluation and
    the stream can't fight over the same properties (the v0.2 flicker).
    """
    with S.lock:
        dx = {n: S.xforms[n] for n in S.dirty_x}
        dp = {k: S.params[k] for k in S.dirty_p}
        dg = set(S.dirty_g)
        dt = {n: S.tex[n] for n in S.dirty_t}
        S.dirty_x.clear()
        S.dirty_p.clear()
        S.dirty_g.clear()
        S.dirty_t.clear()
        now = time.time()
        if now - S._pps_t >= 1.0:
            S.pps = S._pps_n / (now - S._pps_t)
            S._pps_n = 0
            S._pps_t = now

    changed = bool(dx or dp or dg or dt)
    if changed:
        try:
            scene = bpy.context.scene
            live_mute = getattr(scene, "tdb_live_mute", True)
            smooth = getattr(scene, "tdb_smooth", 0.0)
            for name, (vals, camdict) in dx.items():
                obj = _ensure_object(name, camdict)
                if live_mute:
                    _mute_actions(obj)
                view = camdict is not None or obj.type in ('CAMERA', 'LIGHT')
                m = td_matrix_to_blender(vals, view)
                obj.matrix_world = _smoothed(name, m, smooth)
                _apply_camera_props(obj, camdict)
            for (name, path), v in dp.items():
                _set_param(name, path, v)
            for name in dg:
                _update_geo(name)
            for name, t in dt.items():
                _update_tex(name, t)
        except Exception as e:
            S.last_error = str(e)

    if S.record and changed:
        with S.lock:
            # prefer the TD clock stamped on the stream: bake becomes
            # frame-accurate against TD instead of local arrival time
            if S.td_time is not None:
                if S.rec_td_t0 is None:
                    S.rec_td_t0 = S.td_time
                t = S.td_time - S.rec_td_t0
            else:
                t = time.time() - S.rec_t0
            S.rec.append((t, dict(dx), dict(dp)))
    return changed


def _smoothed(name, m, factor):
    """Exponential smoothing toward the streamed matrix (0 = off).

    Loc/scale lerp, rotation slerp; state resets when smoothing is disabled
    so re-enabling never snaps from a stale pose.
    """
    if factor <= 0.0:
        S.smooth.pop(name, None)
        return m
    loc, rot, scale = m.decompose()
    prev = S.smooth.get(name)
    if prev is not None:
        a = 1.0 - factor
        loc = prev[0].lerp(loc, a)
        rot = prev[1].slerp(rot, a)
        scale = prev[2].lerp(scale, a)
    S.smooth[name] = (loc, rot, scale)
    return Matrix.LocRotScale(loc, rot, scale)


@persistent
def _tdb_frame_pre(scene, depsgraph=None):
    # Fires once per frame whenever Blender itself advances the timeline:
    # playback, scrubbing, rendering. bake_recording() calls frame_set in a
    # loop, so it suspends this to keep the stream out of the bake.
    if not S.running or S.bake_active:
        return
    _apply_latest()


def _is_playing():
    for wm in bpy.data.window_managers:
        for win in wm.windows:
            screen = win.screen
            if screen and screen.is_animation_playing:
                return True
    return False


def _apply_timer():
    if not S.running:
        return None
    if _is_playing():
        return 0.1     # frame_change_pre owns the apply; just poll for stop
    scene = bpy.context.scene
    if getattr(scene, "tdb_slave_timeline", False) and S.td_frame is not None:
        # TD is the master clock: follow its timeline frame. frame_set fires
        # _tdb_frame_pre, which applies the stream in sync with evaluation.
        # (Leave Blender's own playback stopped while slaved.)
        f = int(round(S.td_frame))
        if f != scene.frame_current:
            scene.frame_set(f)
            _redraw()
            return 1.0 / 60.0
    if _apply_latest():
        _redraw()
    return 1.0 / 60.0


def _install_frame_handler():
    _remove_frame_handler()
    bpy.app.handlers.frame_change_pre.append(_tdb_frame_pre)


def _remove_frame_handler():
    handlers = bpy.app.handlers.frame_change_pre
    for h in list(handlers):        # match by name: survives script re-runs
        if getattr(h, "__name__", "") == "_tdb_frame_pre":
            handlers.remove(h)


# -- frame server: EEVEE viewport -> TD -------------------------------------

def _fs_find_view3d():
    for win in bpy.data.window_managers[0].windows:
        for a in win.screen.areas:
            if a.type == 'VIEW_3D':
                region = next((r for r in a.regions if r.type == 'WINDOW'), None)
                if region:
                    return a.spaces.active, region
    return None, None


def _fs_capture():
    """Render the scene camera offscreen (EEVEE viewport shading) -> RGBA8.

    Must run inside a View3D draw callback: EEVEE's offscreen draw touches
    live GPU state and segfaults when invoked from a timer (no draw context).
    """
    import gpu
    scene = bpy.context.scene
    cam = scene.camera
    if cam is None:
        return None
    w, h = S.fs_size
    space = getattr(bpy.context, "space_data", None)
    region = bpy.context.region
    if space is None or space.type != 'VIEW_3D' or region is None:
        space, region = _fs_find_view3d()
    if space is None:
        return None
    if (S.fs_offscreen is None
            or S.fs_offscreen.width != w or S.fs_offscreen.height != h):
        S.fs_offscreen = gpu.types.GPUOffScreen(w, h)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    vm = cam.matrix_world.inverted()
    pm = cam.calc_matrix_camera(depsgraph, x=w, y=h)
    try:
        S.fs_offscreen.draw_view3d(scene, bpy.context.view_layer, space, region,
                                   vm, pm, do_color_management=True)
    except TypeError:  # older signature
        S.fs_offscreen.draw_view3d(scene, bpy.context.view_layer, space, region,
                                   vm, pm)
    with S.fs_offscreen.bind():
        import gpu as _g
        fb = _g.state.active_framebuffer_get()
        buf = fb.read_color(0, 0, w, h, 4, 0, 'UBYTE')
    buf.dimensions = w * h * 4
    return _gpu_buf_bytes(buf, w * h * 4), w, h


def _gpu_buf_bytes(buf, nbytes):
    buf.dimensions = nbytes
    try:
        return bytes(memoryview(buf))
    except TypeError:
        import numpy as _np
        return _np.array(buf.to_list(), dtype=_np.uint8).tobytes()


def _fs_grab_viewport():
    """Read the viewport's already-rendered framebuffer instead of paying for
    a second offscreen EEVEE render. Runs inside the draw callback, so the
    active framebuffer IS the viewport being drawn. Frame size follows the
    viewport; overlays/gizmos are included unless disabled in that viewport."""
    import gpu
    region = bpy.context.region
    space = getattr(bpy.context, "space_data", None)
    if region is None or space is None or space.type != 'VIEW_3D':
        return None
    w = region.width - (region.width % 2)
    h = region.height - (region.height % 2)
    if w < 8 or h < 8:
        return None
    fb = gpu.state.active_framebuffer_get()
    buf = fb.read_color(0, 0, w, h, 4, 0, 'UBYTE')
    return _gpu_buf_bytes(buf, w * h * 4), w, h


def _fs_draw():
    """View3D draw callback: capture at most once per fs_interval."""
    if not S.fs_running or not S.fs_clients:
        return
    now = time.time()
    if now - S.fs_last_cap < S.fs_interval * 0.9:
        return
    S.fs_last_cap = now
    try:
        if getattr(bpy.context.scene, "tdb_fs_mode", 'CAMERA') == 'VIEWPORT':
            result = _fs_grab_viewport()
        else:
            result = _fs_capture()
    except Exception as e:
        S.fs_error = "capture: %s" % e
        return
    if result is not None:
        S.fs_latest = result          # (pixels, w, h)
        S.fs_seq += 1


def _fs_tick():
    """Timer: sockets only. Tags redraws so _fs_draw keeps producing frames
    even when the viewport is otherwise idle."""
    if not S.fs_running:
        return None
    # accept new clients (non-blocking)
    while S.fs_sock is not None:
        try:
            c, _ = S.fs_sock.accept()
            c.setblocking(False)
            try:
                c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            S.fs_clients.append(c)
            S.fs_pending[c] = bytearray()
        except (BlockingIOError, OSError):
            break
    if not S.fs_clients:
        return S.fs_interval
    _redraw()
    if S.fs_latest is None or S.fs_seq == S.fs_sent_seq:
        return S.fs_interval / 2
    S.fs_sent_seq = S.fs_seq
    pixels, w, h = S.fs_latest
    header = b"TDBF" + bytes([1, 1]) + struct.pack("<HH", w, h)
    payload = header + pixels
    packet = struct.pack("<I", len(payload)) + payload
    S.fs_frames += 1
    for c in list(S.fs_clients):
        pending = S.fs_pending.get(c)
        if pending is None:
            continue
        if len(pending) == 0:
            pending += packet          # queue this frame
        elif len(pending) > 32 * len(packet):
            _fs_drop(c)                # client hopelessly behind
            continue
        # else: client still draining a previous frame -> drop this frame for it
        try:
            sent = c.send(pending)
            del pending[:sent]
        except BlockingIOError:
            pass
        except OSError:
            _fs_drop(c)
    return S.fs_interval / 2


def _fs_drop(c):
    try:
        S.fs_clients.remove(c)
    except ValueError:
        pass
    S.fs_pending.pop(c, None)
    try:
        c.close()
    except OSError:
        pass


def start_frame_server(port=9502, width=960, height=540, fps=30):
    stop_frame_server()
    try:
        S.fs_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        S.fs_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        S.fs_sock.bind(("0.0.0.0", port))
        S.fs_sock.listen(4)
        S.fs_sock.setblocking(False)
    except OSError as e:
        S.fs_error = "bind failed: %s" % e
        S.fs_sock = None
        return False
    S.fs_size = (int(width) // 4 * 4, int(height) // 4 * 4)
    S.fs_interval = 1.0 / max(1, fps)
    S.fs_running = True
    S.fs_frames = 0
    S.fs_error = ""
    S.fs_latest = None
    S.fs_seq = S.fs_sent_seq = 0
    S.fs_last_cap = 0.0
    S.fs_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
        _fs_draw, (), 'WINDOW', 'POST_PIXEL')
    bpy.app.timers.register(_fs_tick, first_interval=0.1)
    return True


def stop_frame_server():
    S.fs_running = False
    if S.fs_draw_handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(S.fs_draw_handle, 'WINDOW')
        except Exception:
            pass
        S.fs_draw_handle = None
    try:
        if bpy.app.timers.is_registered(_fs_tick):
            bpy.app.timers.unregister(_fs_tick)
    except Exception:
        pass
    for c in list(S.fs_clients):
        _fs_drop(c)
    if S.fs_sock:
        try:
            S.fs_sock.close()
        except OSError:
            pass
    S.fs_sock = None
    S.fs_offscreen = None
    S.fs_latest = None


# -- lifecycle ---------------------------------------------------------------

def start_bridge(udp_port=9500, tcp_port=9501):
    stop_bridge()
    S.running = True
    S.last_error = ""
    try:
        S.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        S.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        S.udp.bind(("0.0.0.0", udp_port))
        S.udp.setblocking(False)
        S.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        S.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        S.tcp.bind(("0.0.0.0", tcp_port))
        S.tcp.listen(4)
        S.tcp.setblocking(False)
    except OSError as e:
        S.last_error = f"bind failed: {e}"
        S.running = False
        for s in (S.udp, S.tcp):
            if s:
                s.close()
        S.udp = S.tcp = None
        return False
    S.thread = threading.Thread(target=_reader, daemon=True)
    S.thread.start()
    _install_frame_handler()
    bpy.app.timers.register(_apply_timer, first_interval=0.05)
    return True


def stop_bridge():
    S.running = False
    _restore_actions()
    _remove_frame_handler()
    try:
        if bpy.app.timers.is_registered(_apply_timer):
            bpy.app.timers.unregister(_apply_timer)
    except Exception:
        pass
    for s in [S.udp, S.tcp] + S.conns:
        try:
            if s:
                s.close()
        except OSError:
            pass
    S.udp = S.tcp = None
    S.conns = []
    S.bufs = {}
    if S.thread and S.thread.is_alive():
        S.thread.join(timeout=0.5)
    S.thread = None


def start_recording():
    with S.lock:
        S.rec = []
        S.rec_t0 = time.time()
        S.rec_td_t0 = None
        S.record = True


def stop_recording():
    with S.lock:
        S.record = False


def bake_recording(frame_start=1):
    """Bake recorded transform/param samples to keyframes at scene fps."""
    scene = bpy.context.scene
    fps = scene.render.fps / scene.render.fps_base
    with S.lock:
        samples = list(S.rec)
    if not samples:
        return 0
    per_frame = {}
    for t, dx, dp in samples:
        per_frame[int(round(t * fps)) + frame_start] = (dx, dp)
    S.bake_active = True   # frame_set() below fires _tdb_frame_pre
    try:
        _bake_frames(per_frame)
    finally:
        S.bake_active = False
    scene.frame_end = max(scene.frame_end, max(per_frame))
    return len(per_frame)


def _bake_frames(per_frame):
    scene = bpy.context.scene
    for frame in sorted(per_frame):
        dx, dp = per_frame[frame]
        scene.frame_set(frame)
        for name, (vals, camdict) in dx.items():
            obj = _ensure_object(name, camdict)
            view = camdict is not None or obj.type in ('CAMERA', 'LIGHT')
            obj.matrix_world = td_matrix_to_blender(vals, view)
            obj.keyframe_insert("location", frame=frame)
            obj.keyframe_insert("rotation_euler", frame=frame)
            obj.keyframe_insert("scale", frame=frame)
            if camdict:
                _apply_camera_props(obj, camdict)
                obj.data.keyframe_insert("lens", frame=frame)
        for (name, path), v in dp.items():
            _set_param(name, path, v)
            obj = bpy.data.objects.get(name)
            if obj is None:
                continue
            holder, attr = obj, path
            if "." in path:
                head, attr = path.rsplit(".", 1)
                try:
                    for p in head.split("."):
                        holder = holder[int(p)] if p.isdigit() else getattr(holder, p)
                except Exception:
                    continue
            try:
                holder.keyframe_insert(attr, frame=frame)
            except Exception:
                pass


# -- UI ----------------------------------------------------------------------

class TDB_OT_start(bpy.types.Operator):
    bl_idname = "tdb.start"
    bl_label = "Start Bridge"

    def execute(self, context):
        ok = start_bridge(context.scene.tdb_udp_port, context.scene.tdb_tcp_port)
        if not ok:
            self.report({'ERROR'}, S.last_error or "failed to start")
            return {'CANCELLED'}
        return {'FINISHED'}


class TDB_OT_stop(bpy.types.Operator):
    bl_idname = "tdb.stop"
    bl_label = "Stop Bridge"

    def execute(self, context):
        stop_bridge()
        return {'FINISHED'}


class TDB_OT_record(bpy.types.Operator):
    bl_idname = "tdb.record"
    bl_label = "Record"

    def execute(self, context):
        if S.record:
            stop_recording()
        else:
            start_recording()
        return {'FINISHED'}


class TDB_OT_bake(bpy.types.Operator):
    bl_idname = "tdb.bake"
    bl_label = "Bake Recording to Keyframes"

    def execute(self, context):
        n = bake_recording(context.scene.frame_start)
        self.report({'INFO'}, f"baked {n} frames")
        return {'FINISHED'}


class TDB_OT_fs_start(bpy.types.Operator):
    bl_idname = "tdb.fs_start"
    bl_label = "Start Frame Server"

    def execute(self, context):
        sc = context.scene
        ok = start_frame_server(sc.tdb_fs_port, sc.tdb_fs_width,
                                sc.tdb_fs_height, sc.tdb_fs_fps)
        if not ok:
            self.report({'ERROR'}, S.fs_error or "failed to start")
            return {'CANCELLED'}
        return {'FINISHED'}


class TDB_OT_fs_stop(bpy.types.Operator):
    bl_idname = "tdb.fs_stop"
    bl_label = "Stop Frame Server"

    def execute(self, context):
        stop_frame_server()
        return {'FINISHED'}


class TDB_PT_panel(bpy.types.Panel):
    bl_label = "TD Bridge"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "TD Bridge"

    def draw(self, context):
        lay = self.layout
        col = lay.column()
        col.prop(context.scene, "tdb_udp_port")
        col.prop(context.scene, "tdb_tcp_port")
        col.prop(context.scene, "tdb_live_mute")
        col.prop(context.scene, "tdb_slave_timeline")
        col.prop(context.scene, "tdb_smooth", slider=True)
        if S.running:
            col.operator("tdb.stop", icon='PAUSE')
            col.label(text=f"running - {S.pps:.0f} msg/s, {S.pkts} total")
            if S.muted:
                col.label(text=f"{len(S.muted)} action(s) parked while live",
                          icon='ACTION')
            if S.td_frame is not None:
                col.label(text=f"TD frame {S.td_frame:.0f}", icon='TIME')
        else:
            col.operator("tdb.start", icon='PLAY')
            col.label(text="stopped")
        col.separator()
        col.operator("tdb.record",
                     text=("Stop Recording" if S.record else "Start Recording"),
                     icon='REC')
        if S.rec:
            col.label(text=f"{len(S.rec)} samples recorded")
            col.operator("tdb.bake", icon='KEY_HLT')
        if S.last_error:
            col.label(text=S.last_error[:64], icon='ERROR')

        col.separator()
        box = col.box()
        box.label(text="EEVEE -> TD frames", icon='RENDER_ANIMATION')
        box.prop(context.scene, "tdb_fs_mode", text="")
        box.prop(context.scene, "tdb_fs_port")
        if context.scene.tdb_fs_mode == 'CAMERA':
            row = box.row()
            row.prop(context.scene, "tdb_fs_width")
            row.prop(context.scene, "tdb_fs_height")
        box.prop(context.scene, "tdb_fs_fps")
        if S.fs_running:
            box.operator("tdb.fs_stop", icon='PAUSE')
            box.label(text="%d clients, %d frames sent"
                      % (len(S.fs_clients), S.fs_frames))
        else:
            box.operator("tdb.fs_start", icon='PLAY')
        if S.fs_error:
            box.label(text=S.fs_error[:64], icon='ERROR')


_classes = (TDB_OT_start, TDB_OT_stop, TDB_OT_record, TDB_OT_bake,
            TDB_OT_fs_start, TDB_OT_fs_stop, TDB_PT_panel)


def register():
    # A previous run of this script (Text Editor re-run) may still have live
    # timers, handlers and sockets bound to its own module instance -- its
    # timers would keep drawing with freed GPU resources. Stop it first.
    prev_stop = bpy.app.driver_namespace.get("tdb_stop")
    if prev_stop is not None:
        try:
            prev_stop()
        except Exception:
            pass

    bpy.types.Scene.tdb_live_mute = bpy.props.BoolProperty(
        name="Live overrides baked actions", default=True,
        description="While the bridge runs, park actions on streamed objects "
                    "so baked keyframes don't fight the stream; restored on "
                    "Stop Bridge")
    bpy.types.Scene.tdb_slave_timeline = bpy.props.BoolProperty(
        name="Slave timeline to TD", default=False,
        description="Follow TD's timeline frame (stamped on the stream): "
                    "frame-accurate recording and deterministic playback. "
                    "Leave Blender's own playback stopped while enabled")
    bpy.types.Scene.tdb_smooth = bpy.props.FloatProperty(
        name="Smoothing", default=0.0, min=0.0, max=0.95,
        description="Exponential smoothing of streamed transforms "
                    "(0 = off, higher = smoother/laggier)")
    bpy.types.Scene.tdb_udp_port = bpy.props.IntProperty(
        name="UDP port", default=9500, min=1024, max=65535)
    bpy.types.Scene.tdb_tcp_port = bpy.props.IntProperty(
        name="TCP port", default=9501, min=1024, max=65535)
    bpy.types.Scene.tdb_fs_mode = bpy.props.EnumProperty(
        name="Capture", default='VIEWPORT',
        items=(('VIEWPORT', "Viewport (fast)",
                "Read the already-rendered viewport framebuffer - no second "
                "render. Frame size follows the viewport; hide overlays in "
                "the viewport for clean output"),
               ('CAMERA', "Scene camera (offscreen)",
                "Render the scene camera offscreen at the configured "
                "resolution - exact size and framing, but pays for a full "
                "second EEVEE render per frame")))
    bpy.types.Scene.tdb_fs_port = bpy.props.IntProperty(
        name="Frame port", default=9502, min=1024, max=65535)
    bpy.types.Scene.tdb_fs_width = bpy.props.IntProperty(
        name="W", default=960, min=64, max=4096)
    bpy.types.Scene.tdb_fs_height = bpy.props.IntProperty(
        name="H", default=540, min=64, max=4096)
    bpy.types.Scene.tdb_fs_fps = bpy.props.IntProperty(
        name="FPS", default=30, min=1, max=120)
    for c in _classes:
        bpy.utils.register_class(c)

    def _stop_all():
        stop_bridge()
        stop_frame_server()
    bpy.app.driver_namespace["tdb_stop"] = _stop_all


def unregister():
    stop_bridge()
    stop_frame_server()
    for c in reversed(_classes):
        try:
            bpy.utils.unregister_class(c)
        except RuntimeError:
            pass
    for p in ("tdb_udp_port", "tdb_tcp_port", "tdb_live_mute",
              "tdb_slave_timeline", "tdb_smooth", "tdb_fs_mode",
              "tdb_fs_port", "tdb_fs_width", "tdb_fs_height", "tdb_fs_fps"):
        if hasattr(bpy.types.Scene, p):
            delattr(bpy.types.Scene, p)


if __name__ == "__main__":
    register()
