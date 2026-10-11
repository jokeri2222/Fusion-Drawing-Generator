"""
Load a STEP file (one per Fusion body, produced by
scripts/Export Project For Drawings/) and expose its geometry in a form
the rest of the pipeline can use: bounding box, cylindrical faces (for
matching holes/threads from project_data.json), and edges (for 2D
projection later in drawing_generator.py).

Uses cadquery (which bundles OCP/OpenCascade bindings) rather than raw
pythonocc-core, since cadquery already wraps STEP import and topology
traversal in a friendlier API.
"""

import math

import cadquery as cq
from cadquery.occ_impl.shapes import Face
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Torus
from OCP.GeomLProp import GeomLProp_SLProps
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_VERTEX
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS

# Minimum fillet radius worth reporting - anything smaller is almost always
# a cosmetic/print-friendliness micro-blend rather than a machining
# feature someone would actually call out on a drawing.
MIN_REPORTED_FILLET_RADIUS_MM = 0.3

# A cylindrical face whose angular (U) span is well short of a full circle
# is one rounded end of a slot, not a round hole - a true drilled/bored
# hole's wall wraps all the way around (~360 deg; a little short of that is
# just numerical/trim slack). Anything below this is treated as a slot end.
FULL_CIRCLE_THRESHOLD_DEG = 300

# Pocket-floor heuristic (see `pocket_faces`): skip slivers left over from
# fillet/chamfer transition faces.
MIN_REPORTED_POCKET_AREA_MM2 = 4.0
# How far (mm) a planar face's offset must sit from the body's true extreme
# along that face's own normal direction to count as "recessed" rather than
# being the part's actual outer surface in that direction.
POCKET_RECESS_MARGIN_MM = 0.1

# A single continuous filleted edge is often exported as SEVERAL adjacent
# face patches of the SAME radius, split wherever the underlying curve has
# a knot (e.g. a straight run and a curved run of one constant-radius
# perimeter fillet meet as a cylinder face butted against a torus face, or
# a long straight run gets trimmed into two cylinder faces at an arbitrary
# midpoint) - geometrically separate `Face` objects for what is visually
# and physically ONE fillet. See `_merge_colinear_fillets`: two candidate
# fillets merge only if they share the same radius AND lie on the exact
# same infinite axis line (not just the same direction elsewhere on the
# body) - a real repeated instance at a different corner has the same
# radius and often the same direction too, but sits on a DIFFERENT line,
# so it is correctly left as its own separate feature.
FILLET_MERGE_RADIUS_TOLERANCE_MM = 0.05
FILLET_MERGE_AXIS_ANGLE_TOLERANCE = 0.02
FILLET_MERGE_AXIS_LINE_DISTANCE_TOLERANCE_MM = 0.15


def _merge_colinear_fillets(candidates):
    """Collapse fillet-face candidates that are really the same physical
    edge split into several adjacent patches (see the constants above) into
    one entry per physical fillet - averaging their centroids for a single,
    more central anchor point instead of picking one patch's midpoint
    arbitrarily.
    """
    merged = []
    used = [False] * len(candidates)
    for i, f in enumerate(candidates):
        if used[i]:
            continue
        group = [f]
        used[i] = True
        for j in range(i + 1, len(candidates)):
            if used[j]:
                continue
            g = candidates[j]
            if abs(f['radius_mm'] - g['radius_mm']) > FILLET_MERGE_RADIUS_TOLERANCE_MM:
                continue
            dot = sum(a * b for a, b in zip(f['axis_direction'], g['axis_direction']))
            if abs(dot) < 1 - FILLET_MERGE_AXIS_ANGLE_TOLERANCE:
                continue
            diff = tuple(g['centroid'][k] - f['centroid'][k] for k in range(3))
            along = sum(diff[k] * f['axis_direction'][k] for k in range(3))
            perp_sq = sum((diff[k] - along * f['axis_direction'][k]) ** 2 for k in range(3))
            if math.sqrt(perp_sq) > FILLET_MERGE_AXIS_LINE_DISTANCE_TOLERANCE_MM:
                continue
            group.append(g)
            used[j] = True
        rep = dict(group[0])
        if len(group) > 1:
            rep['centroid'] = tuple(
                sum(g['centroid'][k] for g in group) / len(group) for k in range(3)
            )
        merged.append(rep)
    return merged


