# Demo network builder - run in TouchDesigner's textport (or via tdmcp-bridge
# /api/exec) to create a complete working demo that streams to Blender:
# an orbiting camera and a noise-deformed grid sent as both mesh and points.
#
# Prerequisites: a Text DAT '/project1/td_blender_sender' containing
# touchdesigner/td_blender_sender.py with:
#   CAMERAS   = {'/project1/cam1': 'TD_Cam'}
#   POINT_OPS = {'/project1/sopto_bridge': 'TD_Points'}
#   MESH_SOPS = {'/project1/geo_bridge/convert1': 'TD_Mesh'}
#   MESH_POS_CHOPS = {'/project1/geo_bridge/convert1': '/project1/sopto_bridge'}

import td

proj = op('/project1')

# geometry: grid -> animated noise -> convert (Mesh prim -> Poly prims)
g = proj.op('geo_bridge') or proj.create(td.geometryCOMP, 'geo_bridge')
grid = g.op('grid1') or g.create(td.gridSOP, 'grid1')
grid.par.rows = 60
grid.par.cols = 60
grid.par.orient = 'zx'
noise = g.op('noise1') or g.create(td.noiseSOP, 'noise1')
noise.inputConnectors[0].connect(grid)
noise.par.tx.expr = 'absTime.seconds*0.5'
noise.par.amp = 1.2
conv = g.op('convert1') or g.create(td.convertSOP, 'convert1')
conv.inputConnectors[0].connect(noise)

# fast per-frame positions
sopto = proj.op('sopto_bridge') or proj.create(td.soptoCHOP, 'sopto_bridge')
sopto.par.sop = conv.path

# orbiting camera aimed at the origin
tgt = proj.op('lookat_target') or proj.create(td.nullCOMP, 'lookat_target')
cam = proj.op('cam1') or proj.create(td.cameraCOMP, 'cam1')
cam.par.tx.expr = 'math.sin(absTime.seconds*0.4)*8'
cam.par.tz.expr = 'math.cos(absTime.seconds*0.4)*8'
cam.par.ty = 4
cam.par.lookat = tgt.path

# drive the sender (and optionally the frame receiver) every frame
ex = proj.op('td_blender_exec') or proj.create(td.executeDAT, 'td_blender_exec')
ex.par.framestart = True
ex.par.active = True
ex.text = (
    "def onFrameStart(frame):\n"
    "    mod('td_blender_sender').tick()\n"
    "    return\n"
)

print('demo network ready - press Start Bridge in Blender')
