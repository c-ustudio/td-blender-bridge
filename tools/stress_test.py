"""Stress the geometry channel: stream N points (default 1M) to the Blender
add-on and report achieved send rate and bandwidth.

Usage:
  python tools/stress_test.py [npoints] [seconds] [--fps 60] [--compress]

The Blender bridge must be running (TD Bridge panel > Start Bridge). Watch the
add-on panel's msg/s counter and the viewport while this runs; the send rate
printed here is the network-side ceiling, the viewport rate is Blender's
apply-side reality.
"""
import argparse
import math
import socket
import struct
import sys
import time
import zlib

import numpy as np

HOST, PORT = "127.0.0.1", 9501
PROTOCOL = 2


def make_cloud(n, t):
    """Animated spiral galaxy-ish cloud, y-up TD space, with color."""
    i = np.arange(n, dtype=np.float32)
    a = i * 0.0009 + t * 0.3
    r = 0.5 + 4.0 * np.sqrt(i / n)
    pos = np.stack([r * np.cos(a),
                    np.sin(i * 0.001 + t) * 0.4,
                    r * np.sin(a)], axis=1).astype(np.float32)
    col = np.stack([0.5 + 0.5 * np.sin(a),
                    0.5 + 0.5 * np.sin(a + 2.1),
                    0.5 + 0.5 * np.sin(a + 4.2),
                    np.ones(n, np.float32)], axis=1).astype(np.float32)
    return pos, col


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("npoints", nargs="?", type=int, default=1_000_000)
    ap.add_argument("seconds", nargs="?", type=float, default=10.0)
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--compress", action="store_true")
    args = ap.parse_args()

    s = socket.create_connection((HOST, PORT), timeout=2.0)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    nm = b"TDB_Stress"
    sent = 0
    payload_bytes = 0
    t0 = time.time()
    frame_budget = 1.0 / args.fps
    while time.time() - t0 < args.seconds:
        f0 = time.time()
        pos, col = make_cloud(args.npoints, f0 - t0)
        body = (struct.pack("<IB", args.npoints, 1)
                + pos.tobytes() + col.tobytes())
        kind = 1
        if args.compress:
            body = zlib.compress(body, 1)
            kind |= 0x80
        payload = (b"TDBG" + bytes([PROTOCOL, kind])
                   + struct.pack("<H", len(nm)) + nm + body)
        s.sendall(struct.pack("<I", len(payload)) + payload)
        sent += 1
        payload_bytes += len(payload)
        leftover = frame_budget - (time.time() - f0)
        if leftover > 0:
            time.sleep(leftover)
    dt = time.time() - t0
    print(f"{args.npoints:,} points x {sent} frames in {dt:.1f}s "
          f"= {sent / dt:.1f} fps send-side, "
          f"{payload_bytes / dt / 1e6:.0f} MB/s"
          f"{' (zlib)' if args.compress else ''}")
    s.close()


if __name__ == "__main__":
    main()
