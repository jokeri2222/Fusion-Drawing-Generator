"""
Generate a technical drawing PDF for a single body: one page per view
(TOP, FRONT, RIGHT, ISO) rather than four small views crammed onto one
sheet - a full page gives the collision-avoidance router room to actually
find clean callout placements instead of fighting over a quarter-page box.
Each orthographic page carries overall width/height + hole-to-hole spacing
dimension lines, real leader-line hole/thread callouts placed directly at
each feature, and a title block (with a "SHEET n OF 4" label).

Views are produced with OpenCascade's Hidden Line Removal algorithm
(HLRBRep_Algo, via OCP) rather than a hand-rolled projection - it gives
real visible/hidden edge separation (solid vs. dashed lines), which a
naive "drop one axis" projection can't distinguish.

Each hole cluster's 3D axis point (from constraint_matcher.py's grouping of
cylindrical faces into physical holes) is projected into the same
view-local 2D frame HLR itself uses (same gp_Ax2, read back via its actual
XDirection()/YDirection() rather than a hand-derived cross product, so the
callout lands exactly where HLR would place that point), then annotated
with a real leader line - an arrowhead at the feature, a bent leader out to
the callout text (diameter/thread designation + depth or "THRU ALL") - the
standard SolidWorks/ASME style. Each hole is called out in exactly the one
view where its axis is most head-on (so it reads as a true circle there),
never repeated across views. Fillets/chamfers/slots/pockets have no single
point to hang a leader off, so they're listed as a summary in the title
block instead.
"""

import math
import os

from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_PolyAlgo, HLRBRep_PolyHLRToShape
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopoDS import TopoDS
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GeomAbs import GeomAbs_Line
from OCP.BRepMesh import BRepMesh_IncrementalMesh

from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.units import mm

import units
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

from step_loader import load_step
from constraint_matcher import match_all_bodies, _best_view_for_point

PAGE_SIZE = landscape(A3)
MARGIN = 15 * mm
# Tall enough to fit the feature legend (see _draw_legend()) in the title
# block's own right-hand area (to the right of the name/date/projection/
# units/scale field grid) - that area was previously just empty space, so
# the legend now lives there instead of its own separate reserved strip
# above the title block (which always cost the view space even on pages
# with very few or no legend entries).
TITLE_BLOCK_HEIGHT = 55 * mm
SAMPLES_PER_LINE = 2      # a straight line needs exactly its 2 endpoints
SAMPLES_PER_CURVE = 64    # circles/arcs/splines - was a flat 20 for every
                          # edge regardless of type, visibly faceting large
                          # arcs and round corners into short straight
                          # segments instead of a smooth curve

# The eye sits on the `direction` side and looks back toward the origin
# (i.e. a face located in +direction from the model's center is the NEAR,
# visible one) - rigorously confirmed in an earlier round via synthetic
# test boxes with a blind hole drilled from one known face at a time,
# rendered through this real HLR pipeline: the hole's rim came out VISIBLE
# precisely when the drilled face's side matched the view direction's own
# sign (e.g. a hole from the -Y face was visible under FRONT, whose
# direction is (0,-1,0)). Fusion doesn't export any "which way is up"
# metadata, so TOP/BOTTOM's Z-sign assignment is an arbitrary convention
# with a real 50/50 chance of not matching a given design's own intended
# orientation - confirmed backwards for this project's actual parts
# (threads meant to read as being on the bottom face were showing under
# the "TOP" page and vice versa), so TOP/BOTTOM's directions are swapped
# from the original (0,0,-1)/(0,0,1) guess.
#
# `_ISO_DIR` was NOT re-verified the same rigorous way when the above was
# established, and was instead reasoned from an since-corrected (and, at
# the time, already self-contradicting - see the "no -" mid-sentence
# correction this comment used to have) belief that "the eye sits at
# -direction" - i.e. exactly backwards from the confirmed convention
# above. Re-verified with the same synthetic-hole methodology used for
# the orthographic views: with the ORIGINAL `(1, 1, 1)`, a hole on the
# +Z/+Y/+X faces (which should be near/visible for a camera positioned in
# the (+,+,+) octant, matching every other view's own confirmed
# convention) came out HIDDEN, and a hole on the far -Z/-Y/-X faces came
# out VISIBLE - exactly inverted. It also projected the model UPSIDE DOWN
# on the page (a synthetic box's +Z/top face landed at negative Y, i.e.
# the bottom half of the page) - negating the direction (to (-1,-1,-1))
# fixed both symptoms in a synthetic single-hole test, but the user
# (checking against the real, current renders directly, not a synthetic
# proxy) confirmed the calculation itself is fine but the vantage point
# is still the wrong one of the two possible corners - flipping back to
# the opposite direction from THAT is what actually looks correct on the
# real geometry. Net effect across both rounds: back to the original
# `(1, 1, 1)`, kept here because it's what's been confirmed against the
# real part, not because the synthetic single-hole test it originally
# came from was trusted - that test's own reasoning was later found
# unreliable for oblique/tangent silhouette cases (see project_view's
# note on tangent tie-breaks). If this ever looks backwards again, negate
# this tuple and re-render against the real model - do not re-derive it
# from a synthetic point-matching test again, since two rounds of that
# both produced confident-looking but ultimately wrong or unreliable
# verdicts for this specific oblique view.
# Getting the top-of-page/visibility right (see the history below) still
# left ISO showing TOP+BACK+RIGHT (the near/solid faces under `(1,1,1)`) -
# a valid, self-consistent pictorial, but not the conventional choice: a
# real ISO drawing conventionally shows TOP+FRONT+RIGHT, the same three
# faces every OTHER orthographic view is already built around, so the
# pictorial reads as "the same part, at an angle" rather than showing its
# back. FRONT's own established direction is (0,-1,0) - swapping `_ISO_DIR`'s
# Y sign (from `(1,1,1)` to `(1,-1,1)`) makes -Y (FRONT) the near side
# instead of +Y (BACK), while keeping +X (RIGHT) and +Z (TOP) as the other
# two near sides exactly as before. Re-verified physical "up" still
# projects to positive page-y with this direction paired with `_ISO_X =
# (1,1,0)` - both symptoms (top-of-page and which face convention) fixed
# together, using a real change of 3D viewpoint, never a 2D page mirror.
_ISO_DIR = (1, -1, 1)
_ISO_NORM = 1 / math.sqrt(3)
_ISO_X = (1, 1, 0)
_ISO_X_NORM = 1 / math.sqrt(2)

# (direction the camera looks along, "up"/x-axis of the view plane)
# All six standard orthographic views, plus one pictorial ISO - the
# opposite-facing three (BOTTOM/BACK/LEFT) mirror TOP/FRONT/RIGHT's own
# direction and were picked/verified by rendering, not derived from a
# convention doc: each one must still look upright (not flipped vertically)
# and correctly mirrored left-right relative to its opposite face (the
# standard result of viewing the same part from behind/below/the other
# side), not merely re-showing the same image.
# RIGHT/LEFT's own x_dir is negated from what a naive "mirror of the other
# side" pick would give: verified directly (project a synthetic (0,0,10)
# point through each view's own gp_Ax2) that with the naive x_dir, +Z
# (physical "up", independently established via TOP/BOTTOM's own hole
# visibility) projected to NEGATIVE page-y for both RIGHT and LEFT - i.e.
# both side views rendered genuinely upside down, while FRONT/BACK (whose
# x_dir was already the version confirmed here) correctly project +Z to
# positive page-y. TOP/BOTTOM have no such up/down ambiguity at all (Z is
# their own viewing axis, dropped from the 2D projection entirely) -
# exactly matching the user's own report that those two were fine while
# every side view wasn't. Negating x_dir here flips the page's Y axis
# (Y_page_direction = view_direction x_dir, standard cross product) without
# touching `direction` itself, which is what actually controls near/far
# visibility and was NOT reported wrong - so it stays exactly as
# previously (rigorously, separately) verified.
VIEW_DIRECTIONS = {
    'FRONT': ((0, -1, 0), (1, 0, 0)),
    'BACK': ((0, 1, 0), (-1, 0, 0)),
    'TOP': ((0, 0, 1), (1, 0, 0)),
    'BOTTOM': ((0, 0, -1), (1, 0, 0)),
    'RIGHT': ((1, 0, 0), (0, 1, 0)),
    'LEFT': ((-1, 0, 0), (0, -1, 0)),
    'ISO': (
        tuple(d * _ISO_NORM for d in _ISO_DIR),
        tuple(d * _ISO_X_NORM for d in _ISO_X),
    ),
}
ORTHOGRAPHIC_VIEWS = ('TOP', 'BOTTOM', 'FRONT', 'BACK', 'LEFT', 'RIGHT')
# Views that get dimension lines + feature callouts. ISO is pictorial only
# (dimensioning an isometric view isn't standard practice).
DIMENSIONED_VIEWS = ORTHOGRAPHIC_VIEWS


def make_ax2(direction, x_dir):
    return gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*direction), gp_Dir(*x_dir))


def project_point(ax2, point3d):
    """Project a 3D point into the same 2D frame HLRBRep_HLRToShape's
    output edges live in: local X/Y read straight from the gp_Ax2 actually
    passed to HLRAlgo_Projector, not re-derived, so this always agrees with
    however OCC internally resolves the frame's handedness.
    """
    p = gp_Pnt(*point3d)
    x_dir = ax2.XDirection()
    y_dir = ax2.YDirection()
    vx, vy, vz = p.X(), p.Y(), p.Z()
    local_x = vx * x_dir.X() + vy * x_dir.Y() + vz * x_dir.Z()
    local_y = vx * y_dir.X() + vy * y_dir.Y() + vz * y_dir.Z()
    return local_x, local_y