def _surface_midpoint(adaptor):
    """A point guaranteed to lie ON the face's surface, at its own
    mid-parameter - unlike `Face.Center()` (BRepGProp's area centroid),
    which for a curved band like a fillet or chamfer can land INSIDE the
    solid rather than on the visible surface (the same reason a circle's
    centroid sits at its middle, not on the circle itself). Using the area
    centroid as a leader's anchor point was producing fillet callouts that
    pointed at empty space near, but not on, the actual rounded edge.
    """
    u = (adaptor.FirstUParameter() + adaptor.LastUParameter()) / 2
    v = (adaptor.FirstVParameter() + adaptor.LastVParameter()) / 2
    p = adaptor.Value(u, v)
    return (p.X(), p.Y(), p.Z())


def _is_internal_cylinder(face_wrapped, adaptor, axis_location, axis_direction):
    """A cylindrical face is a hole/bore (internal, concave - material is
    outside it, e.g. a drilled hole or a sketch-extruded cut) if its
    outward-facing normal points back TOWARD the axis. It's a boss/shaft/
    round stock (external, convex - material is inside it) if the normal
    points AWAY from the axis. This is what lets an unmatched cylindrical
    face be inferred as "a hole with no metadata" instead of e.g. an
    outer round boundary of round bar stock.
    """
    u = (adaptor.FirstUParameter() + adaptor.LastUParameter()) / 2
    v = (adaptor.FirstVParameter() + adaptor.LastVParameter()) / 2
    surface = adaptor.Surface().Surface()
    props = GeomLProp_SLProps(surface, u, v, 1, 1e-6)
    if not props.IsNormalDefined():
        return None
    normal = props.Normal()
    if face_wrapped.Orientation() == TopAbs_REVERSED:
        normal.Reverse()

    point = props.Value()
    ax, ay, az = axis_location
    dx, dy, dz = axis_direction
    # Vector from axis point to surface point, then strip the component
    # along the axis so what's left is the pure radial direction.
    px, py, pz = point.X() - ax, point.Y() - ay, point.Z() - az
    along = px * dx + py * dy + pz * dz
    rx, ry, rz = px - along * dx, py - along * dy, pz - along * dz
    r_len = math.sqrt(rx * rx + ry * ry + rz * rz)
    if r_len < 1e-9:
        return None
    rx, ry, rz = rx / r_len, ry / r_len, rz / r_len

    dot = normal.X() * rx + normal.Y() * ry + normal.Z() * rz
    return dot < 0  # normal opposes radial-outward direction -> concave/internal


# How much extra clearance (mm) beyond a hole's own radius still counts a
# nearby planar face as that hole's floor - a real counterbore/blind-hole
# floor's edge is trimmed right at (or a hair inside) the wall's radius, not
# exactly on it, and STEP export/OCC trimming leaves a small amount of slack.
HOLE_FLOOR_RADIAL_MARGIN_MM = 1.0
# How parallel a planar face's normal must be to a candidate hole's axis to
# count as that hole's floor (dot product of two unit vectors) - a true
# floor is exactly perpendicular to the hole wall, i.e. its normal IS the
# hole's axis; this just tolerates ordinary floating-point slack, not a
# genuinely different orientation.
HOLE_FLOOR_AXIS_ALIGNMENT_MIN = 0.9


def _is_hole_floor(centroid, normal, internal_cylinders):
    """True if `centroid` (a planar face's own surface point) is the flat
    floor of one of `internal_cylinders` - i.e. it sits within that hole's
    own radius (plus a small margin) of the hole's axis LINE, with its
    normal nearly parallel to that axis. Used to keep a counterbore/
    blind-hole floor from being separately reported as an unrelated
    "pocket" (see `pocket_faces`) - it's already part of that hole's own
    depth measurement.
    """
    cx, cy, cz = centroid
    nx, ny, nz = normal
    for cf in internal_cylinders:
        dx, dy, dz = cf.axis_direction
        if abs(nx * dx + ny * dy + nz * dz) < HOLE_FLOOR_AXIS_ALIGNMENT_MIN:
            continue
        ox, oy, oz = cf.axis_origin
        vx, vy, vz = cx - ox, cy - oy, cz - oz
        along = vx * dx + vy * dy + vz * dz
        rx, ry, rz = vx - along * dx, vy - along * dy, vz - along * dz
        perp_dist = math.sqrt(rx * rx + ry * ry + rz * rz)
        if perp_dist <= cf.radius + HOLE_FLOOR_RADIAL_MARGIN_MM:
            return True
    return False


