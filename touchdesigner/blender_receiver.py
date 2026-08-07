# blender_receiver - paste into a Text DAT named 'blender_receiver'
#
# Receives the EEVEE frame stream from the Blender add-on's frame server
# (TCP, default port 9502) on a background thread and hands the latest
# frame to a Script TOP.
#
# Wiring:
#   1. Text DAT named  blender_receiver  with this file's contents.
#   2. Script TOP named e.g. 'blender_frame'; in its callbacks DAT put:
#
#        def onCook(scriptOp):
#            arr = mod('blender_receiver').latest_array()
#            if arr is not None:
#                scriptOp.copyNumpyArray(arr)
#            return
#
#   3. Start the receiver and force the TOP to cook each frame — add to
#      your Execute DAT (the same one driving td_blender_sender):
#
#        def onFrameStart(frame):
#            mod('td_blender_sender').tick()
#            mod('blender_receiver').start()
#            op('blender_frame').cook(force=True)
#            return
#
#   4. In Blender's TD Bridge panel press "Start Frame Server".

import socket
import struct
import threading
import time

HOST = '127.0.0.1'
PORT = 9502

STATE = {'latest': None, 'w': 0, 'h': 0, 'run': False, 'thread': None,
         'frames': 0, 'connected': False,
         'tm': 0.0,                    # TD timestamp carried by TDBF v2
         'depth': None, 'dw': 0, 'dh': 0}


def _reader():
    while STATE['run']:
        try:
            s = socket.create_connection((HOST, PORT), timeout=1.0)
        except OSError:
            STATE['connected'] = False
            time.sleep(0.5)
            continue
        STATE['connected'] = True
        buf = bytearray()
        try:
            while STATE['run']:
                chunk = s.recv(1 << 22)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 4:
                    (plen,) = struct.unpack_from('<I', buf, 0)
                    if plen > 64 * 1024 * 1024:
                        buf.clear()
                        break
                    if len(buf) < 4 + plen:
                        break
                    payload = bytes(buf[4:4 + plen])
                    del buf[:4 + plen]
                    if payload[:4] == b'TDBF':
                        ver, fmt = payload[4], payload[5]
                        w, h = struct.unpack_from('<HH', payload, 6)
                        off = 10
                        if ver >= 2:
                            (STATE['tm'],) = struct.unpack_from('<d', payload, 10)
                            off = 18
                        if fmt == 1:      # RGBA8 color
                            STATE['latest'] = payload[off:]
                            STATE['w'], STATE['h'] = w, h
                            STATE['frames'] += 1
                        elif fmt == 3:    # grayscale depth (RGBA8)
                            STATE['depth'] = payload[off:]
                            STATE['dw'], STATE['dh'] = w, h
        except OSError:
            pass
        finally:
            STATE['connected'] = False
            try:
                s.close()
            except OSError:
                pass


def start():
    """Idempotent - safe to call every frame."""
    t = STATE['thread']
    if t is not None and t.is_alive():
        return
    STATE['run'] = True
    STATE['thread'] = threading.Thread(target=_reader, daemon=True)
    STATE['thread'].start()


def stop():
    STATE['run'] = False


def latest_array():
    """Latest frame as an (h, w, 4) uint8 numpy array, or None."""
    data = STATE['latest']
    if data is None:
        return None
    import numpy as np
    w, h = STATE['w'], STATE['h']
    a = np.frombuffer(data, np.uint8)
    if a.size != w * h * 4:
        return None
    return a.reshape(h, w, 4)


def depth_array():
    """Latest depth pass as an (h, w, 4) uint8 numpy array, or None."""
    data = STATE['depth']
    if data is None:
        return None
    import numpy as np
    w, h = STATE['dw'], STATE['dh']
    a = np.frombuffer(data, np.uint8)
    if a.size != w * h * 4:
        return None
    return a.reshape(h, w, 4)


def latency_seconds(now_td_seconds):
    """End-to-end latency: TD clock at frame arrival minus the TD timestamp
    the frame carried (TDBF v2, TCP transport). Call from the main thread
    with absTime.seconds."""
    if STATE['tm'] <= 0.0:
        return None
    return now_td_seconds - STATE['tm']
