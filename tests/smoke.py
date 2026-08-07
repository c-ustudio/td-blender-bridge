"""Headless Blender smoke test: register the add-on, start the bridge on
test ports, feed real packets through the sockets, assert objects appear.

Run:  blender -b --factory-startup --python tests/smoke.py
Exits non-zero on failure (CI-friendly).
"""
import json
import os
import socket
import struct
import sys
import time

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "blender"))

UDP, TCP = 19500, 19501


def fail(msg):
    print("SMOKE FAIL:", msg)
    sys.exit(1)


import td_blender_bridge as tb  # noqa: E402

tb.register()
if not tb.start_bridge(UDP, TCP):
    fail("bridge did not start: %s" % tb.S.last_error)

# camera over UDP (identity matrix, translation y=2 in TD space)
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
u.sendto(json.dumps({
    "t": "xform", "n": "SmokeCam",
    "m": [1, 0, 0, 0, 0, 1, 0, 2, 0, 0, 1, 0, 0, 0, 0, 1],
    "cam": {"fov": 45.0}, "fr": 12.0, "tm": 1.5,
}).encode(), ("127.0.0.1", UDP))

# light over UDP
u.sendto(json.dumps({
    "t": "xform", "n": "SmokeLight",
    "m": [1, 0, 0, 0, 0, 1, 0, 4, 0, 0, 1, 0, 0, 0, 0, 1],
    "light": {"type": "cone", "dimmer": 0.5, "color": [1, 0.5, 0.25],
              "angle": 30.0, "delta": 5.0},
}).encode(), ("127.0.0.1", UDP))

# points (v2, with color) over TCP
import numpy as np  # noqa: E402

n = 100
pos = np.random.rand(n, 3).astype(np.float32)
col = np.random.rand(n, 4).astype(np.float32)
body = struct.pack("<IB", n, 1) + pos.tobytes() + col.tobytes()
nm = b"SmokePts"
payload = b"TDBG" + bytes([2, 1]) + struct.pack("<H", len(nm)) + nm + body
c = socket.create_connection(("127.0.0.1", TCP), timeout=2)
c.sendall(struct.pack("<I", len(payload)) + payload)

deadline = time.time() + 5.0
ok = False
while time.time() < deadline:
    time.sleep(0.2)
    tb._apply_latest()
    cam = bpy.data.objects.get("SmokeCam")
    lt = bpy.data.objects.get("SmokeLight")
    pts = bpy.data.objects.get("SmokePts")
    if cam and lt and pts:
        ok = True
        break
if not ok:
    fail("objects missing after 5s (cam=%s light=%s pts=%s, err=%r)" % (
        bpy.data.objects.get("SmokeCam"), bpy.data.objects.get("SmokeLight"),
        bpy.data.objects.get("SmokePts"), tb.S.last_error))

cam = bpy.data.objects["SmokeCam"]
if cam.type != 'CAMERA':
    fail("SmokeCam is %s, not CAMERA" % cam.type)
# TD (0,2,0) -> Blender (0, 0, 2)
if abs(cam.matrix_world.translation.z - 2.0) > 1e-4:
    fail("camera position conversion wrong: %s" % cam.matrix_world.translation)

lt = bpy.data.objects["SmokeLight"]
if lt.type != 'LIGHT' or lt.data.type != 'SPOT':
    fail("SmokeLight wrong type: %s/%s" % (lt.type, getattr(lt.data, 'type', '?')))
if abs(lt.data.energy - 500.0) > 1e-3:
    fail("light energy heuristic wrong: %s" % lt.data.energy)

pts = bpy.data.objects["SmokePts"]
if len(pts.data.vertices) != n:
    fail("point count %d != %d" % (len(pts.data.vertices), n))
if pts.data.attributes.get("td_color") is None:
    fail("td_color attribute missing")
if pts.modifiers.get("TDB Points") is None:
    fail("auto GN modifier missing")

if tb.S.td_frame != 12.0:
    fail("TD clock not captured: %r" % tb.S.td_frame)

tb.stop_bridge()
tb.unregister()
print("SMOKE OK: camera + light + points + attributes + GN + clock")
