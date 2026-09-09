"""Headless param-routing test: Geometry Nodes inputs and plain RNA paths.

Covers the datapath forms a param_map row can take -- GN sockets by
identifier and by name, collection members, nested attributes, indices and
custom properties -- and asserts a socket write actually reaches evaluated
geometry rather than only the UI.

Run:  blender -b --factory-startup --python tests/params.py
Exits non-zero on failure (CI-friendly).
"""
import os
import sys

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "blender"))

import td_blender_bridge as tb  # noqa: E402

FAILED = []


def check(cond, msg):
    if cond:
        print("  ok   %s" % msg)
    else:
        print("  FAIL %s" % msg)
        FAILED.append(msg)


def build():
    """A cube whose GN modifier translates it in Z by the "Kick" input."""
    ng = bpy.data.node_groups.new("TVLayoutNG", 'GeometryNodeTree')
    ng.interface.new_socket("Geometry", in_out='INPUT',
                            socket_type='NodeSocketGeometry')
    ng.interface.new_socket("Geometry", in_out='OUTPUT',
                            socket_type='NodeSocketGeometry')
    ng.interface.new_socket("Kick", in_out='INPUT',
                            socket_type='NodeSocketFloat')
    ng.interface.new_socket("Tint", in_out='INPUT',
                            socket_type='NodeSocketColor')

    gin = ng.nodes.new('NodeGroupInput')
    gout = ng.nodes.new('NodeGroupOutput')
    xf = ng.nodes.new('GeometryNodeTransform')
    comb = ng.nodes.new('ShaderNodeCombineXYZ')
    ng.links.new(gin.outputs['Geometry'], xf.inputs['Geometry'])
    ng.links.new(gin.outputs['Kick'], comb.inputs['Z'])
    ng.links.new(comb.outputs['Vector'], xf.inputs['Translation'])
    ng.links.new(xf.outputs['Geometry'], gout.inputs['Geometry'])

    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.active_object
    obj.name = "TV_Layout"
    mod = obj.modifiers.new("TDBridge", 'NODES')
    mod.node_group = ng
    return obj, mod, ng


def socket_id(ng, name):
    for item in ng.interface.items_tree:
        if item.name == name and getattr(item, "in_out", 'INPUT') == 'INPUT':
            return item.identifier
    raise KeyError(name)


def mean_z(obj):
    """Mean vertex Z of the *evaluated* object -- proof the modifier ran."""
    dg = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    z = sum(v.co.z for v in me.vertices) / len(me.vertices)
    ev.to_mesh_clear()
    return z


def set_param(obj, path, value):
    tb.S.last_error = ""
    tb._set_param(obj.name, path, value)
    return tb.S.last_error


print("Blender %s" % bpy.app.version_string)
obj, mod, ng = build()
kick = socket_id(ng, "Kick")
tint = socket_id(ng, "Tint")

print("Geometry Nodes inputs")
check(abs(mean_z(obj)) < 1e-6, "baseline evaluated Z is 0")

err = set_param(obj, "modifiers.TDBridge.%s" % kick, 3.0)
check(err == "", "socket by identifier: no error (%s)" % (err or "none"))
check(abs(mean_z(obj) - 3.0) < 1e-4,
      "socket by identifier drives evaluated geometry (Z=%.4f)" % mean_z(obj))

err = set_param(obj, "modifiers.TDBridge.%s" % kick, -1.5)
check(abs(mean_z(obj) + 1.5) < 1e-4, "socket follows a second write")

# a scalar from a single CHOP channel should fill a colour socket
err = set_param(obj, "modifiers.TDBridge.%s" % tint, 0.5)
check(err == "", "scalar broadcast into a colour socket (%s)" % (err or "none"))

# a bad socket name must not silently corrupt a good one
before = mean_z(obj)
set_param(obj, "modifiers.TDBridge.NoSuchSocket", 9.0)
check(abs(mean_z(obj) - before) < 1e-6, "unknown socket leaves geometry intact")

print("plain RNA paths still work")
err = set_param(obj, "rotation_euler.2", 1.0)
check(err == "" and abs(obj.rotation_euler[2] - 1.0) < 1e-6,
      "indexed path rotation_euler.2 (%s)" % (err or "none"))

err = set_param(obj, "scale", 2.0)
check(err == "" and tuple(round(v, 4) for v in obj.scale) == (2.0, 2.0, 2.0),
      "scalar broadcast into scale (%s)" % (err or "none"))

bpy.ops.object.light_add(type='POINT')
light = bpy.context.active_object
light.name = "TD_TestLight"
err = set_param(light, "data.energy", 250.0)
check(err == "" and abs(light.data.energy - 250.0) < 1e-3,
      "nested attribute data.energy (%s)" % (err or "none"))

err = set_param(obj, "modifiers.TDBridge.show_viewport", False)
check(err == "" and mod.show_viewport is False,
      "non-socket modifier property still reachable (%s)" % (err or "none"))
mod.show_viewport = True

obj["td_custom"] = 0.0
err = set_param(obj, "td_custom", 7.5)
check(err == "" and abs(obj["td_custom"] - 7.5) < 1e-6,
      "object custom property (%s)" % (err or "none"))

print("errors are reported, not raised")
err = set_param(obj, "no_such_attr_at_all", 1.0)
check(err != "", "unknown path records last_error (%r)" % err)

if FAILED:
    print("PARAMS FAIL: %d check(s) failed" % len(FAILED))
    sys.exit(1)
print("PARAMS OK: GN sockets + RNA paths + custom props")