class CylindricalFace:
    """A cylindrical face found in the body, with the data needed to match
    it against a hole/thread entry from project_data.json: diameter and the
    axis location/direction. `is_internal` is None if it couldn't be
    determined (degenerate normal), True for a hole/bore, False for a
    boss/shaft/round stock surface. `angular_span_deg` is how much of the
    circle this face actually wraps (~360 for a true round hole; much less
    for one rounded end of a slot - see `is_slot_end`).

    `axis_origin`/`v_min`/`v_max` capture the face's own raw parametrization
    (the cylinder surface's V parameter is exactly the axial coordinate in
    real length units - not normalized), so `axial_length_mm` and
    `start_point`/`end_point` give this segment's true depth and the two
    endpoints of its visible extent along the axis - needed to group
    multiple cylindrical faces (e.g. a counterbore's two walls) into one
    physical hole and report its real stepped depth, instead of two
    unrelated-looking diameter callouts.
    """

    def __init__(self, face, radius, axis_location, axis_direction, is_internal, angular_span_deg,
                 axis_origin, v_min, v_max):
        self.face = face
        self.radius = radius
        self.diameter = radius * 2
        self.axis_location = axis_location
        self.axis_direction = axis_direction
        self.is_internal = is_internal
        self.angular_span_deg = angular_span_deg
        self.axis_origin = axis_origin
        self.v_min = v_min
        self.v_max = v_max

    @property
    def is_slot_end(self):
        return self.angular_span_deg < FULL_CIRCLE_THRESHOLD_DEG

    @property
    def axial_length_mm(self):
        return self.v_max - self.v_min

    def _point_at_v(self, v):
        ox, oy, oz = self.axis_origin
        dx, dy, dz = self.axis_direction
        return (ox + v * dx, oy + v * dy, oz + v * dz)

    @property
    def start_point(self):
        return self._point_at_v(self.v_min)

    @property
    def end_point(self):
        return self._point_at_v(self.v_max)

    @property
    def surface_midpoint(self):
        """A point guaranteed to lie ON this face's actual visible surface
        (mid U/V, same approach as `_surface_midpoint()` for fillets/
        chamfers) - unlike `start_point`/`end_point`/`axis_location`, which
        sit on the cylinder's AXIS. For a true hole that axis point IS the
        visible circle's center, so it's a fine anchor - but for a partial/
        off-axis cylindrical face (a slot's rounded end, or an elongated
        fillet-like band misclassified as one), the axis can sit well
        outside the body entirely (confirmed on a real part: an axis
        location 55mm from the body's center on a body whose real extent
        only reaches 42mm) - anchoring a leader there puts its arrow
        floating in empty space nowhere near the actual feature. Used for
        any non-hole point-feature callout anchor (see `_summarize_slots`).
        """
        return _surface_midpoint(BRepAdaptor_Surface(self.face.wrapped))

    def __repr__(self):
        kind = 'internal' if self.is_internal else 'external' if self.is_internal is False else 'unknown'
        return (f'CylindricalFace(diameter={self.diameter:.3f}mm, {kind}, '
                f'span={self.angular_span_deg:.0f}deg, len={self.axial_length_mm:.2f}mm, at={self.axis_location})')


