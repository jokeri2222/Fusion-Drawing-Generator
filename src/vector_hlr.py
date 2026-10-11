"""
Exact hidden-line removal (HLRBRep_Algo) that keeps every edge as true vector
geometry.

Each projected edge comes back as an `Edge`: a list of sample points (what the
rest of the pipeline - bounding boxes, snapping, collision avoidance - works
with) that also carries `.segs`, the edge as real path segments:
    ('L', p0, p1)            straight line
    ('C', p0, c1, c2, p1)    cubic Bezier, control points taken from the
                             curve's own exact tangents (Hermite form)
Drawing those segments with moveTo/lineTo/curveTo makes a circle or spline a
genuinely smooth PDF curve at any zoom, and a PDF dash pattern runs along the
curve itself, so dashed curved edges are smooth too - no polygons anywhere.
"""
import math


class Edge(list):
    """Sample points of one projected edge plus its exact path segments."""
    segs = None
    is_line = False
    circle = None    # (cx, cy, radius, is_full_circle) for a circular edge


def _edge_segments(ad):
    from OCP.GeomAbs import GeomAbs_Line, GeomAbs_Circle, GeomAbs_Ellipse, GeomAbs_C1
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.TColStd import TColStd_Array1OfReal

    u0, u1 = ad.FirstParameter(), ad.LastParameter()
    typ = ad.GetType()
    if typ == GeomAbs_Line:
        a, b = ad.Value(u0), ad.Value(u1)
        return [('L', (a.X(), a.Y()), (b.X(), b.Y()))], True

    conic = typ in (GeomAbs_Circle, GeomAbs_Ellipse)
    try:
        n_iv = ad.NbIntervals(GeomAbs_C1)
        arr = TColStd_Array1OfReal(1, n_iv + 1)
        ad.Intervals(arr, GeomAbs_C1)
        knots = [arr.Value(i) for i in range(1, n_iv + 2)]
    except Exception:
        knots = [u0, u1]
    knots[0], knots[-1] = u0, u1

    full = u1 - u0
    segs = []
    for ka, kb in zip(knots, knots[1:]):
        if kb - ka <= 1e-12:
            continue
        if conic:
            # ~15 degrees of arc per Bezier piece
            k = max(2, math.ceil(((kb - ka) / (2 * math.pi)) * 24))
        else:
            k = 10
        for i in range(k):
            ua = ka + (kb - ka) * i / k
            ub = ka + (kb - ka) * (i + 1) / k
            pa, va, pb, vb = gp_Pnt(), gp_Vec(), gp_Pnt(), gp_Vec()
            ad.D1(ua, pa, va)
            ad.D1(ub, pb, vb)
            d = (ub - ua) / 3.0
            p0, p1 = (pa.X(), pa.Y()), (pb.X(), pb.Y())
            c1 = (p0[0] + va.X() * d, p0[1] + va.Y() * d)
            c2 = (p1[0] - vb.X() * d, p1[1] - vb.Y() * d)
            segs.append(('C', p0, c1, c2, p1))
    return segs, False


def compound_to_edges(compound):
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Circle

    out = []
    ex = TopExp_Explorer(compound, TopAbs_EDGE)
    while ex.More():
        ad = BRepAdaptor_Curve(TopoDS.Edge_s(ex.Current()))
        ex.Next()
        try:
            segs, is_line = _edge_segments(ad)
        except Exception:
            continue
        if not segs:
            continue
        if is_line:
            pts = [segs[0][1], segs[0][2]]
            if math.dist(*pts) < 1e-9:
                continue
        else:
            u0, u1 = ad.FirstParameter(), ad.LastParameter()
            pts = []
            for i in range(65):
                p = ad.Value(u0 + (u1 - u0) * i / 64)
                pts.append((p.X(), p.Y()))
        e = Edge(pts)
        e.segs, e.is_line = segs, is_line
        if ad.GetType() == GeomAbs_Circle:
            circ = ad.Circle()
            ctr = circ.Location()
            full = abs((ad.LastParameter() - ad.FirstParameter()) - 2 * math.pi) < 1e-6
            e.circle = (ctr.X(), ctr.Y(), circ.Radius(), full)
        out.append(e)
    return out


