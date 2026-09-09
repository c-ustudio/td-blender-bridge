"""
layout_gen.py — arrangement + cable generation for the CRT sculpture.

Two halves:

  1. Pure Python core (no bpy). BSP layouts, Poisson-disk scatter,
     space colonisation cables, pipe-model radii. Testable anywhere,
     which is why it is written this way.
  2. A thin Blender adapter at the bottom, imported only if bpy exists.

Run standalone to sanity-check:      python layout_gen.py
Run inside Blender to build a scene:  see build_scene() at the bottom.

Layout contract (from the brief): every layout returns exactly N points in
STABLE ORDER, so GN can interpolate between any two of them. Do not
"optimise" this by returning variable-length lists.
"""

import math
import random

# ============================================================ pure core

# ---------------------------------------------------------------- BSP

def bsp_split(x, y, w, h, min_w, min_h, rng, depth=0, max_depth=12):
    """Recursively split a rectangle into TV-sized cells.

    Returns a list of (x, y, w, h). Split axis is biased by aspect ratio
    so the result reads as stacked columns rather than a uniform quilt.
    """
    can_w = w >= min_w * 2
    can_h = h >= min_h * 2
    if depth >= max_depth or not (can_w or can_h):
        return [(x, y, w, h)]

    # stop early sometimes, so big screens survive amongst small ones
    if depth > 2 and rng.random() < 0.18:
        return [(x, y, w, h)]

    if can_w and can_h:
        # bias toward splitting the longer axis, but not deterministically
        p_vert = w / (w + h)
        vertical = rng.random() < p_vert
    else:
        vertical = can_w

    # split ratio near the middle, jittered — avoids a metronomic look
    t = rng.uniform(0.35, 0.65)

    if vertical:
        cut = max(min_w, min(w - min_w, w * t))
        return (bsp_split(x, y, cut, h, min_w, min_h, rng, depth + 1, max_depth)
                + bsp_split(x + cut, y, w - cut, h, min_w, min_h, rng,
                            depth + 1, max_depth))
    cut = max(min_h, min(h - min_h, h * t))
    return (bsp_split(x, y, w, cut, min_w, min_h, rng, depth + 1, max_depth)
            + bsp_split(x, y + cut, w, h - cut, min_w, min_h, rng,
                        depth + 1, max_depth))


def bsp_wall(n_target, width=6.0, height=3.0, seed=0, margin=0.02):
    """A wall of n_target TVs. Cells are retried until the count matches.

    Returns list of dicts: {pos:(x,y), size:(w,h)} in a stable order
    (sorted by column then row, so two layouts correspond sensibly).
    """
    rng = random.Random(seed)
    best = None
    for attempt in range(400):
        min_w = width / math.sqrt(n_target) * rng.uniform(0.55, 0.8)
        min_h = height / math.sqrt(n_target) * rng.uniform(0.55, 0.8)
        cells = bsp_split(0, 0, width, height, min_w, min_h,
                          random.Random(seed * 1000 + attempt))
        if best is None or abs(len(cells) - n_target) < abs(len(best) - n_target):
            best = cells
        if len(cells) == n_target:
            break

    cells = best
    # force exact count: split the largest, or drop the smallest
    while len(cells) > n_target:
        cells.sort(key=lambda c: c[2] * c[3])
        cells.pop(0)
    while len(cells) < n_target:
        cells.sort(key=lambda c: c[2] * c[3], reverse=True)
        x, y, w, h = cells.pop(0)
        if w > h:
            cells += [(x, y, w / 2, h), (x + w / 2, y, w / 2, h)]
        else:
            cells += [(x, y, w, h / 2), (x, y + h / 2, w, h / 2)]

    cells.sort(key=lambda c: (round(c[0], 3), round(c[1], 3)))
    out = []
    for (x, y, w, h) in cells:
        out.append({
            "pos": (x + w / 2 - width / 2, y + h / 2),
            "size": (max(0.01, w - margin), max(0.01, h - margin)),
        })
    return out


# ------------------------------------------------------- Poisson disk