def _edges_to_polylines(compound):
    """Sample every edge in a compound into a polyline of (x, y) points,
    using the compound's own X/Y (HLRBRep_HLRToShape already transforms
    geometry into the view's local 2D frame; the local Z is depth only,
    used internally for visibility - we can ignore it here).
    """
    polylines = []
    explorer = TopExp_Explorer(compound, TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        adaptor = BRepAdaptor_Curve(edge)
        u0, u1 = adaptor.FirstParameter(), adaptor.LastParameter()
        samples = SAMPLES_PER_LINE if adaptor.GetType() == GeomAbs_Line else SAMPLES_PER_CURVE
        points = []
        for i in range(samples + 1):
            u = u0 + (u1 - u0) * i / samples
            p = adaptor.Value(u)
            points.append((p.X(), p.Y()))
        if len(points) >= 2:
            polylines.append(points)
        explorer.Next()
    return polylines


# Mesh deflection (mm) used before running the polygon-based HLR engine -
# see project_view()'s own docstring for why this replaced the exact-BRep
# HLRBRep_Algo entirely. Tightened from an initial 0.05 after a small
# circle's rendered outline was confirmed (via direct before/after
# comparison at the same location) to have a visible flat "dent" at 0.05 -
# a genuine mesh-coarseness artifact in the underlying sample points
# themselves, not something the curve-smoothing pass could or should paper
# over. Gone at this finer value. Still coarse enough that the resulting
# polyline count/render time stays practical for a full 13-body
# regeneration.
HLR_MESH_DEFLECTION_MM = 0.01

# Angular deflection (radians) for the same mesh pass - how much a curved
# surface's normal is allowed to turn between adjacent mesh triangles.
# BRepMesh_IncrementalMesh's own default (0.5 rad, ~29 degrees) is tuned
# for fast rough-display meshing, not for feeding an HLR pass that has to
# decide visible-vs-hidden (and, for round surfaces, the self-occluding
# OutLine silhouette itself) from that mesh - at the default, a shallow
# fillet or large-radius round can go several linear-deflection-sized
# triangles between facets without the LINEAR deflection limit alone
# catching it, faceting self-occlusion boundaries that only ever look
# "behind" or "in front of" the surface they're actually tangent to.
# Tightening this independently of the linear deflection above fixes that
# without needing a much smaller (and much more expensive) linear value.
HLR_MESH_ANGULAR_DEFLECTION_RAD = 0.15


def project_view(shape, direction, x_dir, mesh_deflection_mm=None):
    """Run HLR for one view direction. Returns (visible_edges, hidden_edges).

    Uses the exact-BRep engine (vector_hlr.py): each edge is a list of sample
    points that also carries its true Bezier/line path segments (`.segs`), which
    the PDF drawing uses so curves are smooth vectors, dashed ones included.
    The long note below describes the older polygon engine and is historical.


    `mesh_deflection_mm` overrides HLR_MESH_DEFLECTION_MM for callers that
    don't need dimensioning-grade detail (e.g. the pictorial-only assembly
    instructions PDF) and would rather have a much smaller polyline count/
    file size than the fine curve fidelity every dimensioned drawing page
    needs.

    Uses OpenCascade's POLYGON-based HLR engine (HLRBRep_PolyAlgo, operating
    on a mesh of the shape) rather than the exact-BRep one (HLRBRep_Algo,
    operating on the shape's own analytic surfaces/curves) this project used
    through several earlier rounds. Switched after the exact-BRep engine
    was confirmed, via direct inspection of its own raw output, to
    genuinely and routinely report the SAME physical edge in BOTH the
    visible AND hidden compounds at once for tangent/near-coincident
    surfaces (67 such duplicates found on a single view of one real part) -
    not a rare edge case, but common enough that no reliable rule (tried
    twice, both reverted after causing real regressions) could tell a
    genuine duplicate-reporting bug apart from completely ordinary,
    correct geometry that also happens to produce a visible/hidden overlap.
    The polygon-based engine, working from a discrete mesh instead of exact
    analytic tangency, does not exhibit this: the same tangent seam that
    produced duplicate/fragmented output from HLRBRep_Algo comes out as a
    single, correctly-classified hidden span here (confirmed by direct
    side-by-side comparison on the real part that originally surfaced the
    bug). The trade-off is a much higher polyline count (edges come out as
    many small mesh-triangle-boundary segments rather than one polyline per
    analytic curve) - visually smooth at the mesh deflection used here, but
    heavier to draw; no other part of this pipeline assumes a particular
    polyline count or edge-to-curve correspondence, so this is a drop-in
    replacement at the `project_view()` boundary.
    """
    import vector_hlr
    real_shape = shape.wrapped if hasattr(shape, 'wrapped') else shape
    visible, hidden = vector_hlr.exact_views({'shape': real_shape}, direction, x_dir)['shape']
    return visible, hidden


def generate_views(shape):
    """Project the shape into FRONT/TOP/RIGHT/ISO views."""
    views = {}
    for name, (direction, x_dir) in VIEW_DIRECTIONS.items():
        visible, hidden = project_view(shape, direction, x_dir)
        views[name] = {'visible': visible, 'hidden': hidden, 'ax2': make_ax2(direction, x_dir)}
    return views


def _bbox_of_polylines(polylines):
    xs = [p[0] for line in polylines for p in line]
    ys = [p[1] for line in polylines for p in line]
    if not xs:
        return (0, 0, 0, 0)
    return (min(xs), min(ys), max(xs), max(ys))


def _extreme_points(polylines):
    """The actual (x, y) points achieving min-X/max-X/min-Y/max-Y among the
    real geometry - NOT the four corners of its bounding box, which for
    anything other than an axis-aligned rectangle (an oval flange, a
    rounded corner, ...) are empty space nowhere near the actual outline.
    An envelope dimension's extension line anchored at a bbox corner for a
    rounded shape doesn't visibly connect to anything - it dangles from a
    point outside the part, which is exactly what "the lines don't extend
    fully" looked like: a short disconnected stub near the corner instead
    of a line reaching up to the real edge of the oval.
    """
    pts = [p for line in polylines for p in line]
    if not pts:
        return (0, 0), (0, 0), (0, 0), (0, 0)
    min_x_pt = min(pts, key=lambda p: p[0])
    max_x_pt = max(pts, key=lambda p: p[0])
    min_y_pt = min(pts, key=lambda p: p[1])
    max_y_pt = max(pts, key=lambda p: p[1])
    return min_x_pt, max_x_pt, min_y_pt, max_y_pt


ARROW_LENGTH = 2.2 * mm
ARROW_WIDTH = 0.8 * mm
EXTENSION_GAP = 0.8 * mm
EXTENSION_OVERSHOOT = 1.2 * mm


def _draw_arrowhead(c, tip_x, tip_y, angle_deg):
    """A filled arrowhead triangle, tip at (tip_x, tip_y), pointing in the
    direction angle_deg (0 = +X axis, 90 = +Y axis) - the standard ISO/ASME
    dimension-line arrowhead, replacing the earlier plain perpendicular
    tick marks.
    """
    a = math.radians(angle_deg)
    dx, dy = math.cos(a), math.sin(a)
    px, py = -dy, dx  # perpendicular to the arrow direction
    base_x, base_y = tip_x - dx * ARROW_LENGTH, tip_y - dy * ARROW_LENGTH
    half_w = ARROW_WIDTH / 2
    p1 = (base_x + px * half_w, base_y + py * half_w)
    p2 = (base_x - px * half_w, base_y - py * half_w)
    c.setFillColorRGB(0, 0, 0)
    path = c.beginPath()
    path.moveTo(tip_x, tip_y)
    path.lineTo(*p1)
    path.lineTo(*p2)
    path.close()
    c.drawPath(path, fill=1, stroke=0)


def _draw_extension_line_vertical(c, x, object_y, dim_y):
    """A vertical extension line at page-x `x`, running from just short of
    the object point (small gap, so it doesn't touch the geometry) to just
    past the dimension line (small overshoot) - standard drafting style.
    """
    if abs(object_y - dim_y) < 1e-6:
        return
    direction = 1 if object_y > dim_y else -1
    near = object_y - EXTENSION_GAP * direction
    far = dim_y - EXTENSION_OVERSHOOT * direction
    c.line(x, near, x, far)


def _draw_extension_line_horizontal(c, y, object_x, dim_x):
    if abs(object_x - dim_x) < 1e-6:
        return
    direction = 1 if object_x > dim_x else -1
    near = object_x - EXTENSION_GAP * direction
    far = dim_x - EXTENSION_OVERSHOOT * direction
    c.line(near, y, far, y)


# The dimension line's text sits entirely on its own side of the line
# (below it, for a horizontal line - the analogous rotated side for a
# vertical one), never straddling/overlapping the line itself: baseline
# offset = -(small gap + the font's approximate ascent), so the top of the
# text clears the line instead of the old "just above the line" placement,
# which put text on the SAME side as the geometry the extension lines run
# to, cluttering that side. `CHAIN_TEXT_STAGGER` (further along the same
# direction) is what pushes a colliding label onto a second tier.
DIM_TEXT_FONT_SIZE = 6.5
DIM_TEXT_BASE_OFFSET = -(DIM_TEXT_FONT_SIZE * 0.75 + 2.0)


def _dimension_text_box(c, x1, y1, x2, y2, text, orientation, text_offset=0):
    """Approximate page-space bounding box of the text `_draw_dimension_line`
    draws for the SAME arguments - computed separately (rather than having
    that function return it) so every one of its several existing callers
    doesn't have to change how they unpack its result. Lets a caller add
    envelope/chain dimension text to `avoid_segments` the same way callout
    and offset-dimension text already is - it was never tracked there
    before, which is what let another dimension's text land right on top
    of it undetected.
    """
    width = c.stringWidth(text, 'Helvetica', DIM_TEXT_FONT_SIZE)
    height = DIM_TEXT_FONT_SIZE * 1.1
    if orientation == 'horizontal':
        cx = (x1 + x2) / 2
        cy = y1 + DIM_TEXT_BASE_OFFSET + text_offset + height / 2
        return (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)
    cx = (x1 + x2) / 2 - (DIM_TEXT_BASE_OFFSET + text_offset + height / 2)
    cy = (y1 + y2) / 2
    return (cx - height / 2, cy - width / 2, cx + height / 2, cy + width / 2)


def _draw_dimension_line(c, x1, y1, x2, y2, text, orientation, ext_from1=None, ext_from2=None, text_offset=0):
    """A proper linear dimension: extension lines running from the actual
    geometry point (ext_from1/ext_from2, if given - else falls back to the
    dimension line's own endpoints) out past the dimension line by a small
    overshoot, a dimension line with filled arrowheads pointing outward at
    each end (tips at the extension lines), and the value on its own
    (non-line) side of it (see `DIM_TEXT_BASE_OFFSET`).

    `text_offset` shifts the text further from the dimension line (in
    addition to the fixed base offset) - used to stagger consecutive chain
    dimension labels onto alternating tiers when their segments are too
    narrow for full-length text to fit without colliding with a neighbor.
    """
    c.saveState()
    c.setLineWidth(0.3)

    if orientation == 'horizontal':
        f1 = ext_from1 if ext_from1 is not None else (x1, y1)
        f2 = ext_from2 if ext_from2 is not None else (x2, y2)
        _draw_extension_line_vertical(c, x1, f1[1], y1)
        _draw_extension_line_vertical(c, x2, f2[1], y2)

        c.line(x1, y1, x2, y2)
        _draw_arrowhead(c, x1, y1, 180)
        _draw_arrowhead(c, x2, y2, 0)

        c.setFont('Helvetica', DIM_TEXT_FONT_SIZE)
        c.setFillColorRGB(0, 0, 0)
        c.drawCentredString((x1 + x2) / 2, y1 + DIM_TEXT_BASE_OFFSET + text_offset, text)
    else:
        f1 = ext_from1 if ext_from1 is not None else (x1, y1)
        f2 = ext_from2 if ext_from2 is not None else (x2, y2)
        _draw_extension_line_horizontal(c, y1, f1[0], x1)
        _draw_extension_line_horizontal(c, y2, f2[0], x2)

        c.line(x1, y1, x2, y2)
        _draw_arrowhead(c, x1, y1, 270)
        _draw_arrowhead(c, x2, y2, 90)

        c.setFont('Helvetica', DIM_TEXT_FONT_SIZE)
        c.setFillColorRGB(0, 0, 0)
        c.saveState()
        c.translate((x1 + x2) / 2, (y1 + y2) / 2)
        c.rotate(90)
        c.drawCentredString(0, DIM_TEXT_BASE_OFFSET + text_offset, text)
        c.restoreState()
    c.restoreState()


def _draw_point_to_point_dimension(c, p1, p2, text, text_anchor=None):
    """A real dimension line directly between two known page points, with
    arrowheads at both ends and the text rotated to match the line's own
    angle (kept upright by flipping 180deg when the raw angle would
    otherwise render the text upside down) - unlike `_draw_dimension_line`,
    this makes no assumption that the two points are horizontally or
    vertically aligned, since two paired feature points (e.g. a slot's two
    rounded ends) can project to any angle in a given view. Returns the
    line's own two endpoints (x1, y1, x2, y2), so the caller can add the
    REAL drawn segment to the callout router's avoid-segments - an earlier
    version returned the bounding box's own diagonal corners instead, which
    for a roughly horizontal/vertical line is nearly perpendicular to the
    actual drawn line and barely intersects anything, letting callout
    leaders and text cross straight through both this dimension's line and
    its text undetected.

    `text_anchor`, if given, is the exact world-space point the text is
    centered on (still rotated to match the line) - lets a caller place the
    text somewhere deliberately chosen (e.g. `_draw_offset_dimension`
    pushing it to whichever side sits farther from the body, and/or a
    collision-driven search), instead of always the line's own midpoint.
    """
    (x1, y1), (x2, y2) = p1, p2
    angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
    c.saveState()
    c.setLineWidth(0.3)
    c.line(x1, y1, x2, y2)
    _draw_arrowhead(c, x1, y1, angle + 180)
    _draw_arrowhead(c, x2, y2, angle)

    text_angle = angle
    if text_angle > 90 or text_angle < -90:
        text_angle += 180
    tx, ty = text_anchor if text_anchor is not None else ((x1 + x2) / 2, (y1 + y2) / 2)
    c.setFont('Helvetica', DIM_TEXT_FONT_SIZE)
    c.setFillColorRGB(0, 0, 0)
    c.saveState()
    c.translate(tx, ty)
    c.rotate(text_angle)
    c.drawCentredString(0, 0, text)
    c.restoreState()
    c.restoreState()
    return (x1, y1, x2, y2)


def _ray_bbox_exit_distance(origin, direction, bbox_min, bbox_max):
    """How far along `direction` (a unit vector) a ray from `origin` has to
    travel before it leaves the axis-aligned box [bbox_min, bbox_max] -
    standard ray/AABB slab method, used to find where a feature's own
    dimension needs to clear the view's real silhouette, rather than an
    arbitrary fixed offset. Assumes `origin` starts inside (or on) the box,
    which always holds here (a slot's own points always sit within the
    view's own geometry bbox); returns 0.0 for a direction that can't
    increase distance to any bbox face (degenerate zero vector only).
    """
    ox, oy = origin
    dx, dy = direction
    t_exit = None
    for o, d, lo, hi in ((ox, dx, bbox_min[0], bbox_max[0]), (oy, dy, bbox_min[1], bbox_max[1])):
        if abs(d) < 1e-9:
            continue
        t_far = max((lo - o) / d, (hi - o) / d)
        t_exit = t_far if t_exit is None else min(t_exit, t_far)
    return max(t_exit, 0.0) if t_exit is not None else 0.0


def _rotated_text_aabb(c, center, angle_deg, text, font_name, font_size):
    """The axis-aligned bounding box (x0, y0, x1, y1) of `text`, drawn at
    `font_size` centered on `center` and rotated by `angle_deg` - used to
    collision-test dimension text against real drawn segments (geometry,
    other dimension/extension lines, callout leaders and balloons) the same
    way callout text already is, instead of only checking the dimension
    LINE itself and leaving the text free to land on top of anything.
    """
    width = c.stringWidth(text, font_name, font_size)
    height = font_size * 1.1
    hw, hh = width / 2, height / 2
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    xs, ys = [], []
    for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        xs.append(center[0] + dx * cos_a - dy * sin_a)
        ys.append(center[1] + dx * sin_a + dy * cos_a)
    return (min(xs), min(ys), max(xs), max(ys))


# How far past the offset dimension LINE its own text is pushed, further
# outward along the same direction the line was already offset in - a
# print-legibility gap (text must not sit ON the line), same magnitude as
# the fixed "2.0" DIM_TEXT_BASE_OFFSET already uses elsewhere for the same
# purpose, not a value tuned to any particular body.
DIM_TEXT_GAP = 2.0


def _draw_offset_dimension(c, a, b, text, view_bbox_min, view_bbox_max, avoid_segments, start_clearance):
    """A proper "aligned" dimension: a line PARALLEL to the real feature
    (a, b) - whatever angle that happens to be, axis-aligned or not - offset
    outward until it (and its text) clear both the view's own drawn
    geometry AND everything already in `avoid_segments` (other dimension
    lines, callout leaders, balloons - whatever has been drawn on this
    sheet so far), with perpendicular extension lines connecting the real
    points to it. This is the standard drafting technique for dimensioning
    a feature that isn't itself horizontal/vertical, and it subsumes the
    axis-aligned case too (an offset horizontal/vertical line is just this
    same construction at 0/90 degrees) - so every length/width pair uses
    ONE consistent, geometry-driven placement instead of a special-cased
    "only if axis-aligned, and always toward a fixed side" rule.

    Which of the two possible perpendicular directions to offset TOWARD is
    decided by the feature's own position relative to the view's center -
    outward, away from the bulk of the geometry - not a hardcoded side, so
    it adapts to whichever body/feature is actually being drawn. The text
    itself is then pushed further out again, along that SAME outward
    direction (the one of the line's two sides that's genuinely farther
    from the body's own midpoint), rather than an arbitrary fixed side.

    Returns (line_x1, line_y1, line_x2, line_y2, ext1_x1, ext1_y1, ext1_x2,
    ext1_y2, ext2_x1, ext2_y1, ext2_x2, ext2_y2) - every real segment drawn,
    for the caller to add to `avoid_segments` itself (this function only
    READS that list to pick a clear placement, it doesn't mutate it).
    """
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    ux, uy = dx / length, dy / length
    normal = (-uy, ux)  # one of the two unit normals to (a, b)

    mid = ((ax + bx) / 2, (ay + by) / 2)
    view_center = ((view_bbox_min[0] + view_bbox_max[0]) / 2, (view_bbox_min[1] + view_bbox_max[1]) / 2)
    outward = (mid[0] - view_center[0], mid[1] - view_center[1])
    if outward[0] * normal[0] + outward[1] * normal[1] < 0:
        normal = (-normal[0], -normal[1])
    # Try the geometrically-preferred (outward) side FIRST, but the other
    # side is a real fallback, not given up on - the "outward" side is only
    # a good guess about which side has more room; it can just as easily be
    # the side an established fixed-position row (the envelope/chain
    # dimensions) already claims, making the "inward" side actually clearer.
    candidate_normals = (normal, (-normal[0], -normal[1]))

    text_angle = math.degrees(math.atan2(uy, ux))
    if text_angle > 90 or text_angle < -90:
        text_angle += 180

    text_height = DIM_TEXT_FONT_SIZE * 1.1
    # The text is CENTERED on its anchor point (see `_rotated_text_aabb`),
    # so clearing the dimension LINE itself needs the anchor pushed out by
    # half the text's own height, not just DIM_TEXT_GAP on its own - a flat
    # 2pt gap is smaller than that half-height, so the line was landing
    # inside the text's own box (visually a strikethrough) even though the
    # box itself was correctly clear of every OTHER segment.
    text_center_gap = DIM_TEXT_GAP + text_height / 2

    def geometry_at(nx, ny, offset, t):
        a2 = (ax + nx * offset, ay + ny * offset)
        b2 = (bx + nx * offset, by + ny * offset)
        # `t` slides the text along the line itself (0=at a2, 1=at b2,
        # 0.5=the midpoint) - pushing straight out alone can't dodge an
        # obstacle that runs roughly PARALLEL to the offset direction (a
        # long chain-dimension row, say); sliding along the line as well
        # gives the search a real second dimension to route around it in.
        along = (a2[0] + (b2[0] - a2[0]) * t, a2[1] + (b2[1] - a2[1]) * t)
        text_anchor = (along[0] + nx * text_center_gap, along[1] + ny * text_center_gap)
        text_box = _rotated_text_aabb(c, text_anchor, text_angle, text, 'Helvetica', DIM_TEXT_FONT_SIZE)
        return a2, b2, text_anchor, text_box

    # Only the TEXT is a hard "must not overlap" constraint - same tiered
    # priority `_route_callouts` already uses for balloon/leader text: a
    # dimension LINE (like a leader line) crossing another line is normal
    # in a real hand-drafted drawing and stays readable, but two blocks of
    # TEXT landing on top of each other isn't. Requiring the line itself to
    # also avoid every long extension/chain line on the sheet doesn't
    # converge - those lines run the view's full width/height, so no amount
    # of pushing further out in one direction ever escapes them, and it
    # was driving the offset hundreds of mm off the page chasing a zero
    # that doesn't exist. Tracking the fewest-collisions candidate actually
    # tried (rather than requiring zero) is what makes this always
    # terminate at a genuinely reasonable position.
    view_diagonal = math.hypot(view_bbox_max[0] - view_bbox_min[0], view_bbox_max[1] - view_bbox_min[1])

    # `avoid_segments` covers the WHOLE sheet (every dimension row, every
    # balloon, every geometry edge - tens of thousands of tiny segments
    # after HLR + curve smoothing on a complex view), but any text box this
    # search ever tries is small and local - the same spatial index
    # `_route_callouts` uses for the identical reason, so this doesn't
    # rescan the full list on every one of the many candidates tried below.
    segment_grid = _SegmentGrid(avoid_segments)

    def collision_count(text_box):
        nearby = segment_grid.near(text_box, margin=MIN_DIMENSION_CLEARANCE)
        return _count_segment_intersections(text_box, nearby, margin=MIN_DIMENSION_CLEARANCE)

    best = None  # (count, offset, t, nx, ny)
    for nx, ny in candidate_normals:
        base_offset = _ray_bbox_exit_distance(mid, (nx, ny), view_bbox_min, view_bbox_max)
        # Bounded by the view's own diagonal - a genuine, geometry-derived
        # limit on how far "further out" can sensibly mean for THIS
        # drawing, rather than a fixed iteration count picked to suit one
        # test model.
        max_offset = base_offset + view_diagonal
        for t in (0.5, 0.3, 0.7, 0.15, 0.85):
            offset = base_offset + start_clearance
            while offset < max_offset:
                _, _, _, text_box = geometry_at(nx, ny, offset, t)
                count = collision_count(text_box)
                if best is None or count < best[0]:
                    best = (count, offset, t, nx, ny)
                if count == 0:
                    break
                offset += MIN_DIMENSION_CLEARANCE
            if best[0] == 0:
                break
        if best[0] == 0:
            break
    _, offset, t, nx, ny = best
    a2, b2, text_anchor, text_box = geometry_at(nx, ny, offset, t)

    c.saveState()
    c.setLineWidth(0.3)
    _draw_extension_line_free(c, a, a2)
    _draw_extension_line_free(c, b, b2)
    c.restoreState()
    line_ends = _draw_point_to_point_dimension(c, a2, b2, text, text_anchor=text_anchor)
    return line_ends + (a[0], a[1], a2[0], a2[1]) + (b[0], b[1], b2[0], b2[1])


def _draw_extension_line_free(c, p_from, p_to):
    """A straight extension line between two arbitrary page points (not
    constrained to horizontal/vertical, unlike `_draw_extension_line_*`) -
    used by `_draw_offset_dimension`, whose offset direction is itself
    whatever perpendicular the real feature's own angle demands.
    """
    c.saveState()
    c.setLineWidth(0.2)
    c.line(p_from[0], p_from[1], p_to[0], p_to[1])
    c.restoreState()


def _unique_representative_points(points, coord_index, tolerance, prefer_extreme=None):
    """Keeps a real (x, y) point for each unique coordinate group instead
    of just the coordinate value - so a chain
    dimension's extension line can run to the hole's own actual position,
    not to an arbitrary point on the body's outer edge that merely shares
    its X (or Y). Using the bbox edge instead of the real hole position was
    exactly why extension lines looked disconnected from the holes they
    were meant to dimension.

    `prefer_extreme`, if given, is the OTHER coordinate index (1 if
    `coord_index` is 0, or vice versa) - among several points that merge
    into one group, the one with the LARGEST value on that other axis is
    kept as the representative, instead of an arbitrary "whichever sorted
    first" pick. This matters for the vertical (Y) chain specifically: its
    extension lines run horizontally to a fixed column at the box's right
    edge, so a representative from a far-left hole (picked arbitrarily)
    produced a needlessly long horizontal run crossing the whole view,
    while a same-Y hole closer to that edge would have needed almost none -
    visibly asymmetric extension-line lengths between adjacent ordinates
    for no real geometric reason. Preferring the largest-other-coordinate
    point consistently picks whichever real hole is nearest that edge.
    """
    return _grouped_representative_points(points, coord_index, tolerance, prefer_extreme)[0]


def _grouped_representative_points(points, coord_index, tolerance, prefer_extreme=None):
    """Same grouping/representative-picking as `_unique_representative_points`
    (see its docstring), but also returns each group's FULL membership -
    every real point that merged into that one representative, not just the
    winner. Two (or more) features that are genuinely aligned (same X, or
    same Y) all deserve to visibly connect to their shared ordinate's
    extension line, not just whichever one happened to be picked as the
    representative for the dimension tick itself.
    """
    ordered = sorted(points, key=lambda p: p[coord_index])
    representatives = []
    groups = []
    for p in ordered:
        if not representatives or abs(p[coord_index] - representatives[-1][coord_index]) > tolerance:
            representatives.append(p)
            groups.append([p])
        else:
            groups[-1].append(p)
            if prefer_extreme is not None and p[prefer_extreme] > representatives[-1][prefer_extreme]:
                representatives[-1] = p
    return representatives, groups


CHAIN_TEXT_STAGGER = 11.0   # points - vertical/horizontal offset PER extra tier
CHAIN_TEXT_MARGIN = 4.0     # points - minimum gap kept between two labels sharing a tier


CHAIN_LEADER_LENGTH = 14.0   # points - from the dimension line to a moved-out label
CHAIN_LEADER_STEP = 9.0      # points - shift per extra moved-out label that would collide


CHAIN_LABEL_GAP = 9.0        # points - clearance between a moved-out label and the line end


def _draw_chain_label_outside(c, px1, py1, px2, py2, row_pos, text, text_w, orientation,
                              occupied, avoid_segments=(), leads=None):
    """Draw one chain dimension whose text can't fit beside its segment: the
    dimension line and arrows as usual, the text on a straight pointer from
    the middle of the dimension. Candidates span both sides of the row, four
    pointer lengths and sideways shifts, so neighbouring labels fan out in
    steps. Rejected: overlapping another label in `occupied`, a pointer
    running through a label or crossing another pointer in `leads`, or
    leaving the frame; of the rest, the one crossing the fewest lines wins
    (ties go to the nearest). The chosen box and pointer are recorded."""
    leads = [] if leads is None else leads
    height = DIM_TEXT_FONT_SIZE * 1.1
    vertical = orientation == 'vertical'
    if vertical:
        _draw_dimension_line(c, row_pos, py1, row_pos, py2, '', 'vertical',
                             ext_from1=(px1, py1), ext_from2=(px2, py2))
        lo, hi = min(py1, py2), max(py1, py2)
    else:
        _draw_dimension_line(c, px1, row_pos, px2, row_pos, '', 'horizontal',
                             ext_from1=(px1, py1), ext_from2=(px2, py2))
        lo, hi = min(px1, px2), max(px1, px2)
    mid = (lo + hi) / 2

    def box_for(center, offset):
        if vertical:
            return _dimension_text_box(c, row_pos, center, row_pos, center, text, 'vertical', offset)
        return _dimension_text_box(c, center, row_pos, center, row_pos, text, 'horizontal', offset)

    # Candidates: the text on a straight pointer from the middle of its own
    # dimension, on either side of the row, at stepped distances and sideways
    # shifts (so neighbours fan out in stairs rather than sitting in line).
    candidates = []     # (box, lead)
    for length_step in range(4):
        reach = CHAIN_LEADER_LENGTH + length_step * CHAIN_LEADER_STEP
        for shift_step in range(7):
            shift = ((shift_step + 1) // 2) * CHAIN_LEADER_STEP * (1 if shift_step % 2 else -1)
            for sign in (-1, 1):
                center = mid + shift
                if vertical:
                    x_edge = row_pos + sign * reach
                    box = (x_edge - text_w, center - height / 2, x_edge, center + height / 2) if sign < 0 else                           (x_edge, center - height / 2, x_edge + text_w, center + height / 2)
                    lead = (row_pos, mid, x_edge, center)
                else:
                    y0 = row_pos + sign * reach
                    box = (center - text_w / 2, y0, center + text_w / 2, y0 + height) if sign > 0 else                           (center - text_w / 2, y0 - height, center + text_w / 2, y0)
                    lead = (mid, row_pos, center, y0 if sign < 0 else y0)
                    lead = (mid, row_pos, center, box[1] if sign > 0 else box[3])
                candidates.append((box, lead))

    def lead_hits_box(lead, other):
        x1, y1, x2, y2 = lead
        return any(other[0] - 1 <= x1 + (x2 - x1) * t / 20 <= other[2] + 1
                   and other[1] - 1 <= y1 + (y2 - y1) * t / 20 <= other[3] + 1
                   for t in range(21))

    def leads_cross(l1, l2):
        def side(a, b, p):
            return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
        a1, a2, b1, b2 = l1[:2], l1[2:], l2[:2], l2[2:]
        return side(a1, a2, b1) * side(a1, a2, b2) < 0 and side(b1, b2, a1) * side(b1, b2, a2) < 0

    best, best_score = None, None
    for index, (box, lead) in enumerate(candidates):
        if any(_boxes_overlap(box, o, margin=1.0) for o in occupied):
            continue
        if box[2] > PAGE_SIZE[0] - MARGIN or box[0] < MARGIN:
            continue                       # would leave the drawing frame
        if any(lead_hits_box(lead, o) for o in occupied):
            continue                       # pointer would run through another label
        if any(leads_cross(lead, other) for other in leads):
            continue
        crossings = _count_segment_intersections(box, list(avoid_segments), margin=1.0)
        score = (crossings, index)
        if best_score is None or score < best_score:
            best, best_score = (box, lead), score
    if best is None:                       # nothing free: fall back to the first spot
        best = candidates[0]
    box, lead = best
    occupied.append(box)
    leads.append(lead)

    c.saveState()
    c.setLineWidth(0.3)
    c.setFont('Helvetica', DIM_TEXT_FONT_SIZE)
    c.line(*lead)
    c.setFillColorRGB(0, 0, 0)
    c.circle(lead[0], lead[1], 0.8, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.rect(box[0] - 0.5, box[1], box[2] - box[0] + 1, box[3] - box[1], stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)
    if vertical:
        (c.drawRightString if box[2] <= row_pos else c.drawString)(
            box[2] if box[2] <= row_pos else box[0], (box[1] + box[3]) / 2 - DIM_TEXT_FONT_SIZE * 0.35, text)
    else:
        c.drawCentredString((box[0] + box[2]) / 2, box[1] + 1.5, text)
    c.restoreState()
    return box


def _draw_chain_dimensions(c, ordered_points, row_pos, orientation, tol_fn=None, avoid_segments=()):
    """Chain (hole-to-hole spacing) dimensioning: one dimension line per gap
    between consecutive unique feature positions, stacked on their own row
    outside the overall envelope dimension so the two don't collide.
    `ordered_points` is a list of (model_coord_mm, page_x, page_y) already
    deduplicated and sorted along the dimensioned axis - the real-world span
    is computed directly from `model_coord_mm`, not by reverse-scaling page
    pixels.

    Consecutive segments can be narrower than their own dimension text
    (common for a tightly-spaced bolt pattern, especially once a tolerance
    figure widens the text) - a real per-tier greedy placement (not just a
    fixed alternating 2-tier split) keeps every label clear of its
    neighbors: each label goes on the lowest tier whose most-recently-placed
    label doesn't overlap it, adding a new tier only when every existing one
    is still occupied there. A fixed odd/even alternation broke down for
    THREE OR MORE consecutive narrow segments (two of them would land on the
    same tier and still collide) - this handles any run length.
    """
    # Each label tries the line's usual side first and then the opposite
    # side, tier by tier (own side tier 0, other side tier 0, own side tier 1,
    # ...), taking the first slot not already occupied where it lands.
    flip_offset = 2.0 - DIM_TEXT_BASE_OFFSET      # puts the text on the other side of the line
    outside_boxes = []  # boxes of labels already moved out on a leader
    leads = []          # their pointer lines
    slots = {}          # (side, tier) -> page-position where that slot's last label ends
    text_boxes = []
    for (model1, px1, py1), (model2, px2, py2) in zip(ordered_points, ordered_points[1:]):
        span = abs(model2 - model1)
        text = units.format_dimension(span, tol_fn(span) if tol_fn else None)
        text_w = stringWidth(text, 'Helvetica', 6.5)
        center = (px1 + px2) / 2 if orientation == 'horizontal' else (py1 + py2) / 2
        start, end = center - text_w / 2, center + text_w / 2

        seg_len = abs(py2 - py1) if orientation == 'vertical' else abs(px2 - px1)
        if text_w + 2 * ARROW_LENGTH > seg_len:
            # Doesn't fit beside its own segment: draw the dimension without
            # text and put the text clear of the line on a leader instead.
            text_boxes.append(_draw_chain_label_outside(
                c, px1, py1, px2, py2, row_pos, text, text_w, orientation,
                outside_boxes + text_boxes, avoid_segments, leads))
            outside_boxes.append(text_boxes[-1])
            continue

        tier = 0
        while True:
            for side in (0, 1):
                if slots.get((side, tier), -1e9) <= start - CHAIN_TEXT_MARGIN:
                    break
            else:
                tier += 1
                continue
            break
        slots[(side, tier)] = end
        offset = -CHAIN_TEXT_STAGGER * tier if side == 0 else flip_offset + CHAIN_TEXT_STAGGER * tier

        if orientation == 'horizontal':
            _draw_dimension_line(
                c, px1, row_pos, px2, row_pos, text, 'horizontal',
                ext_from1=(px1, py1), ext_from2=(px2, py2), text_offset=offset,
            )
            text_boxes.append(_dimension_text_box(c, px1, row_pos, px2, row_pos, text, 'horizontal', offset))
        else:
            _draw_dimension_line(
                c, row_pos, py1, row_pos, py2, text, 'vertical',
                ext_from1=(px1, py1), ext_from2=(px2, py2), text_offset=offset,
            )
            text_boxes.append(_dimension_text_box(c, row_pos, py1, row_pos, py2, text, 'vertical', offset))
    return text_boxes


LEADER_OFFSET = 9 * mm
LEADER_SHOULDER = 3 * mm
ARROW_TOUCH_OVERSHOOT = 0.5 * mm
LEADER_ARROW_LENGTH = 1.8 * mm
LEADER_ARROW_WIDTH = 0.7 * mm
CENTER_CROSS_SIZE = 1.2 * mm
# Minimum on-page gap between two independently-placed dimension lines
# before one is pushed further out to avoid the other (see
# `_draw_offset_dimension`'s clearance search) - a print-legibility
# constant like CENTER_CROSS_SIZE above, not a distance tuned to any
# specific body's own geometry.
MIN_DIMENSION_CLEARANCE = 2 * mm


def _draw_center_cross(c, x, y):
    """A small +-mark at a feature's own true point (its hole center, or a
    fillet/pocket's surface point) - drawn in addition to its numbered
    balloon+arrow, not instead of it, so the exact feature location is
    still visible at a glance even where the balloon itself sits some
    distance away."""
    c.saveState()
    c.setLineWidth(0.3)
    c.line(x - CENTER_CROSS_SIZE, y, x + CENTER_CROSS_SIZE, y)
    c.line(x, y - CENTER_CROSS_SIZE, x, y + CENTER_CROSS_SIZE)
    c.restoreState()
CALLOUT_LINE_HEIGHT = 7.2
# This only needs to absorb genuine floating-point noise between two STEP
# points that are really the SAME feature position (typically well under
# 0.001mm) - it must NOT merge two real, distinct feature positions that
# happen to be close together (e.g. two holes 0.072mm apart due to a real
# modeled offset), since that silently drops a real, if small, dimension
# instead of just deduplicating noise. Previously 0.5mm, which was wide
# enough to eat real small-but-genuine gaps between distinct features.
CHAIN_TOLERANCE_MM = 0.01
ENVELOPE_ROW_FRACTION = 0.25  # outermost dimension row (closest to the box border)
CHAIN_ROW_FRACTION = 0.65     # innermost dimension row (closest to the geometry)


CALLOUT_FONT = 'Helvetica'
CALLOUT_FONT_SIZE = 6.5
# Minimum clear gap kept between callout text and anything else - another
# callout's text, or any drawn line (geometry, dimension lines, leaders).
# Widened from the original 1.0/1.2mm for genuinely comfortable margins,
# not just "not touching".
CALLOUT_TEXT_MARGIN = 2.0 * mm
CALLOUT_LINE_MARGIN = 2.0 * mm
# Candidate leader directions to try, in degrees (0 = +X/right, 90 = +Y/up).
# A wide spread (not just the 4 diagonals) gives the router real room to
# dodge other callouts instead of being locked to up-left/up-right. Finer
# 10-degree steps (rather than the previous coarser spread) give a real
# clean (no line/text overlap at all) placement a much better chance of
# existing somewhere in the search, instead of jumping straight from one
# crowded angle to another 20-45 degrees away.
_CANDIDATE_ANGLES_DEG = tuple(
    sign * base for base in range(10, 180, 10) for sign in (1, -1)
) + (0, 180)
_CANDIDATE_DISTANCES = tuple(LEADER_OFFSET + i * 5 * mm for i in range(16))  # up to ~89mm out


def _text_block_bbox(elbow_x, elbow_y, shoulder_x, lines, ox_sign):
    """The page-space bounding box the callout text will occupy, used for
    collision checks before anything is actually drawn.
    """
    widths = [stringWidth(line, CALLOUT_FONT, CALLOUT_FONT_SIZE) for line in lines] or [0]
    max_w = max(widths)
    text_gap = 1 * mm
    top = elbow_y + (len(lines) - 1) * CALLOUT_LINE_HEIGHT / 2 + CALLOUT_FONT_SIZE * 0.35
    bottom = top - len(lines) * CALLOUT_LINE_HEIGHT
    if ox_sign > 0:
        left, right = shoulder_x + text_gap, shoulder_x + text_gap + max_w
    else:
        left, right = shoulder_x - text_gap - max_w, shoulder_x - text_gap
    return (left, bottom, right, top)


def _boxes_overlap(a, b, margin=CALLOUT_TEXT_MARGIN):
    return not (a[2] + margin < b[0] or b[2] + margin < a[0]
                or a[3] + margin < b[1] or b[3] + margin < a[1])


BALLOON_RADIUS = 3.2 * mm
BALLOON_FONT = 'Helvetica-Bold'
BALLOON_FONT_SIZE = 6.5


def _balloon_bbox(center_x, center_y, label):
    """The page-space bounding box a numbered balloon will occupy, centered
    at (center_x, center_y) - a plain circle for a single number, widening
    into a stadium (rounded rect with circular ends) for a multi-character
    label (a merged range like "4-7"), same idea as `_text_block_bbox` but
    for a balloon instead of a text block.
    """
    text_w = stringWidth(label, BALLOON_FONT, BALLOON_FONT_SIZE)
    half_w = max(BALLOON_RADIUS, text_w / 2 + 1.3 * mm)
    return (center_x - half_w, center_y - BALLOON_RADIUS, center_x + half_w, center_y + BALLOON_RADIUS)


def _draw_balloon(c, center_x, center_y, label):
    """A numbered balloon: a plain circle for one number, or a stadium
    (rounded rectangle with semicircular ends) for a merged range/list -
    the same shape distinction a real drafting legend uses for "this one
    marker actually covers several numbered features at the same spot."
    """
    half_w = max(BALLOON_RADIUS, stringWidth(label, BALLOON_FONT, BALLOON_FONT_SIZE) / 2 + 1.3 * mm)
    c.saveState()
    c.setLineWidth(0.4)
    c.setFillColorRGB(1, 1, 1)
    if half_w <= BALLOON_RADIUS + 0.01:
        c.circle(center_x, center_y, BALLOON_RADIUS, stroke=1, fill=1)
    else:
        straight = half_w - BALLOON_RADIUS
        path = c.beginPath()
        path.moveTo(center_x - straight, center_y - BALLOON_RADIUS)
        path.lineTo(center_x + straight, center_y - BALLOON_RADIUS)
        path.arcTo(center_x + straight - BALLOON_RADIUS, center_y - BALLOON_RADIUS,
                   center_x + straight + BALLOON_RADIUS, center_y + BALLOON_RADIUS, startAng=-90, extent=180)
        path.lineTo(center_x - straight, center_y + BALLOON_RADIUS)
        path.arcTo(center_x - straight - BALLOON_RADIUS, center_y - BALLOON_RADIUS,
                   center_x - straight + BALLOON_RADIUS, center_y + BALLOON_RADIUS, startAng=90, extent=180)
        path.close()
        c.drawPath(path, stroke=1, fill=1)
    c.setFillColorRGB(0, 0, 0)
    c.setFont(BALLOON_FONT, BALLOON_FONT_SIZE)
    c.drawCentredString(center_x, center_y - BALLOON_FONT_SIZE * 0.35, label)
    c.restoreState()


def _format_balloon_label(numbers):
    """A sorted, deduplicated list of global feature numbers sharing one
    balloon (from the same-spot merge) -> its display label: a bare number
    when there's only one, a "a-b" range when they're consecutive, else a
    comma list.
    """
    nums = sorted(set(numbers))
    if len(nums) == 1:
        return str(nums[0])
    if nums == list(range(nums[0], nums[-1] + 1)):
        return f'{nums[0]}-{nums[-1]}'
    return ','.join(str(n) for n in nums)


def _segments_intersect(p1, p2, p3, p4):
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])
    return ccw(p1, p3, p4) != ccw(p2, p3, p4) and ccw(p1, p2, p3) != ccw(p1, p2, p4)


def _segment_intersects_box(x1, y1, x2, y2, box):
    bx0, by0, bx1, by1 = box
    if max(x1, x2) < bx0 or min(x1, x2) > bx1 or max(y1, y2) < by0 or min(y1, y2) > by1:
        return False
    if bx0 <= x1 <= bx1 and by0 <= y1 <= by1:
        return True
    if bx0 <= x2 <= bx1 and by0 <= y2 <= by1:
        return True
    corners = ((bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1))
    for i in range(4):
        if _segments_intersect((x1, y1), (x2, y2), corners[i], corners[(i + 1) % 4]):
            return True
    return False


def _box_intersects_any_segment(box, segments, margin=CALLOUT_LINE_MARGIN):
    """True if the (already margin-expanded) box crosses or touches any of
    `segments` - used to keep callout TEXT off of any drawn line (geometry
    edges, dimension lines, other leaders), not just other callout text.
    Leader lines themselves are still free to cross geometry/each other -
    only the text block is checked here.
    """
    return _count_segment_intersections(box, segments, margin) > 0


# Coarse bucket size for `_SegmentGrid` - large enough that a typical
# callout candidate box (a few mm across) touches only a handful of cells,
# small enough that each cell's own segment list stays short. Not sensitive
# to any particular body's size (segments near a query box are found the
# same way regardless of how many total cells that spans).
_SEGMENT_GRID_CELL_SIZE = 15 * mm


class _SegmentGrid:
    """A coarse spatial hash of page-space (x1, y1, x2, y2) line segments,
    so a collision check against one small box only has to scan segments
    in nearby cells instead of the entire list - `_route_callouts`'s own
    candidate search was checking every candidate box against every single
    geometry segment on the sheet (a curve-heavy view, after HLR + the
    Catmull-Rom smoothing that turns each curve into many short segments,
    can carry tens of thousands), which is what made it slow - not the
    number of callouts itself, which is normally small. This is a plain
    algorithmic fix (same results, fewer comparisons), not a tuning knob.
    """

    def __init__(self, segments=()):
        self.cells = {}
        for seg in segments:
            self.add(seg)

    def _cell_range(self, x0, y0, x1, y1):
        cs = _SEGMENT_GRID_CELL_SIZE
        return (int(x0 // cs), int(x1 // cs) + 1, int(y0 // cs), int(y1 // cs) + 1)

    def add(self, seg):
        x1, y1, x2, y2 = seg
        cx0, cx1, cy0, cy1 = self._cell_range(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        for cx in range(cx0, cx1):
            for cy in range(cy0, cy1):
                self.cells.setdefault((cx, cy), []).append(seg)

    def near(self, box, margin=0.0):
        """Every distinct segment whose own cell(s) overlap `box` (expanded
        by `margin`) - a superset of what could possibly intersect it, safe
        to then run the exact `_segment_intersects_box` check against.
        """
        x0, y0, x1, y1 = box
        cx0, cx1, cy0, cy1 = self._cell_range(x0 - margin, y0 - margin, x1 + margin, y1 + margin)
        seen = set()
        result = []
        for cx in range(cx0, cx1):
            for cy in range(cy0, cy1):
                for seg in self.cells.get((cx, cy), ()):
                    key = id(seg)
                    if key not in seen:
                        seen.add(key)
                        result.append(seg)
        return result


def _count_segment_intersections(box, segments, margin=CALLOUT_LINE_MARGIN):
    """How many of `segments` cross the (margin-expanded) box - used to let
    the router's crowded-view fallback pick the LEAST-bad candidate it
    tried (fewest line crossings) instead of whichever one it happened to
    try first that satisfied only the weaker bounds/no-text-overlap
    constraints. Searching farther/wider is pointless if the fallback
    never reconsiders once it has any acceptable-enough candidate.
    """
    expanded = (box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin)
    return sum(1 for (x1, y1, x2, y2) in segments if _segment_intersects_box(x1, y1, x2, y2, expanded))


def _closest_point_on_segment(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-12:
        return x1, y1, math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    cx, cy = x1 + t * dx, y1 + t * dy
    return cx, cy, math.hypot(px - cx, py - cy)


def _snap_to_nearest_segment(px, py, segments, max_dist):
    """The closest point to (px, py) lying ON one of `segments`, as long as
    it's within `max_dist` - otherwise (px, py) unchanged. Used to pull a
    point-feature's leader anchor onto the actual rendered outline instead
    of leaving it in the gap between two nearby curves.
    """
    best = None
    for x1, y1, x2, y2 in segments:
        cx, cy, d = _closest_point_on_segment(px, py, x1, y1, x2, y2)
        if d > max_dist:
            continue
        if best is None or d < best[2]:
            best = (cx, cy, d)
    return (best[0], best[1]) if best else (px, py)


def _route_callouts(items, region, center, avoid_segments=()):
    """Greedy candidate-search leader router: for each callout (in order),
    try increasingly distant offsets across a spread of angles - preferring
    angles that point away from the view's own geometry center of mass
    first - and accept the first placement whose text bounding box neither
    leaves `region` (the area reserved for callouts, excluding the
    dimension-line strip near the box edges), nor overlaps a
    previously-placed callout's box, NOR crosses any line in
    `avoid_segments` (geometry edges, dimension/extension lines, and other
    callouts' own leader lines - text must not sit on top of ANY of them,
    though the leader lines themselves are free to cross geometry/each
    other, same as a real hand-drafted leader would). This is what actually
    avoids the leader-vs-leader collisions a fixed left/right split
    couldn't: two callouts that would naturally land in the same place now
    displace each other by distance/angle instead of one just overwriting
    the other - and now also displace off of any line, not just other text.

    `items`: list of (px, py, payload, radius_page, is_balloon) in page
    coordinates. `payload` is a list of text lines when `is_balloon` is
    False, or a balloon label string (e.g. "12" or "4-7") when True - a
    balloon has no separate horizontal "shoulder" segment, since a numbered
    balloon sits right at the end of its own leader, not offset beside it
    like a multi-line text block.
    `radius_page` is the visible circle's radius in page units for a round
    hole (so the leader's arrow touches the circle's actual edge, the way
    Fusion/SolidWorks draw a diameter callout, not the center point buried
    inside it) or 0 for a point feature (a fillet/chamfer's leader anchors
    exactly at its surface point - there's no circle to stand off from).
    `region`: (x0, y0, x1, y1) the callout text must stay within.
    `center`: (cx, cy) the view's geometry centroid, for outward-bias.
    `avoid_segments`: list of (x1, y1, x2, y2) lines the text must clear -
    mutated in place as each callout's own leader is added, so later
    callouts also avoid earlier ones' leader lines.
    Returns a list of dicts: {anchor, is_balloon, lines/label, elbow,
    shoulder_x, ox_sign}.
    """
    rx0, ry0, rx1, ry1 = region
    cx, cy = center
    placed_boxes = []
    segment_grid = _SegmentGrid(avoid_segments)
    placements = []

    for px, py, payload, radius_page, is_balloon in items:
        vx, vy = px - cx, py - cy
        vlen = math.hypot(vx, vy) or 1.0
        vx, vy = vx / vlen, vy / vlen
        candidate_angles = sorted(
            _CANDIDATE_ANGLES_DEG,
            key=lambda a: -(math.cos(math.radians(a)) * vx + math.sin(math.radians(a)) * vy),
        )

        # Aim the tip slightly INSIDE the mathematical circle radius, not
        # exactly on it: the drawn circle is a ~64-point sampled polyline
        # with its own stroke width, so a tip placed at the exact computed
        # radius could still land a hair short of the visibly rendered
        # line at some angles, leaving a visible gap between the arrowhead
        # and the circle it's supposed to touch.
        touch_radius = max(radius_page - ARROW_TOUCH_OVERSHOOT, 0) if radius_page > 0 else 0

        def candidate_at(dist, angle):
            rad = math.radians(angle)
            dx, dy = math.cos(rad), math.sin(rad)
            # The leader starts at the circle's edge (radius_page out from
            # the true center along the same direction it then travels in),
            # not the center itself, when this is a circular hole.
            anchor_x, anchor_y = px + dx * touch_radius, py + dy * touch_radius
            elbow_x, elbow_y = anchor_x + dx * dist, anchor_y + dy * dist
            ox_sign = 1 if dx >= 0 else -1
            if is_balloon:
                shoulder_x = elbow_x
                box = _balloon_bbox(elbow_x, elbow_y, payload)
            else:
                shoulder_x = elbow_x + math.copysign(LEADER_SHOULDER, dx if dx != 0 else ox_sign)
                box = _text_block_bbox(elbow_x, elbow_y, shoulder_x, payload, ox_sign)
            return (anchor_x, anchor_y), (elbow_x, elbow_y), shoulder_x, ox_sign, box

        chosen = None
        # Tiered constraints, strictest first: staying on the sheet
        # (`region`) and never overlapping another callout's TEXT are hard
        # requirements that must never be violated, no matter how crowded
        # the view gets. Not crossing a line is the goal but gets relaxed
        # before either of those - a callout whose leader crosses a
        # hidden edge is far less broken than one whose text runs off the
        # page or sits on top of another callout's text.
        best_fallback = None       # (crossing_count, candidate) - least-bad seen so far
        best_fallback_count = None
        for dist in _CANDIDATE_DISTANCES:
            for angle in candidate_angles:
                anchor, elbow, shoulder_x, ox_sign, box = candidate_at(dist, angle)
                if not (rx0 <= box[0] and box[2] <= rx1 and ry0 <= box[1] and box[3] <= ry1):
                    continue
                if any(_boxes_overlap(box, pb) for pb in placed_boxes):
                    continue
                candidate = (anchor, elbow, shoulder_x, ox_sign, box)
                nearby = segment_grid.near(box, margin=CALLOUT_LINE_MARGIN)
                crossings = _count_segment_intersections(box, nearby, margin=CALLOUT_LINE_MARGIN)
                if crossings == 0:
                    chosen = candidate
                    break
                # Keep searching (farther distances, other angles) rather
                # than settling for the first acceptable-enough candidate -
                # a callout crowded by surrounding geometry (e.g. a wide
                # multi-line counterbore stack near concentric bore rings)
                # can often find a candidate crossing FEWER lines a little
                # farther out, which searching wider distances only helps
                # with if the fallback actually reconsiders.
                if best_fallback_count is None or crossings < best_fallback_count:
                    best_fallback_count = crossings
                    best_fallback = candidate
            if chosen:
                break

        if chosen is None:
            # No candidate avoided every line, but at least one stayed on
            # the sheet and clear of other callouts' text - use the one
            # crossing the FEWEST lines among everything tried, instead of
            # blindly maximizing distance (which let callouts drift off the
            # page entirely) or settling for whichever was tried first
            # (which ignored every farther/wider candidate that might have
            # crossed less).
            if best_fallback:
                chosen = best_fallback
            else:
                # Nothing even respected the region (pathologically crowded
                # or a tiny view) - EVERY candidate in the whole search grid
                # either left the region or overlapped an already-placed
                # callout's box. Before giving up, try progressively
                # farther distances with the region constraint DROPPED but
                # the no-overlap-with-a-placed-callout constraint kept - a
                # balloon sitting a little outside the nominal callout
                # region is still readable; two balloons stacked directly
                # on top of each other (what happened here before this
                # fallback existed, in a dense cluster of many close-
                # together same-size features) is not.
                extended_distances = _CANDIDATE_DISTANCES + tuple(
                    _CANDIDATE_DISTANCES[-1] + i * 5 * mm for i in range(1, 40)
                )
                no_overlap_candidate = None
                for dist in extended_distances:
                    for angle in candidate_angles:
                        candidate = candidate_at(dist, angle)
                        if not any(_boxes_overlap(candidate[4], pb) for pb in placed_boxes):
                            no_overlap_candidate = candidate
                            break
                    if no_overlap_candidate:
                        break

                if no_overlap_candidate:
                    chosen = no_overlap_candidate
                else:
                    # Truly nothing worked even with unlimited distance
                    # (shouldn't happen in practice) - clamp the farthest
                    # outward candidate back into the region as an
                    # absolute last resort.
                    anchor, elbow, shoulder_x, ox_sign, box = candidate_at(_CANDIDATE_DISTANCES[-1], candidate_angles[0])
                    dx = max(rx0 - box[0], 0) + min(rx1 - box[2], 0)
                    dy = max(ry0 - box[1], 0) + min(ry1 - box[3], 0)
                    anchor = (anchor[0] + dx, anchor[1] + dy)
                    elbow = (elbow[0] + dx, elbow[1] + dy)
                    shoulder_x = shoulder_x + dx
                    box = (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)
                    chosen = (anchor, elbow, shoulder_x, ox_sign, box)

        anchor, elbow, shoulder_x, ox_sign, box = chosen
        placed_boxes.append(box)
        # This callout's own leader (anchor->elbow->shoulder) becomes a line
        # later callouts' TEXT must also avoid - a leader is allowed to
        # cross other leaders/geometry, but no one's text should sit on it.
        segment_grid.add((anchor[0], anchor[1], elbow[0], elbow[1]))
        if not is_balloon:
            segment_grid.add((elbow[0], elbow[1], shoulder_x, elbow[1]))
        placements.append({
            'anchor': anchor, 'payload': payload, 'is_balloon': is_balloon,
            'elbow': elbow, 'shoulder_x': shoulder_x, 'ox_sign': ox_sign, 'box': box,
        })

    return placements


def _draw_leader_callout(c, placement):
    """Draw one already-routed leader callout: an arrowhead touching the
    feature point (or, for a round hole, the circle's actual edge), then
    either a bent leader (diagonal + horizontal shoulder) out to a text
    block, or a straight leader into a numbered balloon - depending on
    `placement['is_balloon']`.
    """
    anchor_x, anchor_y = placement['anchor']
    elbow_x, elbow_y = placement['elbow']
    ox_sign = placement['ox_sign']
    payload = placement['payload']

    c.saveState()
    c.setLineWidth(0.3)
    c.line(anchor_x, anchor_y, elbow_x, elbow_y)
    _draw_arrowhead(c, anchor_x, anchor_y, math.degrees(math.atan2(elbow_y - anchor_y, elbow_x - anchor_x)) + 180)

    if placement['is_balloon']:
        _draw_balloon(c, elbow_x, elbow_y, payload)
    else:
        shoulder_x = placement['shoulder_x']
        c.line(elbow_x, elbow_y, shoulder_x, elbow_y)
        c.setFont(CALLOUT_FONT, CALLOUT_FONT_SIZE)
        text_x = shoulder_x + (1 * mm if ox_sign > 0 else -1 * mm)
        align = c.drawString if ox_sign > 0 else c.drawRightString
        text_y = elbow_y + (len(payload) - 1) * CALLOUT_LINE_HEIGHT / 2
        for line in payload:
            align(text_x, text_y, line)
            text_y -= CALLOUT_LINE_HEIGHT
    c.restoreState()


# Catmull-Rom control points beyond this angle (degrees, measured at the
# shared point between the incoming and outgoing segment) are treated as a
# genuine sharp corner, not a facet of a smooth curve - drawn as a straight
# lineTo instead of curved through. Without this, a real corner (e.g. where
# a curved fillet face meets a flat one) gets Catmull-Rom's un-clamped
# tangent blending applied across the discontinuity anyway, which can
# overshoot into a visible loop/spike right at the corner - confirmed on a
# real render (a small flag-shaped loop appearing on an otherwise straight
# edge at exactly such a junction).
SMOOTH_CORNER_ANGLE_DEG = 35.0
# How close a polyline's own start/end points must be (page units) to treat
# it as a CLOSED curve (a full circle/ellipse silhouette, whole and
# unbroken) rather than an open arc - closed curves need Catmull-Rom's
# tangent blending to wrap around the seam using the curve's own opposite
# end as context, not clamp there; clamping at a seam that's actually the
# same point produces a visible flat "dent" right at that one spot (also
# confirmed on a real render).
CLOSED_CURVE_TOLERANCE = 0.05


def _add_smooth_path(path, pts):
    """Trace `pts` (already in page coordinates) as a smooth curve using
    Catmull-Rom-to-Bezier conversion, instead of straight `lineTo` segments
    between them - since switching the HLR engine to a polygon/mesh-based
    one (see project_view()'s own docstring), every edge - including true
    circles and other smooth curves - comes out as many small straight
    mesh-triangle-boundary segments rather than one polyline sampled from
    an exact analytic curve. Connecting those with straight lines faithfully
    reproduces the underlying mesh facets, which is visibly "jagged" at
    normal print/zoom levels for anything curved (most noticeably round
    holes and fillets). Refitting a smooth curve THROUGH the same sample
    points, purely for rendering, restores a genuinely smooth (curved, not
    polygonal) vector outline in the final PDF without touching the
    underlying HLR visibility computation at all - the points/positions are
    unchanged, only how they're connected on the page.

    Two failure modes an earlier, simpler version of this had (both
    confirmed on real renders, not hypothetical) are guarded against here:
    a visible flat "dent" at a closed circle's own seam point (fixed by
    detecting a closed curve and wrapping the Catmull-Rom context around
    it, instead of clamping as if the seam were a dead end), and a visible
    loop/spike artifact at a genuine sharp corner (fixed by measuring the
    angle at each interior point and falling back to a plain straight
    `lineTo` there instead of curving through a real discontinuity -
    Catmull-Rom's un-clamped tangent blending overshoots badly when the
    points on either side of a corner aren't actually part of one smooth
    curve).
    """
    n = len(pts)
    if n < 3:
        for p in pts[1:]:
            path.lineTo(*p)
        return

    closed = math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < CLOSED_CURVE_TOLERANCE

    def corner_angle_deg(a, b, c):
        v1x, v1y = b[0] - a[0], b[1] - a[1]
        v2x, v2y = c[0] - b[0], c[1] - b[1]
        len1, len2 = math.hypot(v1x, v1y), math.hypot(v2x, v2y)
        if len1 < 1e-9 or len2 < 1e-9:
            return 0.0
        cos_a = max(-1.0, min(1.0, (v1x * v2x + v1y * v2y) / (len1 * len2)))
        return math.degrees(math.acos(cos_a))

    for i in range(n - 1):
        p1 = pts[i]
        p2 = pts[i + 1]
        if closed:
            p0 = pts[i - 1] if i > 0 else pts[-2]
            p3 = pts[i + 2] if i + 2 < n else pts[1]
        else:
            p0 = pts[i - 1] if i > 0 else pts[i]
            p3 = pts[i + 2] if i + 2 < n else pts[i + 1]

        # A sharp turn AT p1 or AT p2 means at least one side of this
        # segment isn't part of a smooth run - draw it straight instead of
        # risking a Catmull-Rom overshoot across the discontinuity.
        sharp = False
        if p0 != p1 and corner_angle_deg(p0, p1, p2) > SMOOTH_CORNER_ANGLE_DEG:
            sharp = True
        elif p3 != p2 and corner_angle_deg(p1, p2, p3) > SMOOTH_CORNER_ANGLE_DEG:
            sharp = True

        if sharp:
            path.lineTo(p2[0], p2[1])
            continue

        c1 = (p1[0] + (p2[0] - p0[0]) / 6.0, p1[1] + (p2[1] - p0[1]) / 6.0)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6.0, p2[1] - (p3[1] - p1[1]) / 6.0)
        path.curveTo(c1[0], c1[1], c2[0], c2[1], p2[0], p2[1])


def _stroke_edge(c, line, to_page):
    """Stroke one projected edge: as its exact vector segments (lines and
    Bezier curves, so dashes follow the curve) when it carries them, else
    the smoothed polyline."""
    path = c.beginPath()
    segs = getattr(line, 'segs', None)
    if segs:
        import vector_hlr
        vector_hlr.trace_segs(path, segs, to_page)
    else:
        pts = [to_page(p) for p in line]
        path.moveTo(*pts[0])
        _add_smooth_path(path, pts)
    c.drawPath(path)


def _draw_view(c, view, origin_x, origin_y, box_w, box_h, label, callouts=None,
               length_dims=None, cross_marks=None, dimension=False, chain_dimension=False,
               chain_points=None, tol_fn=None, chain_points_x=None, chain_points_y=None):
    """Draw one view's visible (solid) and hidden (dashed) polylines,
    scaled to fit within box_w x box_h, anchored at (origin_x, origin_y)
    (bottom-left of the box).

    `callouts` is an optional list of (x, y, label, radius_mm, is_balloon)
    in the view's own local coordinates - each routed by `_route_callouts`
    (a greedy candidate search that displaces callouts around each other
    and away from the dimension-line strip, rather than a fixed direction)
    and drawn as a real leader-line callout (arrowhead at the feature - or,
    for a round hole (radius_mm > 0), at the actual edge of its circle -
    ending in a numbered balloon). `cross_marks` is a list of (x, y) - one
    per physical instance, same points the callouts/balloons are built
    from - each drawn as a small +-cross at the feature's own exact point,
    in addition to (not instead of) its balloon+arrow, which usually sits
    some distance away.

    `dimension` adds an overall width/height envelope dimension line along
    the bottom/right of the box. `chain_dimension` additionally adds
    hole-to-hole spacing dimensions (one segment per gap between
    consecutive unique `chain_points` positions) on an inner row - intended
    for the TOP view only, where bolt-pattern spacing is most meaningful.
    """
    all_lines = view['visible'] + view['hidden']
    min_x, min_y, max_x, max_y = _bbox_of_polylines(all_lines)
    span_x = max(max_x - min_x, 1e-6)
    span_y = max(max_y - min_y, 1e-6)

    # Wider than the original 10mm - callouts and dimension lines both need
    # to live in this margin without fighting each other for space. Widened
    # again from 16mm since the envelope and chain dimension rows' own text
    # (now drawn below/beside their line, not straddling it - see
    # DIM_TEXT_BASE_OFFSET) needs enough absolute room between the two rows,
    # and between the outer row and the sheet border, to not crowd each
    # other even with a wide tolerance-suffixed dimension string.
    padding = 20 * mm
    scale = min((box_w - 2 * padding) / span_x, (box_h - 2 * padding) / span_y)
    scale = max(scale, 1e-6)

    # `scale` is the SMALLER of the two axis-fit ratios, so only the
    # constraining axis actually fills its full (box - 2*padding) span -
    # the other axis's drawn geometry is smaller than the room available
    # for it (e.g. a wide, short view in a tall box leaves extra vertical
    # room). Anchoring both axes at the same fixed `padding` put ALL of
    # that leftover space on one side (right/top) instead of splitting it
    # evenly - the view visibly sat off-center in its own box. Centering
    # it: half of whatever room is left over after the padding and the
    # actually-drawn span is added as an extra, equal offset on each side.
    extra_x = max(0.0, (box_w - 2 * padding - span_x * scale) / 2)
    extra_y = max(0.0, (box_h - 2 * padding - span_y * scale) / 2)

    def to_page(pt):
        x = origin_x + padding + extra_x + (pt[0] - min_x) * scale
        y = origin_y + padding + extra_y + (pt[1] - min_y) * scale
        return x, y

    # Collected as we draw, in page coordinates, so routed callout TEXT can
    # be kept off of every line actually on the sheet - geometry edges,
    # dimension/extension lines, and (added later, in the callouts block)
    # other callouts' own leaders - not just off of other callouts' text.
    avoid_segments = []

    c.saveState()
    c.setLineWidth(0.5)

    # Hidden (dashed) lines are drawn FIRST, visible (solid) lines SECOND,
    # so a solid edge always paints cleanly over any hidden edge it
    # coincides with or crosses in the projection - previously the reverse
    # order let a dashed hidden-line segment visibly interrupt/overlay a
    # real solid edge wherever the two happened to cross on the page.
    c.setDash([3, 2])
    c.setStrokeColorRGB(0.7, 0.7, 0.7)
    c.setLineWidth(0.3)
    for line in view['hidden']:
        _stroke_edge(c, line, to_page)
        pts = [to_page(p) for p in line]
        for p1, p2 in zip(pts, pts[1:]):
            avoid_segments.append((p1[0], p1[1], p2[0], p2[1]))

    c.setDash([])
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.5)
    for line in view['visible']:
        _stroke_edge(c, line, to_page)
        pts = [to_page(p) for p in line]
        for p1, p2 in zip(pts, pts[1:]):
            avoid_segments.append((p1[0], p1[1], p2[0], p2[1]))
    c.setDash([])
    c.setStrokeColorRGB(0, 0, 0)

    # Snapshot before dimension/leader lines get added below - used to snap
    # a point-feature's (fillet/chamfer) anchor onto the nearest ACTUAL
    # drawn edge, never onto a dimension line.
    geometry_only_segments = list(avoid_segments)

    if dimension:
        # Real points on the actual outline achieving each extreme, NOT
        # bbox corners - for anything rounded/oval, a bbox corner is empty
        # space nowhere near the part, so an extension line anchored there
        # doesn't visibly connect to anything (see _extreme_points).
        min_x_pt, max_x_pt, min_y_pt, max_y_pt = _extreme_points(all_lines)
        left_ext = to_page(min_x_pt)
        right_ext = to_page(max_x_pt)
        bottom_ext = to_page(min_y_pt)
        top_ext = to_page(max_y_pt)

        # If this view has a real chain (hole-to-hole) breakdown, the
        # envelope dimension's own tolerance is the RSS (root-sum-square)
        # stack-up of each elementary segment's own tolerance - the
        # standard statistical tolerance-stacking practice - rather than a
        # single flat percentage of the FULL span. Decomposing "135mm" into
        # its real constituent gaps (4.25 | 15.98 | ... | 33.91mm) and
        # combining each one's own (smaller, since it's a smaller length)
        # tolerance via sqrt(sum(t_i^2)) is both more physically meaningful
        # (each real machined feature-to-feature gap has its own tolerance,
        # not the overall span) and normally noticeably tighter than
        # tol_fn(full_span) directly, since RSS combination of several
        # smaller independent tolerances is smaller than one tolerance
        # computed on their sum. Falls back to the flat calculation on
        # views with no such breakdown (FRONT/BACK/LEFT/RIGHT/BOTTOM don't
        # carry a hole-to-hole chain the way TOP's bolt pattern does).
        # chain_points_x / chain_points_y, when given, override the shared
        # chain_points for that axis (geometry-derived ordinates).
        src_x = chain_points_x if chain_points_x is not None else chain_points
        src_y = chain_points_y if chain_points_y is not None else chain_points
        xs_pts = ys_pts = None
        if tol_fn and chain_dimension and (src_x or src_y):
            xs_pts = _unique_representative_points(src_x or [], 0, CHAIN_TOLERANCE_MM, prefer_extreme=1)
            ys_pts = _unique_representative_points(src_y or [], 1, CHAIN_TOLERANCE_MM, prefer_extreme=0)

        def _rss_or_flat_tolerance(full_span, ordered_pts, coord_index):
            if ordered_pts and len(ordered_pts) > 1:
                segments = [abs(b[coord_index] - a[coord_index]) for a, b in zip(ordered_pts, ordered_pts[1:])]
                return math.sqrt(sum(tol_fn(seg) ** 2 for seg in segments))
            return tol_fn(full_span)

        dim_y = origin_y + padding * ENVELOPE_ROW_FRACTION
        dim_x = origin_x + box_w - padding * ENVELOPE_ROW_FRACTION
        span_x_text = units.format_dimension(
            span_x, _rss_or_flat_tolerance(span_x, xs_pts, 0) if tol_fn else None)
        span_y_text = units.format_dimension(
            span_y, _rss_or_flat_tolerance(span_y, ys_pts, 1) if tol_fn else None)
        _draw_dimension_line(c, left_ext[0], dim_y, right_ext[0], dim_y, span_x_text, 'horizontal',
                              ext_from1=left_ext, ext_from2=right_ext)
        _draw_dimension_line(c, dim_x, bottom_ext[1], dim_x, top_ext[1], span_y_text, 'vertical',
                              ext_from1=bottom_ext, ext_from2=top_ext)
        # Approximate but sufficient for collision purposes: the dimension
        # line itself, plus each extension line's full run from the actual
        # feature point down/across to the dimension row.
        avoid_segments.append((left_ext[0], dim_y, right_ext[0], dim_y))
        avoid_segments.append((left_ext[0], left_ext[1], left_ext[0], dim_y))
        avoid_segments.append((right_ext[0], right_ext[1], right_ext[0], dim_y))
        avoid_segments.append((dim_x, bottom_ext[1], dim_x, top_ext[1]))
        avoid_segments.append((bottom_ext[0], bottom_ext[1], dim_x, bottom_ext[1]))
        avoid_segments.append((top_ext[0], top_ext[1], dim_x, top_ext[1]))
        # The dimension TEXT itself, not just its line - previously only the
        # line/extension segments were tracked, so nothing else on the sheet
        # could tell this text was even there, and could freely land right
        # on top of it.
        for bx0, by0, bx1, by1 in (
            _dimension_text_box(c, left_ext[0], dim_y, right_ext[0], dim_y, span_x_text, 'horizontal'),
            _dimension_text_box(c, dim_x, bottom_ext[1], dim_x, top_ext[1], span_y_text, 'vertical'),
        ):
            avoid_segments.append((bx0, by0, bx1, by0))
            avoid_segments.append((bx1, by0, bx1, by1))
            avoid_segments.append((bx1, by1, bx0, by1))
            avoid_segments.append((bx0, by1, bx0, by0))

        if chain_dimension and (src_x or src_y):
            # chain_points are already in this view's local (model) units -
            # mm, the same frame min_x/max_x/etc. are in - so the tolerance
            # is used as-is; dividing by `scale` here would convert it into
            # neither useful unit and shrink it depending on page geometry,
            # which is exactly why two points ~0.17mm apart were previously
            # left undeduplicated instead of merging into one ordinate.
            #
            # The extension line for each ordinate runs to that hole's own
            # actual (mx, my) position - not to a point on the body's outer
            # bbox edge that merely shares its X or Y - so it visibly
            # connects to the real feature, even if that means crossing
            # over other geometry on the way (normal, expected in real
            # dimensioned drawings).
            xs_pts, xs_groups = _grouped_representative_points(
                src_x or [], 0, CHAIN_TOLERANCE_MM, prefer_extreme=1)
            ys_pts, ys_groups = _grouped_representative_points(
                src_y or [], 1, CHAIN_TOLERANCE_MM, prefer_extreme=0)

            def _add_text_box_edges(box):
                bx0, by0, bx1, by1 = box
                avoid_segments.append((bx0, by0, bx1, by0))
                avoid_segments.append((bx1, by0, bx1, by1))
                avoid_segments.append((bx1, by1, bx0, by1))
                avoid_segments.append((bx0, by1, bx0, by0))

            # Two (or more) features that are genuinely aligned (same X, or
            # same Y) all get their own extension line reaching the shared
            # ordinate's row - not just whichever one was picked to anchor
            # the dimension tick itself. Per explicit request: an aligned
            # feature that only ever got the IMPLICIT "this line happens to
            # pass near it" treatment (or nothing at all, if it was nearer
            # the row than the picked representative) now gets a real,
            # separately drawn extension line of its own.
            # a chain that is just one segment spanning the whole view repeats the envelope
            if len(xs_pts) > 1 and not (len(xs_pts) == 2 and abs(abs(xs_pts[1][0] - xs_pts[0][0]) - span_x) <= CHAIN_TOLERANCE_MM):
                chain_row_y = origin_y + padding * CHAIN_ROW_FRACTION
                points = [(mx,) + to_page((mx, my)) for mx, my in xs_pts]
                for box in _draw_chain_dimensions(c, points, chain_row_y, 'horizontal', tol_fn=tol_fn, avoid_segments=avoid_segments):
                    _add_text_box_edges(box)
                for group in xs_groups:
                    for mx, my in group:
                        px, py = to_page((mx, my))
                        _draw_extension_line_vertical(c, px, py, chain_row_y)
                        avoid_segments.append((px, py, px, chain_row_y))
                for (_m1, px1, _py1), (_m2, px2, _py2) in zip(points, points[1:]):
                    avoid_segments.append((px1, chain_row_y, px2, chain_row_y))
            if len(ys_pts) > 1 and not (len(ys_pts) == 2 and abs(abs(ys_pts[1][1] - ys_pts[0][1]) - span_y) <= CHAIN_TOLERANCE_MM):
                chain_row_x = origin_x + box_w - padding * CHAIN_ROW_FRACTION
                points = [(my,) + to_page((mx, my)) for mx, my in ys_pts]
                for box in _draw_chain_dimensions(c, points, chain_row_x, 'vertical', tol_fn=tol_fn, avoid_segments=avoid_segments):
                    _add_text_box_edges(box)
                for group in ys_groups:
                    for mx, my in group:
                        px, py = to_page((mx, my))
                        _draw_extension_line_horizontal(c, py, px, chain_row_x)
                        avoid_segments.append((px, py, chain_row_x, py))
                for (_m1, _px1, py1), (_m2, _px2, py2) in zip(points, points[1:]):
                    avoid_segments.append((chain_row_x, py1, chain_row_x, py2))

    if cross_marks:
        for mx, my, radius_mm in cross_marks:
            px, py = to_page((mx, my))
            px = min(max(px, origin_x), origin_x + box_w)
            py = min(max(py, origin_y), origin_y + box_h)
            if radius_mm == 0:
                # Same snap-to-nearest-drawn-edge the matching callout
                # anchor gets below (for the identical reason: a fillet/
                # chamfer/slot's surface-midpoint projection doesn't always
                # land exactly on the rendered outline) - using the SAME
                # snapped point here is what keeps the cross and its own
                # leader's arrowhead touching the same spot instead of the
                # arrow visibly landing on a different edge than the cross.
                px, py = _snap_to_nearest_segment(px, py, geometry_only_segments, max_dist=6 * mm)
            _draw_center_cross(c, px, py)
            # A cross is a real mark on the sheet too - callout text landing
            # on top of one is just as much a readability problem as
            # landing on a drawn edge (the leader LINE itself is still free
            # to cross one, same as it's always been free to cross geometry).
            avoid_segments.append((px - CENTER_CROSS_SIZE, py, px + CENTER_CROSS_SIZE, py))
            avoid_segments.append((px, py - CENTER_CROSS_SIZE, px, py + CENTER_CROSS_SIZE))

    if callouts:
        # Callouts are confined to a region inset from the box edges by
        # slightly more than the dimension lines' own offset (see
        # ENVELOPE_ROW_FRACTION/CHAIN_ROW_FRACTION above) - a hard
        # separation, not just hope, so a routed callout can never land on
        # top of a dimension line/arrow no matter how the search goes.
        dim_clearance = padding * (CHAIN_ROW_FRACTION + 0.15) if dimension else 2 * mm
        region = (origin_x + dim_clearance, origin_y + dim_clearance,
                  origin_x + box_w - dim_clearance, origin_y + box_h - dim_clearance)
        center_page = to_page(((min_x + max_x) / 2, (min_y + max_y) / 2))

        items = []
        for mx, my, balloon_label, radius_mm, is_balloon in callouts:
            px, py = to_page((mx, my))
            # Clamp (not drop) an anchor that projects just outside this
            # view's box - every feature assigned to this view must get
            # SOME leader here, never silently disappear because of a
            # borderline out-of-bounds projection (seen in practice for a
            # slot-end face whose trimmed extent OCC reports slightly beyond
            # the rest of the body's silhouette).
            px = min(max(px, origin_x), origin_x + box_w)
            py = min(max(py, origin_y), origin_y + box_h)
            if radius_mm == 0:
                # A point feature (fillet/chamfer): its anchor is a 3D
                # surface point's own 2D projection, which doesn't always
                # land exactly on the view's rendered outline (the surface
                # midpoint used to compute it isn't guaranteed to be the
                # part of the fillet actually visible from this angle).
                # Snap it onto the nearest real drawn edge instead of
                # leaving the leader pointing at a gap between curves.
                px, py = _snap_to_nearest_segment(px, py, geometry_only_segments, max_dist=6 * mm)
            items.append((px, py, balloon_label, radius_mm * scale, is_balloon))

        for placement in _route_callouts(items, region, center_page, avoid_segments):
            _draw_leader_callout(c, placement)
            # _route_callouts routes against its OWN internal copy of
            # avoid_segments (so later callouts in that same call already
            # avoid earlier ones), but never mutates the list passed in -
            # anything drawn AFTER the callouts block (the slot length/width
            # dimensions below) would otherwise have no idea a balloon or
            # leader is sitting there. Feeding each real placement's leader
            # line(s) and text/balloon footprint back in here is what lets
            # those later dimensions actually dodge them.
            anchor, elbow = placement['anchor'], placement['elbow']
            avoid_segments.append((anchor[0], anchor[1], elbow[0], elbow[1]))
            if not placement['is_balloon']:
                avoid_segments.append((elbow[0], elbow[1], placement['shoulder_x'], elbow[1]))
            bx0, by0, bx1, by1 = placement['box']
            avoid_segments.append((bx0, by0, bx1, by0))
            avoid_segments.append((bx1, by0, bx1, by1))
            avoid_segments.append((bx1, by1, bx0, by1))
            avoid_segments.append((bx0, by1, bx0, by0))

    if length_dims:
        # A slot's own length/width, between its two rounded ends or its
        # two width-offset points, drawn as a proper "aligned" dimension -
        # a line parallel to the real feature (whatever angle that is,
        # axis-aligned or not), offset outward until it (and its text)
        # clear the view's own drawn geometry AND everything already on
        # this sheet - other dimension lines, callout leaders, balloons -
        # not just the view's own silhouette (see `_draw_offset_dimension`).
        # Drawn AFTER callouts specifically so that check has real balloon/
        # leader positions to react to. Which side it's pushed toward is
        # decided per-pair from the feature's own position relative to the
        # view's center - outward, away from the bulk of the geometry - not
        # a fixed side, so this adapts to whatever body is actually being
        # drawn instead of a placement tuned for one part.
        view_bbox_min = to_page((min_x, min_y))
        view_bbox_max = to_page((max_x, max_y))
        for (p1x, p1y), (p2x, p2y), length_mm in length_dims:
            a = to_page((p1x, p1y))
            b = to_page((p2x, p2y))
            text = units.format_dimension(length_mm, tol_fn(length_mm) if tol_fn else None)
            result = _draw_offset_dimension(c, a, b, text, view_bbox_min, view_bbox_max,
                                             avoid_segments, MIN_DIMENSION_CLEARANCE)
            if result:
                x1, y1, x2, y2, ax1, ay1, ax2, ay2, bx1, by1, bx2, by2 = result
                avoid_segments.append((x1, y1, x2, y2))
                avoid_segments.append((ax1, ay1, ax2, ay2))
                avoid_segments.append((bx1, by1, bx2, by2))

    c.rect(origin_x, origin_y, box_w, box_h, stroke=1, fill=0)
    c.setFont('Helvetica-Bold', 9)
    c.drawString(origin_x + 3, origin_y + box_h - 12, label)
    c.restoreState()
    return scale


def format_drawing_scale(scale):
    """Convert a page-points-per-model-mm `scale` into a real "N:1"/"1:N"
    drafting scale label. `mm` (reportlab's mm-to-points constant) IS what
    1 real-world mm maps to on a physically true-size printout, so
    scale/mm is the dimensionless ratio of how much the drawing has been
    enlarged (>1) or reduced (<1) to fit the page - not a fixed "1:1 (fit
    to sheet)" label that was never actually checked against the real
    per-view scale each view independently computes to fit its own box.
    """
    ratio = scale / mm
    if ratio <= 0:
        return '1:1'
    if ratio >= 1:
        return f'{_round_nicely(ratio)}:1'
    return f'1:{_round_nicely(1 / ratio)}'


def _round_nicely(value):
    if value >= 10:
        return f'{value:.0f}'
    if value >= 1:
        return f'{value:.1f}'.rstrip('0').rstrip('.')
    return f'{value:.2f}'.rstrip('0').rstrip('.')


LEGEND_ROW_HEIGHT = 4.0 * mm
LEGEND_HEADER_HEIGHT = 11
LEGEND_MIN_COL_WIDTH = 65 * mm


def _legend_sort_key(label):
    """Sort a legend/balloon label ("12", "10-12", "4,7") by its first
    number, so the legend reads in the same ascending order the numbers
    were originally assigned in, not alphabetically (which would put "10"
    before "2")."""
    try:
        return int(label.replace(',', '-').split('-')[0])
    except ValueError:
        return 0


def _legend_rows(entries):
    """Flatten {label: lines} into the actual text rows the legend will
    print - the label prefixes only its first line; every additional
    stacked line gets its own row, indented under it, instead of being
    crammed onto one "a; b; c" line - both easier to read and unambiguous
    about which lines belong to which merged feature.
    """
    rows = []
    for label in sorted(entries.keys(), key=_legend_sort_key):
        lines = entries[label] or ['']
        rows.append(f'{label}  {lines[0]}')
        for line in lines[1:]:
            rows.append(f'      {line}')
    return rows


def _legend_columns_for(entries, width, height):
    """How many columns the legend needs to fit `entries` within a FIXED
    `width` x `height` area (the title block's own empty region to the
    right of its field grid, not a separately-reserved strip) - as many
    columns as the row budget requires, capped by how many actually fit
    across `width` at a readable minimum column width.
    """
    rows = _legend_rows(entries)
    if not rows:
        return rows, 0
    max_cols_by_width = max(1, int(width / LEGEND_MIN_COL_WIDTH))
    max_rows_per_col = max(1, int((height - LEGEND_HEADER_HEIGHT) / LEGEND_ROW_HEIGHT))
    num_cols = max(1, math.ceil(len(rows) / max_rows_per_col))
    num_cols = min(num_cols, max_cols_by_width)
    return rows, num_cols


def _draw_legend(c, x, y, width, height, entries):
    """The feature legend, drawn directly in the title block's own empty
    area (to the right of its name/date/projection/units/scale field grid)
    - every balloon shown on this page has its full text (diameter/depth,
    thread designation, radius, etc.) listed here by its balloon label,
    since the balloon itself only ever carries a bare number or merged
    range - laid out in as many columns as fit `width` x `height`, each of
    a merged entry's stacked lines on its own row. `entries`:
    {label: callout_lines}. Draws nothing (not even a header) when empty -
    the title block's own border already frames the area, so an empty
    legend correctly looks like a plain (not "there's an empty box here")
    part of the title block instead of an unlabeled inner box.
    """
    rows, num_cols = _legend_columns_for(entries, width, height)
    if not rows:
        return
    c.saveState()
    c.setFont('Helvetica-Bold', 7)
    c.drawString(x + 2, y + height - 9, 'FEATURE LEGEND')

    max_rows = max(1, math.ceil(len(rows) / num_cols))
    col_w = width / num_cols
    c.setFont('Helvetica', 6.3)
    top_y = y + height - LEGEND_HEADER_HEIGHT - LEGEND_ROW_HEIGHT + 2
    for i, text in enumerate(rows):
        col, row = divmod(i, max_rows)
        rx = x + col * col_w + 2
        ry = top_y - row * LEGEND_ROW_HEIGHT
        if ry < y + 2:
            continue  # pathologically many entries - clip rather than overflow into the field grid
        c.drawString(rx, ry, text)
    c.restoreState()


def _draw_third_angle_symbol(c, x, y, scale=0.22):
    """A precise scaled replica of the REAL ANSI/ASME Y14.3 third-angle
    projection symbol - not a hand-approximated icon, and not the earlier
    "corrected" version either, which still had the trapezoid backwards.
    Traced directly from the reference SVG (Wikimedia Commons,
    "Third_angle_projection_symbol.svg", by Redrose64), whose own geometry
    (a 200x100 unit canvas) is:
      - trapezoid (frustum side view): polygon (10,10)-(90,30)-(90,70)-
        (10,90) - the LEFT edge (x=10) is the TALL/large end (height 80,
        y from 10 to 90), the RIGHT edge (x=90, nearest the circles) is
        the SHORT/small end (height 40, y from 30 to 70) - i.e. the
        trapezoid tapers from large (left) to small (right), NOT the
        other way around as an earlier version of this drew it.
      - two concentric circles (frustum end view), both centered at
        (150, 50): radius 20 (matching the trapezoid's adjacent small
        end) and radius 40 (matching its far large end).
      - a horizontal centerline (y=50, the shared axis) and a vertical
        crosshair (x=150, through the circles' own center), both in the
        SVG's own dash-dot pattern (a long dash, then two short dashes),
        not a plain uniform dash.

    `(x, y)` is where the symbol's own shared centerline (SVG y=50) starts
    on the page, at the trapezoid's own left edge (SVG x=0, i.e. the
    centerline overshoot before the trapezoid itself). `scale` converts
    SVG units to page points (points; default lands at roughly a 44pt-wide
    symbol overall).
    """
    def P(svg_x, svg_y):
        # SVG's own Y increases downward; page Y increases upward, and
        # svg_y=50 is the shared centerline, anchored at this call's own y.
        return x + svg_x * scale, y - (svg_y - 50) * scale

    c.saveState()

    # Shared centerline (horizontal) + crosshair (vertical through the
    # circles) - the SVG's own dash-dot pattern, scaled with everything else.
    dash = [d * scale for d in (35, 5, 5, 5)]
    c.setStrokeColorRGB(0.25, 0.25, 0.25)
    c.setDash(dash, 17.5 * scale)
    c.setLineWidth(1 * scale)
    c.line(*P(0, 50), *P(200, 50))
    c.line(*P(150, 0), *P(150, 100))
    c.setDash([])
    c.setStrokeColorRGB(0, 0, 0)

    # Trapezoid (frustum side view) - large end left, small end right.
    c.setLineWidth(2 * scale)
    path = c.beginPath()
    path.moveTo(*P(10, 10))
    path.lineTo(*P(90, 30))
    path.lineTo(*P(90, 70))
    path.lineTo(*P(10, 90))
    path.close()
    c.drawPath(path, stroke=1, fill=0)

    # Concentric circles (frustum end view).
    ccx, ccy = P(150, 50)
    c.circle(ccx, ccy, 20 * scale, stroke=1, fill=0)
    c.circle(ccx, ccy, 40 * scale, stroke=1, fill=0)
    c.restoreState()


def _draw_title_block(c, page_w, body_name, sheet_label=None, scale_label=None, legend=None):
    import datetime

    c.saveState()
    c.setLineWidth(0.75)
    block_w = page_w - 2 * MARGIN
    c.rect(MARGIN, MARGIN, block_w, TITLE_BLOCK_HEIGHT, stroke=1, fill=0)

    left_w = 90 * mm
    c.line(MARGIN + left_w, MARGIN, MARGIN + left_w, MARGIN + TITLE_BLOCK_HEIGHT)

    c.setFont('Helvetica-Bold', 14)
    c.drawString(MARGIN + 5 * mm, MARGIN + TITLE_BLOCK_HEIGHT - 15, body_name)
    if sheet_label:
        c.setFont('Helvetica-Bold', 9)
        c.drawRightString(MARGIN + left_w - 3 * mm, MARGIN + TITLE_BLOCK_HEIGHT - 15, sheet_label)
    c.setFont('Helvetica', 6.5)
    c.setFillColorRGB(0.35, 0.35, 0.35)
    c.drawString(MARGIN + 5 * mm, MARGIN + TITLE_BLOCK_HEIGHT - 25, 'Auto-generated drawing - verify before manufacture')
    c.setFillColorRGB(0, 0, 0)
    c.setLineWidth(0.4)
    c.line(MARGIN, MARGIN + TITLE_BLOCK_HEIGHT - 29, MARGIN + left_w, MARGIN + TITLE_BLOCK_HEIGHT - 29)

    # Small standard-title-block style field grid: label above value, two
    # columns x two rows, below the part name.
    field_y_top = MARGIN + TITLE_BLOCK_HEIGHT - 29
    row_h = (field_y_top - MARGIN) / 2
    col_w = left_w / 2
    fields = [
        ('DATE', datetime.date.today().isoformat(), 0, 1),
        ('PROJECTION', 'THIRD ANGLE', 1, 1),
        ('UNITS', units.length_symbol(), 0, 0),
        ('SCALE', scale_label or 'N/A', 1, 0),
    ]
    for label_text, value_text, col, row in fields:
        fx = MARGIN + col * col_w
        fy = MARGIN + row * row_h
        if col == 1:
            c.line(fx, MARGIN, fx, field_y_top)
        if row == 1:
            c.line(MARGIN, fy, MARGIN + left_w, fy)
        c.setFont('Helvetica', 5.5)
        c.setFillColorRGB(0.35, 0.35, 0.35)
        c.drawString(fx + 2, fy + row_h - 7, label_text)
        c.setFont('Helvetica-Bold', 7.5)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(fx + 2, fy + 3, value_text)
        if label_text == 'PROJECTION':
            # Per explicit request: top-right corner of this field's own
            # cell, at 2x the previous scale. The symbol's own local
            # origin (see _draw_third_angle_symbol's docstring) sits at
            # its centerline's left end, with the whole shape extending
            # 190*scale to the right (to the far circle's edge) and
            # 50*scale above/below that centerline - so the anchor is
            # placed that far in from the cell's own top-right corner
            # (minus a small margin) to land the symbol flush there.
            symbol_scale = 0.44
            margin_pt = 3
            symbol_x = fx + col_w - margin_pt - 190 * symbol_scale
            symbol_y = fy + row_h - margin_pt - 50 * symbol_scale
            _draw_third_angle_symbol(c, symbol_x, symbol_y, scale=symbol_scale)

    # The feature legend lives in the title block's own right-hand area
    # (to the right of the name/date/projection/units/scale field grid) -
    # previously just empty space once the old "ADDITIONAL FEATURES" table
    # was removed, and later a separately-reserved strip above the title
    # block that cost the view space even on pages with few/no entries.
    if legend:
        _draw_legend(c, MARGIN + left_w + 4 * mm, MARGIN + 2 * mm,
                     block_w - left_w - 6 * mm, TITLE_BLOCK_HEIGHT - 4 * mm, legend)

    c.restoreState()


def _assign_holes_to_views(point_features, bbox):
    """Each hole cluster is called out in exactly ONE view - its own best,
    position-correct view via `_best_view_for_point` (e.g. a 5x M8 pattern
    on one face shows only there, not also duplicated onto the opposite
    face's page where an unrelated 8x M6 pattern belongs) - per explicit
    request: a hole's callout should appear on only one view, not be
    duplicated as a "hidden reference" on the opposite one (an earlier
    round's behavior, reverted here).

    Fillets/chamfers/slots/pockets are different: per a later explicit
    request, these should be named/dimensioned in EVERY view where they are
    actually visible (e.g. a corner fillet blending a top face and a side
    face belongs on both that TOP page and the side page), falling back to
    just their one best view when they're only visible from one side. Each
    carries a `visible_views` set (from `constraint_matcher.py`'s
    `_visible_views_for_feature`) - the SAME `pf` dict is appended to every
    view bucket it names, not a copy, so `id(pf)`-keyed lookups (the global
    feature number, the legend lines) stay a single shared identity no
    matter how many pages reference it.
    """
    assignment = {name: [] for name in ORTHOGRAPHIC_VIEWS}
    for pf in point_features:
        if pf['kind'] == 'hole_cluster':
            primary = _best_view_for_point(pf['axis_point'], pf['axis_direction'], bbox)
            assignment[primary].append(pf)
        else:
            for view in pf.get('visible_views') or {pf['forced_view']}:
                assignment[view].append(pf)
    return assignment


SUMMARY_KINDS = ('fillet_summary', 'chamfer_summary', 'slot_summary', 'pocket_summary')
# There is no title-block summary table any more - every one of these kinds
# now carries its own axis_point/axis_direction (a slot's midpoint between
# its two rounded ends, a pocket group's representative face centroid) and
# gets a real in-view leader, the same treatment holes/threads already had,
# so every feature is pointed out somewhere in the drawing instead of only
# appearing as title-block text.
LEADERED_SUMMARY_KINDS = SUMMARY_KINDS


def generate_drawing(step_path, matches, output_pdf_path, tol_fn=None, feature_settings=None):
    body = load_step(step_path)
    body_name = os.path.splitext(os.path.basename(step_path))[0]
    views = generate_views(body.shape)

    hole_clusters = [m for m in matches if m['kind'] == 'hole_cluster']
    leadered_summaries = [m for m in matches if m['kind'] in LEADERED_SUMMARY_KINDS]
    point_features = hole_clusters + leadered_summaries
    holes_by_view = _assign_holes_to_views(point_features, body.bounding_box)

    # Chain (hole-to-hole spacing) dimensioning uses only the holes actually
    # ASSIGNED to (and so shown with a balloon on) each of TOP/BOTTOM - a
    # 5x M8 pattern that's a TOP-only feature has no business getting a
    # spacing dimension on the BOTTOM page too, where it isn't even called
    # out at all (previously this used every hole_cluster unconditionally
    # on both views, showing dimensions for holes that page never actually
    # references). Every physical instance's position is used, not just
    # one representative per NX group - otherwise an 8-hole bolt pattern
    # only ever contributed ONE point to the spacing dimensions, silently
    # leaving the other 7 instances' positions undimensioned.
    def _chain_points_for(view_name):
        ax2 = views[view_name]['ax2']
        own_holes = [pf for pf in holes_by_view[view_name] if pf['kind'] == 'hole_cluster']
        pts = [project_point(ax2, hc['axis_point']) for hc in own_holes]
        pts += [
            project_point(ax2, pt)
            for hc in own_holes
            for pt in hc.get('other_instance_points', [])
        ]
        return pts

    import feature_dimensions
    fd_settings = feature_settings or feature_dimensions.FeatureDimensionSettings()

    def _hole_circles_for(view_name):
        ax2 = views[view_name]['ax2']
        out = []
        for hc in hole_clusters:
            for pt in [hc['axis_point']] + list(hc.get('other_instance_points', [])):
                x, y = project_point(ax2, pt)
                out.append((x, y, hc['entry_diameter_mm'] / 2))
        return out

    def _dimension_inputs_for(view_name):
        """(x chain points, y chain points, diameter callouts) for one view:
        hole positions plus the view's own stepped outline and circles."""
        view = views[view_name]
        x0, y0, x1, y1 = _bbox_of_polylines(view['visible'] + view['hidden'])
        gx, gy = feature_dimensions.chain_points(view['visible'], x1 - x0, y1 - y0, fd_settings)
        hole_pts = _chain_points_for(view_name)
        # a geometry ordinate too close to a hole's own position (or to the
        # view's edge) would only stack its label on top of that one
        gap_x = fd_settings.min_ordinate_gap_fraction * (x1 - x0)
        gap_y = fd_settings.min_ordinate_gap_fraction * (y1 - y0)
        gx = [p for p in gx if all(abs(p[0] - h[0]) >= gap_x for h in hole_pts)]
        gy = [p for p in gy if all(abs(p[1] - h[1]) >= gap_y for h in hole_pts)]
        diameters = feature_dimensions.diameter_callouts(
            view['visible'], _hole_circles_for(view_name), fd_settings)
        return hole_pts + gx, hole_pts + gy, diameters

    diameter_number = {}
    dimension_inputs = {name: _dimension_inputs_for(name) for name in ORTHOGRAPHIC_VIEWS}

    # Every feature group gets ONE number, stable across the whole PDF (all
    # 7 pages of this body) - "same number always means the same feature",
    # per explicit request. Order is whatever `point_features` already is
    # (holes first, then fillets/chamfers/slots/pockets in their own
    # existing order) - arbitrary but deterministic and consistent for a
    # given body.
    feature_number = {id(pf): i + 1 for i, pf in enumerate(point_features)}

    def build_callouts(view_name):
        # Actual leader direction/distance is decided by _route_callouts
        # (a real collision-avoidance search) inside _draw_view - this just
        # hands it each feature INSTANCE's projected position, its group's
        # global number, and (for a hole) the visible circle's radius so
        # the leader's arrow touches the circle's actual edge instead of
        # its buried center. Every physical instance now gets its own
        # routed arrow+balloon (not just one representative per group,
        # with the rest as a bare unlabeled cross) - per explicit request,
        # nothing is left as just an unexplained mark.
        ax2 = views[view_name]['ax2']
        raw_items = []  # (x, y, number, radius_mm)
        seen_points_by_number = {}  # number -> set of rounded (x, y) already placed
        length_dims = []
        lines_by_number = {}

        def _add_instance(number, radius_mm, point_3d):
            px, py = project_point(ax2, point_3d)
            # Two DIFFERENT physical instances of the same feature can sit
            # at different depths along this view's own viewing axis (e.g.
            # a through-slot's two ends, one at each side of the material)
            # and so project to the EXACT same 2D point in this particular
            # view - genuinely distinct in 3D, but indistinguishable here.
            # Drawing two overlapping arrows to one visible dot conveys
            # nothing extra and just looks like a duplicate, so only the
            # first instance to land on a given rounded page-position (per
            # number) gets a balloon in THIS view - the others are still
            # real instances, still correctly counted/labeled, just not
            # each given their own redundant on-page marker here.
            key = (round(px, 3), round(py, 3))
            seen = seen_points_by_number.setdefault(number, set())
            if key in seen:
                return
            seen.add(key)
            raw_items.append((px, py, number, radius_mm))

        for pf in holes_by_view[view_name]:
            # A slot's own length dimension is only meaningful in the ONE
            # view actually looking down its axis (its own `forced_view` -
            # the same "best" view `_best_view_for_point` picked) - the
            # slot's two length-pairs are naturally far apart there. Since
            # slots now get a balloon on EVERY view they're visible in (not
            # just their primary one), drawing this same length dimension
            # on every one of those views too meant it also got drawn in
            # views where the axis is nearly edge-on to the camera, so the
            # two points project almost on top of each other - the
            # resulting near-zero-length dimension line/arrowheads/text all
            # collapsed into one small illegible cluster that any nearby
            # balloon then inevitably overlapped.
            if pf.get('kind') != 'slot_summary' or view_name == pf.get('forced_view'):
                for p1, p2, length_mm in pf.get('slot_length_pairs', []):
                    length_dims.append((project_point(ax2, p1), project_point(ax2, p2), length_mm))
                for p1, p2, width_mm in pf.get('slot_width_pairs', []):
                    length_dims.append((project_point(ax2, p1), project_point(ax2, p2), width_mm))
            number = feature_number[id(pf)]
            lines_by_number[number] = pf['callout_lines']
            radius_mm = pf['entry_diameter_mm'] / 2 if pf['kind'] == 'hole_cluster' else 0.0
            _add_instance(number, radius_mm, pf['axis_point'])
            for pt in pf.get('other_instance_points', []):
                _add_instance(number, radius_mm, pt)

        # Every physical instance still gets its own small center cross at
        # its own true point, in addition to its balloon+arrow - the
        # balloon itself usually sits some distance away, so the cross is
        # what actually marks the feature's exact location. Carries
        # radius_mm along so `_draw_view` can apply the SAME nearest-edge
        # snap to a point feature's cross as its callout anchor already
        # gets (see `_snap_to_nearest_segment` there) - drawing the cross
        # at the raw, unsnapped point while the leader's arrowhead lands on
        # the snapped one visibly disconnected the two (the arrow touching
        # a nearby edge instead of the cross it was supposed to mark).
        cross_marks = [(x, y, radius_mm) for x, y, _number, radius_mm in raw_items]

        # Every feature INSTANCE gets its own separate balloon - per
        # explicit request, two different features (or two different
        # instances) landing close together must never be combined into
        # one shared balloon showing a merged range like "10-12": that
        # made the legend's own "one feature per number" fix above
        # pointless (you'd still see one ambiguous merged marker on the
        # actual drawing) and hid which specific instance is which. The
        # collision-avoidance router (`_route_callouts`) is what keeps
        # separate nearby balloons from actually overlapping on the page,
        # not a pre-merge here.
        callouts = [
            (x, y, _format_balloon_label([number]), radius_mm, True)
            for x, y, number, radius_mm in raw_items
        ]
        # The legend is keyed by each INDIVIDUAL number, one row-group per
        # feature - every number with at least one instance on this page
        # gets its own legend entry.
        legend = {}
        for _x, _y, number, _radius_mm in raw_items:
            legend[str(number)] = lines_by_number[number]
        return callouts, length_dims, legend, cross_marks

    page_w, page_h = PAGE_SIZE
    c = canvas.Canvas(output_pdf_path, pagesize=PAGE_SIZE)

    drawing_area_h = page_h - 2 * MARGIN - TITLE_BLOCK_HEIGHT
    drawing_area_w = page_w - 2 * MARGIN

    # One view per page, each filling the whole drawing area - not a 2x2
    # grid on a single page. A quarter-page box was the real ceiling on how
    # much room the collision-avoidance router had to work with; no amount
    # of routing cleverness fixes a box that's fundamentally too small for
    # its callout load. All six standard orthographic views get their own
    # full page, plus one pictorial ISO.
    pages = []
    for view_key in ORTHOGRAPHIC_VIEWS:
        callouts, length_dims, legend, cross_marks = build_callouts(view_key)
        xs_chain, ys_chain, diameters = dimension_inputs[view_key]
        for cx, cy, _r, dia in diameters:
            # same diameter -> same number on every page, continuing after
            # the matched features' own numbers
            number = diameter_number.setdefault(round(dia, 3), len(feature_number) + len(diameter_number) + 1)
            callouts.append((cx, cy, _format_balloon_label([number]), _r, True))
            legend[str(number)] = ['Ø' + units.format_dimension(dia, tol_fn(dia) if tol_fn else None) + ' DIAMETER']
        pages.append((view_key, view_key, callouts, length_dims, legend, cross_marks, True, True,
                      (xs_chain, ys_chain)))
    pages.append(('ISO', 'ISO (pictorial)', None, None, {}, None, False, False, None))

    for i, (view_key, label, callouts, length_dims, legend, cross_marks,
            dimension, chain_dim, cpoints) in enumerate(pages, start=1):
        # The legend now lives inside the title block's own area (see
        # _draw_title_block), not a separately-reserved strip - every page
        # gets the full drawing area for its view regardless of how many
        # legend entries it has.
        page_scale = _draw_view(c, views[view_key], MARGIN, MARGIN + TITLE_BLOCK_HEIGHT,
                                 drawing_area_w, drawing_area_h, label,
                                 callouts=callouts, length_dims=length_dims, cross_marks=cross_marks,
                                 dimension=dimension, chain_dimension=chain_dim,
                                 chain_points_x=cpoints[0] if cpoints else None,
                                 chain_points_y=cpoints[1] if cpoints else None,
                                 tol_fn=tol_fn)
        _draw_title_block(c, page_w, body_name,
                           sheet_label=f'SHEET {i} OF {len(pages)} - {label}',
                           scale_label=format_drawing_scale(page_scale), legend=legend)
        c.showPage()

    c.save()
    return output_pdf_path


def generate_all(test_dir, output_dir=None, progress=None, include_tolerances=True, length_unit=None):
    """Generate every part's drawing folder under `output_dir`.

    `progress(fraction, message)`, if given, is called as the work advances
    (fraction 0-1); it may raise to abort the run.
    `include_tolerances=False` leaves every +/- figure off the drawings even
    when the project has a saved tolerance configuration.
    `length_unit` (a key of units.LENGTH, e.g. 'inch') sets the unit the
    dimension labels are written in; None keeps the current one.
    """
    if length_unit:
        units.set_units(length=length_unit)
    import glob
    import json

    import assembly_guide
    import tolerance as tolerance_mod

    def report(fraction, message):
        if progress is not None:
            progress(min(max(fraction, 0.0), 1.0), message)

    output_dir = output_dir or os.path.join(test_dir, 'drawings')
    os.makedirs(output_dir, exist_ok=True)
    report(0.0, 'Reading project...')

    with open(os.path.join(test_dir, 'project_data.json'), encoding='utf-8') as f:
        project_data = json.load(f)

    # tolerance_config.json is optional - its absence means every dimension
    # renders exactly as before (no thermal/tolerance system engaged at
    # all). When present, it names a preset (loose/standard/tight) and the
    # manufacturing/operating temperatures used to compute each dimension's
    # "+/-X.XX" tolerance from that body's own Fusion-assigned material.
    tolerance_config = tolerance_mod.load_tolerance_config(test_dir)
    has_tolerance_config = include_tolerances and os.path.exists(
        tolerance_mod.config_file(test_dir, 'tolerance_config.json'))
    if has_tolerance_config:
        try:
            import fit_check
            tolerance_config.body_gaps = fit_check.nearest_gap_per_body(test_dir)
        except Exception as exc:
            print(f'Could not measure gaps between parts ({exc}); using the largest tolerance.')

    import body_inclusion
    import feature_dimensions

    feature_settings = feature_dimensions.load_settings(test_dir)

    all_step_paths = sorted(glob.glob(os.path.join(test_dir, '*.step')))

    # A body's own saved/default inclusion state (see body_inclusion.py)
    # decides what happens to it here:
    # - 'ignore': excluded entirely, BEFORE match_all_bodies (which loads
    #   and analyzes every body's real geometry) even runs - never loaded,
    #   never matched, never drawn.
    # - 'assembly_only' (off-the-shelf hardware by default - there's
    #   nothing to machine, and a standard fastener is specified by its
    #   callout/designation, not a page of dimensions): still exported/
    #   counted everywhere else (bodychain, fit_check, the assembly guide)
    #   but skips the matching + per-body PDF, same as before this
    #   selector existed.
    # - 'drawing': the normal case, gets both.
    inclusion = body_inclusion.load_inclusion(test_dir)
    step_paths = []
    skipped_names = []
    ignored_names = []
    for step_path in all_step_paths:
        body_name = os.path.splitext(os.path.basename(step_path))[0]
        state = inclusion.get(body_name, 'drawing')
        if state == 'ignore':
            ignored_names.append(body_name)
        elif state == 'assembly_only':
            skipped_names.append(body_name)
        else:
            step_paths.append(step_path)
    fastener_names = skipped_names

    # Each part gets its own folder holding its drawing and a copy of its STEP
    # re-oriented so its dominant features run along the axes. Matching and
    # drawing both work from that rotated copy.
    import part_orientation
    original_step_paths = step_paths
    step_paths = []
    for index, original in enumerate(original_step_paths):
        name = os.path.splitext(os.path.basename(original))[0]
        report(0.02 + 0.18 * index / max(len(original_step_paths), 1), f'Orienting {name}')
        part_dir = os.path.join(output_dir, name)
        os.makedirs(part_dir, exist_ok=True)
        rotated_path = os.path.join(part_dir, f'{name}.step')
        try:
            body = load_step(original)
            if len(body.faces()) == 0:
                step_paths.append(original)
                continue
            angle = part_orientation.export_oriented_step(body.shape, rotated_path)
            print(f'Oriented {name}: rotated {angle:.1f} deg')
            step_paths.append(rotated_path)
        except Exception as exc:
            print(f'Could not orient {name} ({exc}); using the original orientation.')
            step_paths.append(original)

    report(0.20, 'Matching features...')
    match_results = match_all_bodies(
        step_paths, project_data,
        tolerance_config=tolerance_config if has_tolerance_config else None,
    )

    generated = []
    skipped = []
    for index, step_path in enumerate(step_paths):
        body_name = os.path.splitext(os.path.basename(step_path))[0]
        report(0.30 + 0.70 * index / max(len(step_paths), 1), f'Drawing {body_name} ({index + 1}/{len(step_paths)})')

        matches = match_results['per_body'][step_path]['matched']
        part_dir = os.path.join(output_dir, body_name)
        os.makedirs(part_dir, exist_ok=True)
        output_pdf = os.path.join(part_dir, f'{body_name}.pdf')

        # A STEP file can look normal (non-trivial file size, valid header)
        # yet contain zero actual faces - seen in practice with bodies
        # belonging to a referenced/linked sub-component that didn't
        # resolve at Fusion's export time. Rather than silently emit a
        # blank, useless PDF for it (which loading it doesn't itself error
        # on - the shape just has no faces), skip it with a clear warning
        # so it doesn't look like a legitimate empty part.
        body = load_step(step_path)
        if len(body.faces()) == 0:
            skipped.append(body_name)
            print(f'Skipped (no geometry in STEP file): {step_path}')
            continue

        tol_fn = None
        if has_tolerance_config:
            material_entry = project_data.get('materials', {}).get(body_name)
            tol_fn = tolerance_mod.make_tolerance_fn(material_entry, tolerance_config.for_body(body_name))

        generate_drawing(step_path, matches, output_pdf, tol_fn=tol_fn, feature_settings=feature_settings)
        generated.append(output_pdf)
        print(f'Generated: {output_pdf}')

    report(1.0, 'Done')
    if skipped:
        print(f'\n{len(skipped)} body(ies) skipped - STEP file(s) had no geometry: {", ".join(skipped)}')
        print('Re-check these in Fusion (see the export script\'s completion message for details).')

    if fastener_names:
        print(f'\n{len(fastener_names)} body(ies) marked "assembly only" skipped (no detail drawing needed): '
              f'{", ".join(fastener_names)}')

    if ignored_names:
        print(f'\n{len(ignored_names)} body(ies) marked "ignore" excluded entirely: '
              f'{", ".join(ignored_names)}')

    return generated