def _sig(e):
    m = e[len(e) // 2]
    ends = sorted([(round(e[0][0], 2), round(e[0][1], 2)), (round(e[-1][0], 2), round(e[-1][1], 2))])
    return (tuple(ends), round(m[0], 1), round(m[1], 1))


_CUT_CACHE = {}       # (shape, tool) -> shape with the tool carved out
_OVERLAP_CACHE = {}   # (shape, shape) -> do the two solids really overlap


def _surface_samples(shp, per_face=24):
    """Points spread over a solid's faces (a regular UV grid per face)."""
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    pts = []
    ex = TopExp_Explorer(shp, TopAbs_FACE)
    n = max(2, int(math.sqrt(per_face)))
    while ex.More():
        ad = BRepAdaptor_Surface(TopoDS.Face_s(ex.Current()))
        ex.Next()
        u0, u1, v0, v1 = ad.FirstUParameter(), ad.LastUParameter(), ad.FirstVParameter(), ad.LastVParameter()
        if not all(math.isfinite(t) and abs(t) < 1e6 for t in (u0, u1, v0, v1)):
            continue
        for i in range(n):
            for j in range(n):
                q = ad.Value(u0 + (u1 - u0) * (i + 0.5) / n, v0 + (v1 - v0) * (j + 0.5) / n)
                pts.append(q)
    return pts


def _overlaps(small, big, min_inside=3):
    """True if the solids interpenetrate: surface points of `small` lie
    strictly inside `big` (touching / coincident surfaces count as outside)."""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_IN

    clf = BRepClass3d_SolidClassifier(big)
    inside = 0
    for q in _surface_samples(small):
        clf.Perform(q, 1e-4)
        if clf.State() == TopAbs_IN:
            inside += 1
            if inside >= min_inside:
                return True
    return False


def _carve_overlaps(shapes):
    """The exact hidden-line engine mis-classifies edges where two solids
    interpenetrate (a screw shank is wider than its tapped hole, a bearing is
    pressed into its seat). Wherever two solids really overlap, the smaller
    one is subtracted from the larger, so they only touch afterwards."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBndLib import BRepBndLib
    from OCP.Bnd import Bnd_Box
    from OCP.TopTools import TopTools_ListOfShape

    def box(shp):
        b = Bnd_Box()
        BRepBndLib.Add_s(shp, b)
        return b

    def bbox_volume(b):
        x0, y0, z0, x1, y1, z1 = b.Get()
        return (x1 - x0) * (y1 - y0) * (z1 - z0)

    names = list(shapes)
    boxes = {n: box(shapes[n]) for n in names}
    carve = {n: [] for n in names}
    for i, a_ in enumerate(names):
        for b_ in names[i + 1:]:
            if boxes[a_].IsOut(boxes[b_]):
                continue
            small, big = (a_, b_) if bbox_volume(boxes[a_]) <= bbox_volume(boxes[b_]) else (b_, a_)
            key = (hash(shapes[small]), hash(shapes[big]))
            if key not in _OVERLAP_CACHE:
                # either one may stick into the other
                _OVERLAP_CACHE[key] = (_overlaps(shapes[small], shapes[big])
                                       or _overlaps(shapes[big], shapes[small]))
            if _OVERLAP_CACHE[key]:
                carve[big].append(small)
    out = dict(shapes)
    order = {n: i for i, n in enumerate(names)}
    for big, smalls in carve.items():
        # one tool at a time, in placement order, each step cached: the same
        # (shape, tool) cut on a later page is reused, and the expensive ones
        # (e.g. an O-ring in its groove) are only ever computed once
        cur = shapes[big]
        for sm in sorted(smalls, key=order.get):
            key = (hash(cur), hash(shapes[sm]))
            if key not in _CUT_CACHE:
                args, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
                args.Append(cur)
                tools.Append(shapes[sm])
                op = BRepAlgoAPI_Cut()
                op.SetArguments(args)
                op.SetTools(tools)
                op.SetRunParallel(True)
                op.Build()
                ok = op.IsDone() and not op.Shape().IsNull()
                _CUT_CACHE[key] = op.Shape() if ok else cur
            cur = _CUT_CACHE[key]
        out[big] = cur
    return out


def exact_views(shapes, direction, x_dir):
    """{name: (visible_edges, hidden_edges)} for OCC shapes in `shapes`
    ({name: TopoDS_Shape}), from ONE hidden-line pass over all of them
    together (real mutual occlusion), after overlapping solids are carved
    apart (see `_carve_overlaps`)."""
    shapes = _carve_overlaps(shapes)
    from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
    from OCP.HLRAlgo import HLRAlgo_Projector
    from OCP.gp import gp_Ax2, gp_Pnt, gp_Dir

    algo = HLRBRep_Algo()
    for shp in shapes.values():
        algo.Add(shp)
    algo.Projector(HLRAlgo_Projector(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*direction), gp_Dir(*x_dir))))
    algo.Update()
    algo.Hide()
    to_shape = HLRBRep_HLRToShape(algo)

    def collect(shp, fns):
        out = []
        for fn in fns:
            comp = fn(shp)
            if comp is not None and not comp.IsNull():
                out.extend(compound_to_edges(comp))
        return out

    result = {}
    for name, shp in shapes.items():
        visible = collect(shp, (to_shape.VCompound, to_shape.Rg1LineVCompound,
                                to_shape.RgNLineVCompound, to_shape.OutLineVCompound))
        hidden = collect(shp, (to_shape.HCompound, to_shape.Rg1LineHCompound,
                               to_shape.RgNLineHCompound, to_shape.OutLineHCompound))
        # the same edge can be reported as visible and hidden (tangent
        # surfaces) - keep it once, as visible; and drop plain duplicates
        seen = {_sig(e) for e in visible}
        hidden = [e for e in hidden if _sig(e) not in seen]
        result[name] = (_unique(visible), _unique(hidden))
    return result


def _unique(edges):
    seen, out = set(), []
    for e in edges:
        k = _sig(e) + (len(e),)
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


# ---------------------------------------------------------------- drawing

def reverse_segs(segs):
    out = []
    for s in reversed(segs):
        out.append((s[0],) + tuple(reversed(s[1:])))
    return out


def chain_edges(edges, tol=0.002):
    """Join curved edges that share an end point into one continuous path
    (segment lists), so a dash pattern runs unbroken along the whole curve.
    Returns a list of seg lists."""
    key = lambda p: (round(p[0] / tol), round(p[1] / tol))
    ends = {}
    for i, e in enumerate(edges):
        ends.setdefault(key(e.segs[0][1]), []).append(i)
        ends.setdefault(key(e.segs[-1][-1]), []).append(i)
    used = [False] * len(edges)

    def take(k):
        # only join where exactly two edge ends meet - at a junction of
        # three or more curves there is no single continuation
        if len(ends.get(k, ())) != 2:
            return None
        for j in ends[k]:
            if not used[j]:
                return j
        return None

    chains = []
    for i, e in enumerate(edges):
        if used[i]:
            continue
        used[i] = True
        chain = list(e.segs)
        while True:
            j = take(key(chain[-1][-1]))
            if j is None:
                break
            used[j] = True
            nxt = edges[j].segs
            chain.extend(nxt if key(nxt[0][1]) == key(chain[-1][-1]) else reverse_segs(nxt))
        while True:
            j = take(key(chain[0][1]))
            if j is None:
                break
            used[j] = True
            nxt = edges[j].segs
            chain[:0] = (nxt if key(nxt[-1][-1]) == key(chain[0][1]) else reverse_segs(nxt))
        chains.append(chain)
    return chains


def canonical_segs(segs):
    """Reorder a path so its start point and direction depend only on its
    shape (closed: leftmost point, counter-clockwise; open: lower-left end
    first), so identical or translated copies dash identically."""
    key = lambda q: (round(q[0], 3), round(q[1], 3))
    starts = [s[1] for s in segs]
    if math.dist(segs[0][1], segs[-1][-1]) < 1e-3 and len(segs) > 1:
        area = sum(starts[i][0] * starts[(i + 1) % len(starts)][1]
                   - starts[(i + 1) % len(starts)][0] * starts[i][1] for i in range(len(starts)))
        if area < 0:
            segs = reverse_segs(segs)
            starts = [s[1] for s in segs]
        k = min(range(len(starts)), key=lambda i: key(starts[i]))
        return segs[k:] + segs[:k]
    return reverse_segs(segs) if key(segs[-1][-1]) < key(segs[0][1]) else segs


def trace_segs(path, segs, to_page):
    """Append `segs` (view coordinates) to a reportlab path in page space."""
    first = to_page(segs[0][1])
    path.moveTo(*first)
    for s in segs:
        if s[0] == 'L':
            path.lineTo(*to_page(s[2]))
        else:
            c1, c2, p = to_page(s[2]), to_page(s[3]), to_page(s[4])
            path.curveTo(c1[0], c1[1], c2[0], c2[1], p[0], p[1])


def flatten(edges, per_seg=64):
    """Dense points along the edges' exact segments, for proximity tests."""
    pts = []
    for e in edges:
        for sg in e.segs:
            if sg[0] == 'L':
                (x0, y0), (x1, y1) = sg[1], sg[2]
                n = max(2, int(math.dist(sg[1], sg[2]) / 0.05))
                pts.extend((x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(n + 1))
            else:
                p0, c1, c2, p1 = sg[1:]
                for i in range(per_seg + 1):
                    t = i / per_seg
                    a, b, c_, d = (1 - t) ** 3, 3 * t * (1 - t) ** 2, 3 * t * t * (1 - t), t ** 3
                    pts.append((a * p0[0] + b * c1[0] + c_ * c2[0] + d * p1[0],
                                a * p0[1] + b * c1[1] + c_ * c2[1] + d * p1[1]))
    return pts


class PointIndex:
    """Grid index over points: `near(p, tol)` is True if any is within tol."""

    def __init__(self, pts, cell=0.25):
        self.cell = cell
        self.grid = {}
        for p in pts:
            self.grid.setdefault((int(p[0] // cell), int(p[1] // cell)), []).append(p)

    def add(self, pts):
        for p in pts:
            self.grid.setdefault((int(p[0] // self.cell), int(p[1] // self.cell)), []).append(p)

    def near(self, p, tol):
        cx, cy = int(p[0] // self.cell), int(p[1] // self.cell)
        r = int(tol // self.cell) + 1
        for i in range(cx - r, cx + r + 1):
            for j in range(cy - r, cy + r + 1):
                for q in self.grid.get((i, j), ()):
                    if abs(q[0] - p[0]) <= tol and abs(q[1] - p[1]) <= tol and math.dist(p, q) <= tol:
                        return True
        return False


def edge_mid(e):
    """Point halfway along the edge's own path."""
    sg = e.segs[len(e.segs) // 2]
    if sg[0] == 'L':
        return ((sg[1][0] + sg[2][0]) / 2, (sg[1][1] + sg[2][1]) / 2)
    p0, c1, c2, p1 = sg[1:]
    return tuple(0.125 * p0[k] + 0.375 * c1[k] + 0.375 * c2[k] + 0.125 * p1[k] for k in (0, 1))


def edge_length(e):
    """Approximate path length of an edge (view units)."""
    pts = flatten([e], per_seg=8)
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)) if len(e.segs) == 1 and e.is_line else         sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1) if (i + 1) % 9)


def drop_covered(edges, index, tol=0.05, share=0.9):
    """Edges not already covered by `index` (which then also learns them).
    An edge lying (nearly) on top of geometry drawn before it would be a
    second dashed line over the first with a different dash phase, which
    reads as a continuous line - so only the first one is kept."""
    kept = []
    for e in edges:
        pts = flatten([e], per_seg=24)
        if pts and sum(1 for p in pts if index.near(p, tol)) >= share * len(pts):
            continue
        index.add(pts)
        kept.append(e)
    return kept