def poisson_disk(width, height, r, seed=0, k=30):
    """Bridson 2007. Blue-noise points in a width x height rectangle.

    Grid cell size r/sqrt(2) guarantees at most one sample per cell, which
    is what makes the neighbour check O(1) and the whole thing O(n).
    """
    rng = random.Random(seed)
    cell = r / math.sqrt(2)
    gw, gh = int(math.ceil(width / cell)), int(math.ceil(height / cell))
    grid = [[None] * gh for _ in range(gw)]
    samples, active = [], []

    def emit(p):
        samples.append(p)
        active.append(p)
        grid[int(p[0] / cell)][int(p[1] / cell)] = p

    emit((rng.uniform(0, width), rng.uniform(0, height)))

    while active:
        i = rng.randrange(len(active))
        px, py = active[i]
        placed = False
        for _ in range(k):
            ang = rng.uniform(0, math.tau)
            rad = rng.uniform(r, 2 * r)          # the annulus, not the disk
            q = (px + math.cos(ang) * rad, py + math.sin(ang) * rad)
            if not (0 <= q[0] < width and 0 <= q[1] < height):
                continue
            gx, gy = int(q[0] / cell), int(q[1] / cell)
            ok = True
            for ax in range(max(0, gx - 2), min(gw, gx + 3)):
                for ay in range(max(0, gy - 2), min(gh, gy + 3)):
                    s = grid[ax][ay]
                    if s and (s[0] - q[0]) ** 2 + (s[1] - q[1]) ** 2 < r * r:
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                emit(q)
                placed = True
                break
        if not placed:
            active.pop(i)
    return samples


def poisson_scatter(n_target, width=10.0, depth=8.0, seed=0):
    """Exactly n_target scattered positions. Radius is solved by bisection
    so the count lands on target rather than wherever it happens to fall."""
    lo, hi = 0.05, max(width, depth)
    pts = []
    for _ in range(40):
        mid = (lo + hi) / 2
        pts = poisson_disk(width, depth, mid, seed=seed)
        if len(pts) > n_target:
            lo = mid
        else:
            hi = mid
        if len(pts) == n_target:
            break
    pts = pts[:n_target]
    while len(pts) < n_target:                    # pad deterministically
        rng = random.Random(seed + len(pts))
        pts.append((rng.uniform(0, width), rng.uniform(0, depth)))
    pts.sort(key=lambda p: (round(p[0], 3), round(p[1], 3)))
    return [(x - width / 2, y - depth / 2) for (x, y) in pts]


# -------------------------------------------------- space colonisation

def space_colonization(attractors, roots, di=2.5, dk=0.4, D=0.18,
                       tropism=(0, 0, -0.35), max_iter=400):
    """Runions/Lane/Prusinkiewicz 2007.

    attractors : [(x,y,z)]  - here, the TVs that want feeding
    roots      : [(x,y,z)]  - junction box / floor entry points
    di         : influence radius. SMALL = wiggly. Wiggly is good for cable.
    dk         : kill distance; attractor consumed when a node gets this close
    D          : segment length
    tropism    : bias vector g, added before normalising. Points down = sag.

    Returns (nodes, parents) where parents[i] indexes nodes, -1 for roots.
    """
    nodes = list(roots)
    parents = [-1] * len(roots)
    live = list(attractors)

    def d2(a, b):
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)

    for _ in range(max_iter):
        if not live:
            break

        # step 1: each attractor influences its single nearest node
        influence = {}
        for s in live:
            best_i, best_d = None, di * di
            for i, v in enumerate(nodes):
                dd = d2(s, v)
                if dd < best_d:
                    best_i, best_d = i, dd
            if best_i is not None:
                influence.setdefault(best_i, []).append(s)

        if not influence:
            # Nothing in range — the classic stall. Rather than dying,
            # grow the nearest node toward the nearest attractor until
            # contact is made. Makes root placement forgiving.
            best = None
            for si, s in enumerate(live):
                for i, v in enumerate(nodes):
                    dd = d2(s, v)
                    if best is None or dd < best[0]:
                        best = (dd, i, s)
            if best is None:
                break
            influence = {best[1]: [best[2]]}

        # step 2/3: sum NORMALISED directions, step D, add node
        new_nodes = []
        for i, pts in influence.items():
            v = nodes[i]
            nx = ny = nz = 0.0
            for s in pts:
                dx, dy, dz = s[0] - v[0], s[1] - v[1], s[2] - v[2]
                L = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
                nx += dx / L; ny += dy / L; nz += dz / L
            nx += tropism[0]; ny += tropism[1]; nz += tropism[2]
            L = math.sqrt(nx * nx + ny * ny + nz * nz)
            if L < 1e-9:
                continue
            new_nodes.append((
                (v[0] + D * nx / L, v[1] + D * ny / L, v[2] + D * nz / L), i))

        if not new_nodes:
            break
        for pos, parent in new_nodes:
            nodes.append(pos)
            parents.append(parent)

        # step 4: consume attractors that a node has now reached
        live = [s for s in live
                if all(d2(s, v) > dk * dk for v in nodes)]

    return nodes, parents