class StepBody:
    """Loaded geometry for a single body's STEP file."""

    def __init__(self, step_path):
        self.step_path = step_path
        self.workplane = cq.importers.importStep(step_path)
        self.shape = self.workplane.val()
        self._bbox = None

    @property
    def bounding_box(self):
        # Exact (optimal) bounding boxes are slow on complex bodies (~0.3 s
        # each) and callers ask for them hundreds of times - compute once.
        if self._bbox is None:
            self._bbox = self.shape.BoundingBox()
        return self._bbox

    def faces(self):
        return self.shape.Faces()

    def edges(self):
        return self.shape.Edges()

    def triangle_mesh(self, deflection=0.5):
        """(vertices, triangles) for the whole body, for display only (e.g.
        the 3D body-picker viewer) - NOT used anywhere in the actual
        dimension/HLR pipeline, which works from exact BRep geometry.
        `vertices` is a list of (x, y, z) tuples in the body's own absolute
        STEP coordinates; `triangles` is a list of (i, j, k) 0-based index
        tuples into `vertices`. Meshes every face independently (matching
        OCP's own per-face triangulation storage) rather than merging shared
        vertices across face boundaries - fine for a rendering-only view.
        """
        BRepMesh_IncrementalMesh(self.shape.wrapped, deflection)
        vertices = []
        triangles = []
        explorer = TopExp_Explorer(self.shape.wrapped, TopAbs_FACE)
        while explorer.More():
            face = TopoDS.Face_s(explorer.Current())
            loc = TopLoc_Location()
            tri_data = BRep_Tool.Triangulation_s(face, loc)
            if tri_data is not None:
                trsf = loc.Transformation()
                base_index = len(vertices)
                for i in range(1, tri_data.NbNodes() + 1):
                    node = tri_data.Node(i).Transformed(trsf)
                    vertices.append((node.X(), node.Y(), node.Z()))
                for i in range(1, tri_data.NbTriangles() + 1):
                    n1, n2, n3 = tri_data.Triangle(i).Get()
                    triangles.append((base_index + n1 - 1, base_index + n2 - 1, base_index + n3 - 1))
            explorer.Next()
        return vertices, triangles

    def wireframe_edges(self, deflection=0.3):
        """List of polylines (each a list of (x, y, z) tuples) approximating
        every edge on the body, for a fast display-only wireframe (e.g. the
        3D body-picker viewer). Much cheaper than `triangle_mesh()` - it
        samples each of the body's own edge curves directly (via
        GCPnts_QuasiUniformDeflection over a BRepAdaptor_Curve) instead of
        meshing every face's full surface, and draws as lines instead of
        filled/shaded triangles, both of which were the actual sources of
        slowness for a viewer that only needs to convey shape for picking.
        """
        polylines = []
        explorer = TopExp_Explorer(self.shape.wrapped, TopAbs_EDGE)
        while explorer.More():
            edge = TopoDS.Edge_s(explorer.Current())
            curve_adaptor = BRepAdaptor_Curve(edge)
            sampler = GCPnts_QuasiUniformDeflection(curve_adaptor, deflection)
            if sampler.IsDone() and sampler.NbPoints() >= 2:
                points = [sampler.Value(i) for i in range(1, sampler.NbPoints() + 1)]
                polylines.append([(p.X(), p.Y(), p.Z()) for p in points])
            explorer.Next()
        return polylines

    def cylindrical_faces(self):
        """Return every cylindrical face on the body, e.g. hole/bore walls
        and thread-modeled cylinders, with radius and axis info extracted
        via BRepAdaptor_Surface (the plain cadquery Face API doesn't expose
        cylinder parameters directly, so we drop to OCP for this).
        """
        results = []
        for face in self.faces():
            adaptor = BRepAdaptor_Surface(face.wrapped)
            if adaptor.GetType() != GeomAbs_Cylinder:
                continue
            cylinder = adaptor.Cylinder()
            radius = cylinder.Radius()
            axis = cylinder.Axis()
            location = axis.Location()
            direction = axis.Direction()
            axis_direction = (direction.X(), direction.Y(), direction.Z())

            # `location` is merely SOME reference point OCC picked on the
            # cylinder's infinite mathematical axis (V=0) - it is NOT
            # guaranteed to fall within this face's actual finite trimmed
            # extent, so using it directly as "the hole's position" can
            # place a marker well outside the visible hole in any view
            # where the axis isn't parallel to the view direction. For a
            # cylindrical surface, V is exactly the axial coordinate (point
            # = Location + V*Direction + radius*radial), so the true axial
            # midpoint of the visible face is Location + V_mid*Direction.
            v_mid = (adaptor.FirstVParameter() + adaptor.LastVParameter()) / 2
            axis_location = (
                location.X() + v_mid * direction.X(),
                location.Y() + v_mid * direction.Y(),
                location.Z() + v_mid * direction.Z(),
            )
            is_internal = _is_internal_cylinder(face.wrapped, adaptor, axis_location, axis_direction)
            angular_span_deg = math.degrees(adaptor.LastUParameter() - adaptor.FirstUParameter())
            axis_origin = (location.X(), location.Y(), location.Z())
            results.append(CylindricalFace(
                face, radius, axis_location, axis_direction, is_internal, angular_span_deg,
                axis_origin, adaptor.FirstVParameter(), adaptor.LastVParameter(),
            ))
        return results

    def fillet_faces(self):
        """Every fillet (constant-radius edge blend) on the body.

        A fillet blending a CURVED edge (e.g. rounding the rim of a hole, or
        blending a corner where three faces meet) is modeled as a toroidal
        surface, so its minor radius IS the fillet radius. But a fillet
        blending a plain STRAIGHT edge - by far the most common kind, e.g.
        rounding a straight corner of a machined box - is geometrically
        just a CYLINDER (a constant circular cross-section swept along a
        straight line), indistinguishable in surface TYPE from a round
        boss/shaft's own outer wall. An earlier version of this only
        scanned toroidal surfaces, so every straight-edge fillet was
        silently reported as an ordinary "external cylindrical face" -
        never matched against anything, never summarized, never given a
        callout anywhere - a real, previously undiagnosed gap where a
        genuinely visible rounded edge on the part just had no balloon on
        any page at all.

        The two are told apart by angular span: a real round boss/shaft
        wraps essentially the full circle (~360 deg, like a true hole's
        wall), while an edge fillet only covers the angle BETWEEN the two
        blended faces (typically ~90 deg for a box corner, but genuinely
        anything under a full circle) - the same `angular_span_deg` /
        `FULL_CIRCLE_THRESHOLD_DEG` signal `CylindricalFace.is_slot_end`
        already uses for internal faces, applied here to EXTERNAL
        (convex) ones instead.

        Sub-0.3mm radii are skipped (see MIN_REPORTED_FILLET_RADIUS_MM) -
        those are near-universally cosmetic micro-blends, not something a
        machinist would care about.
        """
        results = []
        for face in self.faces():
            adaptor = BRepAdaptor_Surface(face.wrapped)
            if adaptor.GetType() != GeomAbs_Torus:
                continue
            radius = adaptor.Torus().MinorRadius()
            if radius < MIN_REPORTED_FILLET_RADIUS_MM:
                continue
            point = _surface_midpoint(adaptor)
            axis_dir = adaptor.Torus().Axis().Direction()
            results.append({
                'face': face,
                'radius_mm': radius,
                'centroid': point,
                'axis_direction': (axis_dir.X(), axis_dir.Y(), axis_dir.Z()),
            })

        for cf in self.cylindrical_faces():
            if cf.is_internal is not False:
                continue  # only external/convex cylinders can be a straight-edge fillet
            if cf.angular_span_deg >= FULL_CIRCLE_THRESHOLD_DEG:
                continue  # a real round boss/shaft, not an edge fillet
            if cf.radius < MIN_REPORTED_FILLET_RADIUS_MM:
                continue
            results.append({
                'face': cf.face,
                'radius_mm': cf.radius,
                'centroid': cf.surface_midpoint,
                'axis_direction': cf.axis_direction,
            })
        return _merge_colinear_fillets(results)

    def chamfer_faces(self):
        """Every chamfer (flat angled edge break) on the body, modeled as a
        conical frustum face. Reports the cone's half-angle (the angle a
        chamfer callout is normally given in, e.g. "45 deg x 1mm") AND the
        linear chamfer size (`depth_mm`) - the face's own extent along the
        cone's axis (`LastVParameter() - FirstVParameter()`; for a
        BRepAdaptor cone surface, V is exactly the signed distance along the
        axis from the cone's reference circle, so this IS the chamfer's real
        leg length, not an estimate).
        """
        results = []
        for face in self.faces():
            adaptor = BRepAdaptor_Surface(face.wrapped)
            if adaptor.GetType() != GeomAbs_Cone:
                continue
            angle_deg = abs(math.degrees(adaptor.Cone().SemiAngle()))
            depth_mm = abs(adaptor.LastVParameter() - adaptor.FirstVParameter())
            point = _surface_midpoint(adaptor)
            axis_dir = adaptor.Cone().Axis().Direction()
            results.append({
                'face': face,
                'angle_deg': angle_deg,
                'depth_mm': depth_mm,
                'centroid': point,
                'axis_direction': (axis_dir.X(), axis_dir.Y(), axis_dir.Z()),
            })
        return results

    def _all_vertex_points(self):
        points = []
        explorer = TopExp_Explorer(self.shape.wrapped, TopAbs_VERTEX)
        while explorer.More():
            vtx = TopoDS.Vertex_s(explorer.Current())
            pnt = BRep_Tool.Pnt_s(vtx)
            points.append((pnt.X(), pnt.Y(), pnt.Z()))
            explorer.Next()
        return points

    def extent_along_direction(self, direction):
        """The body's own min/max projection onto an arbitrary unit
        direction (not necessarily X/Y/Z) - used to tell whether a hole
        cluster's axial span reaches all the way through the material
        (THRU) or stops partway (blind).
        """
        all_points = self._all_vertex_points()
        if not all_points:
            return 0.0, 0.0
        dx, dy, dz = direction
        offsets = [px * dx + py * dy + pz * dz for px, py, pz in all_points]
        return min(offsets), max(offsets)

    def pocket_faces(self):
        """Heuristic detector for pocket/pad floors and other recessed
        planar faces that aren't holes, threads, fillets, or chamfers
        (rectangular pockets, milled slots' flat bottoms, counterbore
        floors, stepped shoulders, etc.) - the shapes a cylindrical/
        toroidal/conical surface classifier can't catch at all.

        A planar face is reported as "recessed" if, projected onto its own
        normal direction, its offset sits strictly between the body's true
        min/max extent along that same direction (by more than
        POCKET_RECESS_MARGIN_MM) - i.e. it is NOT the part's actual outer
        surface in that direction, so something must be cut below/above it
        on at least one side. This is a coarse proxy, not real feature
        recognition: it will also flag legitimate stepped/shoulder faces
        that aren't "pockets" in the colloquial sense, and can't tell a
        round pocket from a slot-shaped one from a free-form one - it only
        answers "is there a recessed flat face here worth mentioning",
        which is what a diameter/radius-based classifier structurally
        cannot do for non-cylindrical cavities.

        A flat annular/circular face sitting at the bottom of a real
        internal cylindrical hole (a counterbore or blind-hole floor) is
        ALSO a planar face recessed below the surrounding surface, so it
        passes the same test above - but it's already reported as part of
        that hole's own depth measurement (`_summarize_holes`), so counting
        it again here as a separate "pocket" was a real mislabeling bug:
        the same physical floor showed up twice, once correctly as the
        hole's DEEP dimension and once bogusly as an unrelated "POCKET".
        Excluded via `_is_hole_floor()`: a planar face whose centroid sits
        within (radius + margin) of an internal cylinder's own axis line,
        with its normal nearly parallel to that cylinder's axis, IS that
        hole's floor, not an independent pocket.
        """
        all_points = self._all_vertex_points()
        if not all_points:
            return []

        internal_cylinders = [cf for cf in self.cylindrical_faces() if cf.is_internal]

        results = []
        for face in self.faces():
            adaptor = BRepAdaptor_Surface(face.wrapped)
            if adaptor.GetType() != GeomAbs_Plane:
                continue

            area = face.Area()
            if area < MIN_REPORTED_POCKET_AREA_MM2:
                continue

            plane = adaptor.Plane()
            normal = plane.Axis().Direction()
            nx, ny, nz = normal.X(), normal.Y(), normal.Z()
            loc = plane.Location()
            face_offset = loc.X() * nx + loc.Y() * ny + loc.Z() * nz

            offsets = [px * nx + py * ny + pz * nz for px, py, pz in all_points]
            min_off, max_off = min(offsets), max(offsets)

            is_recessed = (
                face_offset > min_off + POCKET_RECESS_MARGIN_MM
                and face_offset < max_off - POCKET_RECESS_MARGIN_MM
            )
            if not is_recessed:
                continue

            centroid = _surface_midpoint(adaptor)
            if _is_hole_floor(centroid, (nx, ny, nz), internal_cylinders):
                continue

            results.append({
                'face': face,
                'area_mm2': area,
                'centroid': centroid,
                'normal': (nx, ny, nz),
            })
        return results

    def summary(self):
        bb = self.bounding_box
        cyl_faces = self.cylindrical_faces()
        return {
            'step_path': self.step_path,
            'face_count': len(self.faces()),
            'edge_count': len(self.edges()),
            'bounding_box_mm': {'x': bb.xlen, 'y': bb.ylen, 'z': bb.zlen},
            'cylindrical_face_count': len(cyl_faces),
            'cylindrical_face_diameters_mm': sorted({round(f.diameter, 3) for f in cyl_faces}),
        }


def load_step(step_path):
    return StepBody(step_path)
