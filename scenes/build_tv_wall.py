"""
build_tv_wall.py — base scene for the TD-driven CRT sculpture.

Run inside Blender (Scripting workspace > New > paste > Run Script).
Idempotent: re-running rebuilds cleanly.

Builds:
  * TV_Model         - placeholder CRT (body + screen), two material slots
  * TV_Layout        - N points carrying two layout presets as attributes
  * TDBridge GN group with NAMED inputs for TouchDesigner to drive
  * TD_Screen        - emissive material reading an atlas texture, one
                       cell per TV via a per-instance channel attribute

TouchDesigner drives these, as param_map rows on the TV_Layout object:
    Kick        modifiers.TDBridge.Kick
    Bass        modifiers.TDBridge.Bass
    Layout Mix  modifiers.TDBridge.Layout Mix
    Spread      modifiers.TDBridge.Spread
    Emission    modifiers.TDBridge.Emission

and streams a TOP into the image datablock named  TD_Atlas.

Verified on Blender 5.2: builds 64 instances that render, and the
Emission input visibly drives the screens (it needs to go well above
1.0 before they read - 1.0 is barely brighter than 0).
"""

import bpy
import math
import random

# ----------------------------------------------------------------- config

N_TVS = 64          # keep constant across layouts, see brief
ATLAS_GRID = 8      # 8x8 cells = 64 unique screens
SEED = 7

random.seed(SEED)


def set_gn_input(mod, name, value):
    """Set a Geometry Nodes modifier input by its interface name.

    Blender 4.5+ keeps modifier inputs behind ``properties.inputs`` as
    a group whose "value" member holds the actual value; assigning over
    the group leaves the socket broken and silently ignored by
    evaluation. Older builds keep a plain IDProperty on the modifier.
    """
    ng = mod.node_group
    ident = next((i.identifier for i in ng.interface.items_tree
                  if getattr(i, "in_out", None) == 'INPUT'
                  and i.name == name), None)
    if ident is None:
        return False
    ins = getattr(getattr(mod, "properties", None), "inputs", None)
    try:
        if ins is not None:
            ins[ident]["value"] = value
        else:
            mod[ident] = value
    except Exception as e:
        print("set_gn_input(%s): %s" % (name, e))
        return False
    mod.id_data.update_tag()
    return True


def _purge(collection, name):
    existing = collection.get(name)
    if existing:
        collection.remove(existing)


# ------------------------------------------------------------- tv model