def pipe_radii(nodes, parents, r0=0.006, n=2.4):
    """Pipe model, basipetal: r^n = r1^n + r2^n. Thick trunk, thin leaves —
    which is exactly how a real cable loom behaves."""
    children = [[] for _ in nodes]
    for i, p in enumerate(parents):
        if p >= 0:
            children[p].append(i)

    radii = [0.0] * len(nodes)
    order = sorted(range(len(nodes)), key=lambda i: -i)   # tips first
    for i in order:
        if not children[i]:
            radii[i] = r0
        else:
            radii[i] = sum(radii[c] ** n for c in children[i]) ** (1.0 / n)
    return radii


# ------------------------------------------------------------ layouts

def tower_stacks(n_tvs, n_towers=11, area=(9.0, 7.0), aisle=1.6,
                 seed=0, tv_w=0.5, tv_h=0.42):
    """Freestanding towers of stacked CRTs — the reference-video look.

    Footprints come from Poisson disk (even spacing, no clumps), then TVs
    are dealt out unevenly so some towers loom and others are knee-high.
    An aisle is carved down the middle so a camera can travel through.

    Returns n_tvs entries {pos, size, rot_z} in stable order.
    """
    rng = random.Random(seed)
    W, Dp = area

    # tower footprints, blue noise, then pushed clear of the central aisle
    r = math.sqrt(W * Dp / max(1, n_towers)) * 0.62
    feet = poisson_disk(W, Dp, r, seed=seed)
    feet = [(x - W / 2, y - Dp / 2) for (x, y) in feet]
    feet = [(x + math.copysign(aisle, x if abs(x) > 1e-6 else 1.0), y)
            for (x, y) in feet]
    # Balance the two sides explicitly. Sampling the whole set at random
    # reliably lands lopsided, which kills the tunnel read.
    left = [p for p in feet if p[0] < 0]
    right = [p for p in feet if p[0] >= 0]
    nl = n_towers // 2
    nr = n_towers - nl
    feet = (rng.sample(left, min(nl, len(left)))
            + rng.sample(right, min(nr, len(right))))
    feet.sort(key=lambda p: (round(p[0], 3), round(p[1], 3)))
    feet = feet or [(-aisle, 0.0), (aisle, 0.0)]

    # deal TVs to towers: uneven, but every tower gets at least two
    counts = [2] * len(feet)
    for _ in range(n_tvs - 2 * len(feet)):
        w = [1.0 / (1 + c) ** 0.5 for c in counts]      # favour short towers
        tot = sum(w)
        pick, acc = 0, rng.uniform(0, tot)
        for i, ww in enumerate(w):
            acc -= ww
            if acc <= 0:
                pick = i
                break
        counts[pick] += 1

    out = []
    for (fx, fy), n in zip(feet, counts):
        z = 0.0
        facing = math.atan2(-fx, 0.001) + rng.uniform(-0.25, 0.25)
        for k in range(n):
            sw = tv_w * rng.uniform(0.72, 1.18)
            sh = tv_h * rng.uniform(0.78, 1.15)
            z += sh / 2
            out.append({
                "pos": (fx + rng.uniform(-0.05, 0.05),
                        fy + rng.uniform(-0.05, 0.05),
                        z),
                "size": (sw, sh),
                # each unit twisted slightly — stacked by hand, not machined
                "rot_z": facing + rng.uniform(-0.16, 0.16),
            })
            z += sh / 2 + 0.012
    return out[:n_tvs]


def make_layouts(n_tvs=64, seed=0):
    """Returns a dict of named layouts, each a list of n_tvs entries:
    {pos:(x,y,z), size:(w,h), rot_z:float}. Same count, same ordering."""
    layouts = {}

    # A: two banks facing a corridor
    half = n_tvs // 2
    left = bsp_wall(half, width=6.0, height=3.0, seed=seed + 1)
    right = bsp_wall(n_tvs - half, width=6.0, height=3.0, seed=seed + 2)
    pts = []
    for c in left:
        pts.append({"pos": (-2.4, c["pos"][0], c["pos"][1]),
                    "size": c["size"], "rot_z": math.pi / 2})
    for c in right:
        pts.append({"pos": (2.4, c["pos"][0], c["pos"][1]),
                    "size": c["size"], "rot_z": -math.pi / 2})
    layouts["corridor"] = pts

    # B: one dense wall, seen head-on
    wall = bsp_wall(n_tvs, width=8.0, height=4.0, seed=seed + 3)
    layouts["wall"] = [{"pos": (c["pos"][0], 3.0, c["pos"][1]),
                        "size": c["size"], "rot_z": 0.0} for c in wall]

    # C: scattered on the floor, blue noise
    rng = random.Random(seed + 4)
    scat = poisson_scatter(n_tvs, width=11.0, depth=9.0, seed=seed + 4)
    layouts["scatter"] = [
        {"pos": (x, y, 0.25 + (i % 4) * 0.42),
         "size": wall[i]["size"],                    # keep sizes consistent
         "rot_z": rng.uniform(-math.pi, math.pi)}
        for i, (x, y) in enumerate(scat)]

    # D: towers — the reference-video arrangement
    layouts["towers"] = tower_stacks(n_tvs, n_towers=11, seed=seed + 5)
    layouts["towers_tall"] = tower_stacks(n_tvs, n_towers=7, aisle=1.9,
                                          seed=seed + 6)

    return layouts


