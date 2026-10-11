"""
Re-orient a part so its dominant geometry lines up with the world axes.

A part exported from an assembly sits at whatever angle it had there, so its
bores, flats and edges are generally not parallel to X/Y/Z - and then none of
the orthographic views shows them square-on and no dimension can run along
them. This finds the part's own dominant directions (plane normals and
cylinder/cone/torus axes, weighted by face area), builds an orthonormal frame
from the two strongest perpendicular ones, and rotates the part about its
bounding-box centre onto the nearest world axes. A part that is already
aligned comes out unchanged (identity rotation).

Nothing here depends on a part's name or size.
"""
import itertools
import math

from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepBndLib import BRepBndLib
from OCP.Bnd import Bnd_Box
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Torus
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Trsf

# two face directions closer than this (degrees) count as the same direction
DIRECTION_CLUSTER_DEG = 0.5
# a second direction must be this close to perpendicular (|cos|) to the first
PERPENDICULAR_COS_LIMIT = 0.05
# a rotation smaller than this (degrees) is treated as "already aligned"
NEGLIGIBLE_ROTATION_DEG = 0.05


def _unit(v):
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / n for c in v)


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _canonical(v):
    """Sign-normalise so d and -d are the same direction."""
    for c in v:
        if abs(c) > 1e-9:
            return v if c > 0 else tuple(-x for x in v)
    return v


def face_directions(shape):
    """[(unit_direction, area)] for every plane / cylinder / cone / torus face
    (its normal or axis)."""
    out = []
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        face = TopoDS.Face_s(ex.Current())
        ex.Next()
        ad = BRepAdaptor_Surface(face)
        t = ad.GetType()
        if t == GeomAbs_Plane:
            d = ad.Plane().Axis().Direction()
        elif t == GeomAbs_Cylinder:
            d = ad.Cylinder().Axis().Direction()
        elif t == GeomAbs_Cone:
            d = ad.Cone().Axis().Direction()
        elif t == GeomAbs_Torus:
            d = ad.Torus().Axis().Direction()
        else:
            continue
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        area = props.Mass()
        if area > 0:
            out.append((_canonical((d.X(), d.Y(), d.Z())), area))
    return out


def cluster_directions(items):
    """Merge near-parallel directions: [(direction, total_area)], heaviest first."""
    cos_limit = math.cos(math.radians(DIRECTION_CLUSTER_DEG))
    clusters = []   # [direction_sum_vector, total_area]
    for d, a in items:
        for cl in clusters:
            ref = _unit(cl[0])
            if abs(_dot(ref, d)) >= cos_limit:
                sgn = 1 if _dot(ref, d) >= 0 else -1
                cl[0] = tuple(cl[0][i] + sgn * d[i] * a for i in range(3))
                cl[1] += a
                break
        else:
            clusters.append([tuple(c * a for c in d), a])
    res = [(_canonical(_unit(v)), a) for v, a in clusters]
    return sorted(res, key=lambda t: -t[1])


def dominant_frame(shape):
    """Orthonormal (f1, f2, f3): the strongest direction, the strongest one
    perpendicular to it, and their cross product. None if the shape has no
    usable faces."""
    clusters = cluster_directions(face_directions(shape))
    if not clusters:
        return None
    f1 = clusters[0][0]
    f2 = None
    for d, _a in clusters[1:]:
        if abs(_dot(f1, d)) < PERPENDICULAR_COS_LIMIT:
            f2 = d
            break
    if f2 is None:
        f2 = min(((1, 0, 0), (0, 1, 0), (0, 0, 1)), key=lambda a: abs(_dot(f1, a)))
    f2 = _unit(tuple(f2[i] - _dot(f2, f1) * f1[i] for i in range(3)))
    f3 = _cross(f1, f2)
    return f1, f2, f3


def alignment_rotation(frame):
    """3x3 rotation (row-major nested tuples) taking the frame's vectors onto
    world axes, picking - of the 24 proper axis assignments - the one closest
    to no rotation at all."""
    best, best_trace = None, -1e9
    axes = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((1, -1), repeat=3):
            # R = sum_i  axis[perm[i]] * sign_i * frame[i]^T
            rows = [[0.0] * 3 for _ in range(3)]
            for i in range(3):
                for r in range(3):
                    for k in range(3):
                        rows[r][k] += axes[perm[i]][r] * signs[i] * frame[i][k]
            det = (rows[0][0] * (rows[1][1] * rows[2][2] - rows[1][2] * rows[2][1])
                   - rows[0][1] * (rows[1][0] * rows[2][2] - rows[1][2] * rows[2][0])
                   + rows[0][2] * (rows[1][0] * rows[2][1] - rows[1][1] * rows[2][0]))
            if det < 0.99:
                continue
            tr = rows[0][0] + rows[1][1] + rows[2][2]
            if tr > best_trace:
                best, best_trace = rows, tr
    return tuple(tuple(r) for r in best)


def rotation_angle_deg(rot):
    tr = rot[0][0] + rot[1][1] + rot[2][2]
    return math.degrees(math.acos(max(-1.0, min(1.0, (tr - 1) / 2))))


def _bbox_centre(shape):
    b = Bnd_Box()
    BRepBndLib.Add_s(shape, b)
    x0, y0, z0, x1, y1, z1 = b.Get()
    return ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)


def oriented_shape(shape):
    """(rotated TopoDS shape, 3x3 rotation, angle in degrees) for `shape`
    (a TopoDS_Shape or cadquery Shape)."""
    raw = shape.wrapped if hasattr(shape, 'wrapped') else shape
    frame = dominant_frame(raw)
    identity = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    if frame is None:
        return raw, identity, 0.0
    rot = alignment_rotation(frame)
    angle = rotation_angle_deg(rot)
    if angle < NEGLIGIBLE_ROTATION_DEG:
        return raw, rot, 0.0
    cx, cy, cz = _bbox_centre(raw)
    # rotate about the bounding-box centre: p' = R (p - c) + c
    t = gp_Trsf()
    t.SetValues(rot[0][0], rot[0][1], rot[0][2], cx - (rot[0][0] * cx + rot[0][1] * cy + rot[0][2] * cz),
                rot[1][0], rot[1][1], rot[1][2], cy - (rot[1][0] * cx + rot[1][1] * cy + rot[1][2] * cz),
                rot[2][0], rot[2][1], rot[2][2], cz - (rot[2][0] * cx + rot[2][1] * cy + rot[2][2] * cz))
    return BRepBuilderAPI_Transform(raw, t, True).Shape(), rot, angle


def export_oriented_step(shape, path):
    """Write the oriented part to a STEP file; returns the rotation angle."""
    import cadquery as cq
    rotated, _rot, angle = oriented_shape(shape)
    cq.Shape.cast(rotated).exportStep(path)
    return angle