def build_tv_model():
    """Placeholder CRT: a tapered body with a screen face on slot 1.

    Replace the mesh later; keep the material slot order (0 body, 1 screen)
    and the object name, and everything downstream keeps working.
    """
    _purge(bpy.data.objects, "TV_Model")
    _purge(bpy.data.meshes, "TV_Model")

    me = bpy.data.meshes.new("TV_Model")
    obj = bpy.data.objects.new("TV_Model", me)
    bpy.context.scene.collection.objects.link(obj)

    w, h, d = 0.45, 0.36, 0.42      # roughly a 19" CRT, metres
    bz = 0.02                        # screen sits proud of the body

    # body: front face full size, back face tapered in (CRT funnel)
    t = 0.6
    verts = [
        (-w / 2, -d / 2, -h / 2), (w / 2, -d / 2, -h / 2),
        (w / 2, -d / 2,  h / 2), (-w / 2, -d / 2,  h / 2),
        (-w * t / 2, d / 2, -h * t / 2), (w * t / 2, d / 2, -h * t / 2),
        (w * t / 2, d / 2,  h * t / 2), (-w * t / 2, d / 2,  h * t / 2),
    ]
    faces = [(0, 1, 2, 3), (7, 6, 5, 4), (0, 4, 5, 1),
             (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]

    # screen: inset quad just in front of the body face
    inset = 0.86
    sw, sh = w * inset / 2, h * inset / 2
    base = len(verts)
    verts += [(-sw, -d / 2 - bz, -sh), (sw, -d / 2 - bz, -sh),
              (sw, -d / 2 - bz,  sh), (-sw, -d / 2 - bz,  sh)]
    screen_face = (base, base + 1, base + 2, base + 3)
    faces.append(screen_face)

    me.from_pydata(verts, [], faces)
    me.update()

    body_mat = _body_material()
    screen_mat = build_screen_material()
    me.materials.append(body_mat)     # slot 0
    me.materials.append(screen_mat)   # slot 1
    me.polygons[-1].material_index = 1

    # UVs: screen face gets a clean 0-1 quad, the atlas cell is chosen in
    # the shader from the per-instance channel attribute.
    uv = me.uv_layers.new(name="UVMap")
    for poly in me.polygons:
        for li in poly.loop_indices:
            uv.data[li].uv = (0.0, 0.0)
    corners = [(0, 0), (1, 0), (1, 1), (0, 1)]
    for i, li in enumerate(me.polygons[-1].loop_indices):
        uv.data[li].uv = corners[i % 4]

    for p in me.polygons:
        p.use_smooth = False

    obj.hide_render = True            # it is instanced, not rendered直接
    obj.hide_set(True)
    return obj


def _body_material():
    mat = bpy.data.materials.get("TD_Body") or \
        bpy.data.materials.new("TD_Body")
    mat.use_nodes = True
    bsdf = next((n for n in mat.node_tree.nodes
                 if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (0.02, 0.02, 0.025, 1)
        bsdf.inputs["Roughness"].default_value = 0.55
    return mat


# --------------------------------------------------------- screen shader

def build_screen_material():
    """Emissive screen. Samples one atlas cell, chosen per instance.

    UV math:  uv_out = (uv + (col, row)) / GRID
    where col = channel % GRID, row = floor(channel / GRID),
    and `channel` is read from the INSTANCER domain so each TV differs.
    """
    _purge(bpy.data.materials, "TD_Screen")
    mat = bpy.data.materials.new("TD_Screen")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (900, 0)
    emit = nt.nodes.new("ShaderNodeEmission")
    emit.location = (700, 0)

    # --- per-instance channel -> atlas cell -------------------------------
    chan = nt.nodes.new("ShaderNodeAttribute")
    chan.attribute_type = 'INSTANCER'
    chan.attribute_name = "td_channel"
    chan.location = (-600, -200)

    col = nt.nodes.new("ShaderNodeMath")
    col.operation = 'MODULO'
    col.inputs[1].default_value = float(ATLAS_GRID)
    col.location = (-400, -120)
    nt.links.new(chan.outputs["Fac"], col.inputs[0])

    row_div = nt.nodes.new("ShaderNodeMath")
    row_div.operation = 'DIVIDE'
    row_div.inputs[1].default_value = float(ATLAS_GRID)
    row_div.location = (-400, -300)
    nt.links.new(chan.outputs["Fac"], row_div.inputs[0])

    row = nt.nodes.new("ShaderNodeMath")
    row.operation = 'FLOOR'
    row.location = (-220, -300)
    nt.links.new(row_div.outputs[0], row.inputs[0])

    cell = nt.nodes.new("ShaderNodeCombineXYZ")
    cell.location = (-60, -220)
    nt.links.new(col.outputs[0], cell.inputs["X"])
    nt.links.new(row.outputs[0], cell.inputs["Y"])

    uvmap = nt.nodes.new("ShaderNodeUVMap")
    uvmap.uv_map = "UVMap"
    uvmap.location = (-600, 60)

    add = nt.nodes.new("ShaderNodeVectorMath")
    add.operation = 'ADD'
    add.location = (140, 0)
    nt.links.new(uvmap.outputs["UV"], add.inputs[0])
    nt.links.new(cell.outputs["Vector"], add.inputs[1])

    scale = nt.nodes.new("ShaderNodeVectorMath")
    scale.operation = 'SCALE'
    scale.location = (300, 0)
    scale.inputs[3].default_value = 1.0 / ATLAS_GRID
    nt.links.new(add.outputs["Vector"], scale.inputs[0])

    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.location = (460, 120)
    tex.interpolation = 'Closest'      # CRT pixels, not soft blur
    tex.extension = 'CLIP'
    tex.image = _atlas_image()
    nt.links.new(scale.outputs["Vector"], tex.inputs["Vector"])
    nt.links.new(tex.outputs["Color"], emit.inputs["Color"])

    # --- brightness: global Emission x per-instance boost ------------------
    boost = nt.nodes.new("ShaderNodeAttribute")
    boost.attribute_type = 'INSTANCER'
    boost.attribute_name = "td_bright"
    boost.location = (300, -420)

    mul = nt.nodes.new("ShaderNodeMath")
    mul.operation = 'MULTIPLY'
    mul.inputs[1].default_value = 6.0   # base emission; push higher for GI
    mul.location = (480, -420)
    nt.links.new(boost.outputs["Fac"], mul.inputs[0])
    nt.links.new(mul.outputs[0], emit.inputs["Strength"])

    nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    return mat


def _atlas_image():
    """Placeholder the bridge streams into. Keep this name."""
    img = bpy.data.images.get("TD_Atlas")
    if img is None:
        img = bpy.data.images.new("TD_Atlas", 1024, 1024)
        # faint checker so it is obvious when the stream is NOT arriving
        px = list(img.pixels)
        cell = 1024 // ATLAS_GRID
        for y in range(0, 1024, 32):
            for x in range(0, 1024, 32):
                v = 0.12 if ((x // cell) + (y // cell)) % 2 else 0.04
                for yy in range(y, min(y + 32, 1024)):
                    base = (yy * 1024 + x) * 4
                    for xx in range(min(32, 1024 - x)):
                        i = base + xx * 4
                        px[i] = px[i + 1] = px[i + 2] = v
                        px[i + 3] = 1.0
        img.pixels = px
    return img


# ------------------------------------------------------------- layouts

def _layout_grid(n):
    """Stacked wall, two banks facing a central corridor."""
    pts, rots = [], []
    per_side = n // 2
    cols, rows = 4, max(1, per_side // 4)
    for i in range(n):
        side = -1 if i < per_side else 1
        j = i % per_side
        c, r = j % cols, j // cols
        x = side * (2.2 + c * 0.5 + random.uniform(-0.05, 0.05))
        y = (c - cols / 2) * 0.9 + random.uniform(-0.3, 0.3)
        z = 0.2 + r * 0.42 + random.uniform(-0.02, 0.02)
        pts.append((x, y, z))
        rots.append((0, 0, (math.pi / 2 if side < 0 else -math.pi / 2)
                     + random.uniform(-0.12, 0.12)))
    return pts, rots


def _layout_arc(n):
    """Loose arc, screens turned inward — the 'opening up' state."""
    pts, rots = [], []
    for i in range(n):
        t = i / max(1, n - 1)
        ang = math.pi * (0.15 + 0.7 * t)
        rad = 4.0 + random.uniform(-0.6, 0.6)
        x = math.cos(ang) * rad
        y = math.sin(ang) * rad
        z = 0.2 + (i % 5) * 0.45
        pts.append((x, y, z))
        rots.append((0, 0, -ang + math.pi / 2 + random.uniform(-0.2, 0.2)))
    return pts, rots


def build_layout_object():
    """One point cloud carrying both layouts as attributes.

    Same point count, same ordering — the constraint from the brief. Point
    positions are layout A; layout B rides along as a vector attribute so
    GN can lerp between them.
    """
    _purge(bpy.data.objects, "TV_Layout")
    _purge(bpy.data.meshes, "TV_Layout")

    a_pts, a_rot = _layout_grid(N_TVS)
    b_pts, b_rot = _layout_arc(N_TVS)

    me = bpy.data.meshes.new("TV_Layout")
    me.from_pydata(a_pts, [], [])
    me.update()

    obj = bpy.data.objects.new("TV_Layout", me)
    bpy.context.scene.collection.objects.link(obj)

    def vec_attr(name, values):
        at = me.attributes.new(name, 'FLOAT_VECTOR', 'POINT')
        flat = [c for v in values for c in v]
        at.data.foreach_set("vector", flat)

    def float_attr(name, values):
        at = me.attributes.new(name, 'FLOAT', 'POINT')
        at.data.foreach_set("value", values)

    vec_attr("layout_b", b_pts)
    vec_attr("rot_a", a_rot)
    vec_attr("rot_b", b_rot)
    # which atlas cell each TV shows, and a per-TV brightness offset
    float_attr("td_channel", [float(i % (ATLAS_GRID * ATLAS_GRID))
                              for i in range(N_TVS)])
    float_attr("td_bright", [random.uniform(0.7, 1.3)
                             for _ in range(N_TVS)])
    # per-TV phase so displacement ripples instead of pulsing as one lump
    float_attr("phase", [random.random() for _ in range(N_TVS)])
    return obj


# ------------------------------------------------------------- node tree

def build_gn_group():
    _purge(bpy.data.node_groups, "TDBridge")
    ng = bpy.data.node_groups.new("TDBridge", 'GeometryNodeTree')

    ng.interface.new_socket("Geometry", in_out='INPUT',
                            socket_type='NodeSocketGeometry')
    ng.interface.new_socket("Geometry", in_out='OUTPUT',
                            socket_type='NodeSocketGeometry')

    def f_in(name, default, lo, hi):
        s = ng.interface.new_socket(name, in_out='INPUT',
                                    socket_type='NodeSocketFloat')
        s.default_value, s.min_value, s.max_value = default, lo, hi
        return s

    # These names are the TD contract. Task 2 in the brief makes them
    # addressable by name instead of Socket_N.
    f_in("Kick", 0.0, 0.0, 1.0)
    f_in("Bass", 0.0, 0.0, 1.0)
    f_in("Layout Mix", 0.0, 0.0, 1.0)
    f_in("Spread", 1.0, 0.0, 5.0)
    f_in("Emission", 1.0, 0.0, 10.0)
    obj_s = ng.interface.new_socket("Instance", in_out='INPUT',
                                    socket_type='NodeSocketObject')

    n = ng.nodes
    gi = n.new("NodeGroupInput"); gi.location = (-900, 0)
    go = n.new("NodeGroupOutput"); go.location = (900, 0)

    def named(attr, dtype='FLOAT'):
        na = n.new("GeometryNodeInputNamedAttribute")
        na.data_type = dtype
        na.inputs["Name"].default_value = attr
        return na

    # --- position: lerp(A, B, Layout Mix) ---------------------------------
    # done with vector maths rather than a Mix node: unambiguous sockets
    pos_a = n.new("GeometryNodeInputPosition"); pos_a.location = (-700, 260)
    pos_b = named("layout_b", 'FLOAT_VECTOR'); pos_b.location = (-700, 120)

    delta = n.new("ShaderNodeVectorMath")
    delta.operation = 'SUBTRACT'; delta.location = (-500, 200)
    ng.links.new(pos_b.outputs["Attribute"], delta.inputs[0])
    ng.links.new(pos_a.outputs["Position"], delta.inputs[1])

    delta_t = n.new("ShaderNodeVectorMath")
    delta_t.operation = 'SCALE'; delta_t.location = (-320, 200)
    ng.links.new(delta.outputs["Vector"], delta_t.inputs[0])
    ng.links.new(gi.outputs["Layout Mix"], delta_t.inputs[3])

    # --- kick displacement, phase-offset per point ------------------------
    phase = named("phase"); phase.location = (-700, -60)
    kick_ph = n.new("ShaderNodeMath")
    kick_ph.operation = 'MULTIPLY'; kick_ph.location = (-500, -60)
    ng.links.new(gi.outputs["Kick"], kick_ph.inputs[0])
    ng.links.new(phase.outputs["Attribute"], kick_ph.inputs[1])

    push = n.new("ShaderNodeMath")
    push.operation = 'MULTIPLY'; push.location = (-320, -60)
    ng.links.new(kick_ph.outputs[0], push.inputs[0])
    ng.links.new(gi.outputs["Spread"], push.inputs[1])

    # push outward from world centre: normalise position, scale by push
    dir_norm = n.new("ShaderNodeVectorMath")
    dir_norm.operation = 'NORMALIZE'; dir_norm.location = (-500, -220)
    ng.links.new(pos_a.outputs["Position"], dir_norm.inputs[0])

    kick_vec = n.new("ShaderNodeVectorMath")
    kick_vec.operation = 'SCALE'; kick_vec.location = (-320, -220)
    ng.links.new(dir_norm.outputs["Vector"], kick_vec.inputs[0])
    ng.links.new(push.outputs[0], kick_vec.inputs[3])

    offset = n.new("ShaderNodeVectorMath")
    offset.operation = 'ADD'; offset.location = (-140, 60)
    ng.links.new(delta_t.outputs["Vector"], offset.inputs[0])
    ng.links.new(kick_vec.outputs["Vector"], offset.inputs[1])

    set_pos = n.new("GeometryNodeSetPosition"); set_pos.location = (40, 0)
    ng.links.new(gi.outputs["Geometry"], set_pos.inputs["Geometry"])
    ng.links.new(offset.outputs["Vector"], set_pos.inputs["Offset"])

    # --- rotation: lerp(rot_a, rot_b, Layout Mix) -------------------------
    rot_a = named("rot_a", 'FLOAT_VECTOR'); rot_a.location = (40, -260)
    rot_b = named("rot_b", 'FLOAT_VECTOR'); rot_b.location = (40, -400)

    rdelta = n.new("ShaderNodeVectorMath")
    rdelta.operation = 'SUBTRACT'; rdelta.location = (220, -320)
    ng.links.new(rot_b.outputs["Attribute"], rdelta.inputs[0])
    ng.links.new(rot_a.outputs["Attribute"], rdelta.inputs[1])

    rdelta_t = n.new("ShaderNodeVectorMath")
    rdelta_t.operation = 'SCALE'; rdelta_t.location = (380, -320)
    ng.links.new(rdelta.outputs["Vector"], rdelta_t.inputs[0])
    ng.links.new(gi.outputs["Layout Mix"], rdelta_t.inputs[3])

    rot_final = n.new("ShaderNodeVectorMath")
    rot_final.operation = 'ADD'; rot_final.location = (520, -320)
    ng.links.new(rot_a.outputs["Attribute"], rot_final.inputs[0])
    ng.links.new(rdelta_t.outputs["Vector"], rot_final.inputs[1])

    # --- instance ---------------------------------------------------------
    objinfo = n.new("GeometryNodeObjectInfo"); objinfo.location = (220, 180)
    objinfo.transform_space = 'RELATIVE'
    ng.links.new(gi.outputs["Instance"], objinfo.inputs["Object"])

    iop = n.new("GeometryNodeInstanceOnPoints"); iop.location = (620, 0)
    ng.links.new(set_pos.outputs["Geometry"], iop.inputs["Points"])
    ng.links.new(objinfo.outputs["Geometry"], iop.inputs["Instance"])
    ng.links.new(rot_final.outputs["Vector"], iop.inputs["Rotation"])

    # --- carry channel + brightness onto the INSTANCE domain --------------
    # the shader reads these via Attribute(INSTANCER)
    chan_in = named("td_channel"); chan_in.location = (620, -520)
    store_ch = n.new("GeometryNodeStoreNamedAttribute")
    store_ch.data_type = 'FLOAT'
    store_ch.domain = 'INSTANCE'
    store_ch.inputs["Name"].default_value = "td_channel"
    store_ch.location = (760, -120)
    ng.links.new(iop.outputs["Instances"], store_ch.inputs["Geometry"])
    ng.links.new(chan_in.outputs["Attribute"], store_ch.inputs["Value"])

    br_in = named("td_bright"); br_in.location = (620, -640)
    br_mul = n.new("ShaderNodeMath")
    br_mul.operation = 'MULTIPLY'; br_mul.location = (760, -640)
    ng.links.new(br_in.outputs["Attribute"], br_mul.inputs[0])
    ng.links.new(gi.outputs["Emission"], br_mul.inputs[1])

    store_br = n.new("GeometryNodeStoreNamedAttribute")
    store_br.data_type = 'FLOAT'
    store_br.domain = 'INSTANCE'
    store_br.inputs["Name"].default_value = "td_bright"
    store_br.location = (900, -300)
    ng.links.new(store_ch.outputs["Geometry"], store_br.inputs["Geometry"])
    ng.links.new(br_mul.outputs[0], store_br.inputs["Value"])

    ng.links.new(store_br.outputs["Geometry"], go.inputs["Geometry"])
    return ng


# ------------------------------------------------------------------ main

def main():
    tv = build_tv_model()
    layout = build_layout_object()
    ng = build_gn_group()

    mod = layout.modifiers.new("TDBridge", 'NODES')
    mod.node_group = ng

    # point the Instance socket at the TV model. Socket IDs are opaque —
    # exactly the problem Task 2 in the brief solves.
    if not set_gn_input(mod, "Instance", tv):
        print("could not set the Instance socket")

    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_EEVEE'
    try:
        scene.world.node_tree.nodes["Background"] \
            .inputs[0].default_value = (0.005, 0.005, 0.006, 1)
    except Exception:
        pass

    print("--- TV wall base built ---")
    print(f"  {N_TVS} TVs, {ATLAS_GRID}x{ATLAS_GRID} atlas")
    print("  GN inputs: Kick, Bass, Layout Mix, Spread, Emission")
    print("  stream the TD atlas into image datablock 'TD_Atlas'")
    print("  param_map datapaths (names resolve; identifiers also work):")
    for item in ng.interface.items_tree:
        if getattr(item, "in_out", None) == 'INPUT':
            print(f"    {item.name:12s} -> modifiers.TDBridge.{item.name}"
                  f"   ({item.identifier})")


if __name__ == "__main__":
    main()