def cables_for(layout, roots=None, di=1.2, dk=0.15, D=0.10, **kw):
    """Grow one cable network toward a layout's TVs.

    Grow ONCE against a reference layout, then let endpoints follow their
    TVs as arrangements change — topology stays fixed, which is both
    interpolatable and physically honest.
    """
    attractors = [p["pos"] for p in layout]
    if roots is None:
        roots = [(0.0, -4.0, 0.02)]
    nodes, parents = space_colonization(attractors, roots,
                                        di=di, dk=dk, D=D,
                                        max_iter=kw.pop("max_iter", 900), **kw)
    return nodes, parents, pipe_radii(nodes, parents)


# ================================================================ bpy

def build_scene(n_tvs=64, seed=0):
    """Blender-side: writes layouts as attributes on one point cloud and
    cables as a curve object. Call from Blender only."""
    import bpy

    layouts = make_layouts(n_tvs, seed)
    names = list(layouts.keys())
    base = layouts[names[0]]

    me = bpy.data.meshes.new("TV_Layout")
    me.from_pydata([p["pos"] for p in base], [], [])
    me.update()
    obj = bpy.data.objects.new("TV_Layout", me)
    bpy.context.scene.collection.objects.link(obj)

    def vattr(name, vals):
        a = me.attributes.new(name, 'FLOAT_VECTOR', 'POINT')
        a.data.foreach_set("vector", [c for v in vals for c in v])

    def fattr(name, vals):
        a = me.attributes.new(name, 'FLOAT', 'POINT')
        a.data.foreach_set("value", list(vals))

    for li, nm in enumerate(names):
        L = layouts[nm]
        vattr(f"layout_{li}", [p["pos"] for p in L])
        vattr(f"rot_{li}", [(0.0, 0.0, p["rot_z"]) for p in L])
        vattr(f"size_{li}", [(p["size"][0], p["size"][1], 1.0) for p in L])

    rng = random.Random(seed)
    fattr("td_channel", [float(i % 64) for i in range(n_tvs)])
    fattr("td_bright", [rng.uniform(0.7, 1.3) for _ in range(n_tvs)])
    fattr("phase", [rng.random() for _ in range(n_tvs)])

    # cables, grown once against the reference layout
    nodes, parents, radii = cables_for(base)
    cu = bpy.data.curves.new("TV_Cables", 'CURVE')
    cu.dimensions = '3D'
    for i, p in enumerate(parents):
        if p < 0:
            continue
        sp = cu.splines.new('POLY')
        sp.points.add(1)
        sp.points[0].co = (*nodes[p], 1.0)
        sp.points[1].co = (*nodes[i], 1.0)
        sp.points[0].radius = radii[p] * 100
        sp.points[1].radius = radii[i] * 100
    cu.bevel_depth = 0.01
    cobj = bpy.data.objects.new("TV_Cables", cu)
    bpy.context.scene.collection.objects.link(cobj)

    print(f"layouts: {names}")
    print(f"cable nodes: {len(nodes)}")
    return obj, cobj


# =============================================================== check

if __name__ == "__main__":
    N = 64
    lay = make_layouts(N, seed=3)
    print(f"{'layout':10s} {'count':>6s}   bounds")
    for nm, L in lay.items():
        xs = [p["pos"][0] for p in L]
        zs = [p["pos"][2] for p in L]
        print(f"{nm:10s} {len(L):6d}   "
              f"x[{min(xs):6.2f},{max(xs):6.2f}] z[{min(zs):5.2f},{max(zs):5.2f}]")
        assert len(L) == N, "layout point counts must match — see brief"

    nodes, parents, radii = cables_for(lay["corridor"])
    print(f"\ncables: {len(nodes)} nodes, "
          f"trunk r={max(radii):.4f}, leaf r={min(radii):.4f}")
