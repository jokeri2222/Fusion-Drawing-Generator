"""
Assembly guide: an ordered, human-readable build sequence covering every
real body in the export AND every detected fastener.

Fastener bodies are identified primarily by NAME (screw, bolt, nut,
washer, hex socket, ...) - see `FASTENER_NAME_KEYWORDS`/`is_fastener_name`.
project_data.json's `body_types` entry, when present, adds Fusion's real
`isReferencedComponent` flag as a POSITIVE-only override on top of that:
if Fusion says True (this component's definition was inserted from an
external file/library), that's trusted outright. A False reading is NOT
trusted as "not a fastener", though - confirmed on a real project export
to come back False for every single body, screws included, because
Fusion only sets it for a component still *linked* to an external
document, not one inserted as a full local copy (the normal way a
fastener library actually gets used, e.g. "Insert McMaster-Carr
Component"). So in practice this flag can promote a body to "definitely
a fastener" but can never demote one - the name heuristic is still what
actually does most of the work, and is what tells apart a natively
designed body like "Motor Custom Key" ("key" was deliberately dropped
from the keyword list for being too collision-prone - see below) from a
real inserted "Hex Socket Head Cap Screw" component.

Two things this builds on, already established elsewhere in this project:
- bodychain.py's "bodies preserve their true absolute assembly position"
  fact (Fusion's export doesn't re-center each body at its own origin) -
  what makes any of this possible without separate assembly metadata.
- The same "no assembly metadata, work from bounding boxes/geometry alone"
  spirit as bodychain.py's chain detection and fit_check.py's nearest-point
  matching - reused here (bounding-box axial + transverse overlap) to find
  which non-fastener bodies each fastener actually passes through, without
  needing a precise concentric-axis fit.
"""

import glob
import itertools
import math
import re
import json
import os

import step_loader

# Vocabulary real Fusion/McMaster-Carr hardware components are named with -
# the FALLBACK detection, only used for a project_data.json that predates
# `body_types` (see the module docstring for why the real flag there is
# always preferred when available).
FASTENER_NAME_KEYWORDS = (
    'screw', 'bolt', 'nut', 'washer', 'rivet', 'stud',
    'hex socket', 'socket head', 'cap screw', 'anchor', 'fastener',
)

AXES = ('X', 'Y', 'Z')


def is_fastener_name(name):
    """NAME-only fastener check - the fallback `is_fastener` uses when
    `body_types` isn't available. Exported under its old name too since
    other code (and callers outside this module) may still reference it
    directly.
    """
    lname = name.lower()
    return any(kw in lname for kw in FASTENER_NAME_KEYWORDS)


def load_body_types(project_dir):
    """The `body_types` section of project_data.json, if present - see the
    module docstring for what it holds and why it's preferred over name
    matching. Returns {} (not an error) for a project exported before this
    field existed, or with no project_data.json at all.
    """
    path = os.path.join(project_dir, 'project_data.json')
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {}
    return data.get('body_types', {}) or {}


def is_fastener(name, body_types):
    """Whether `name` is a fastener/off-the-shelf-hardware body.

    `is_referenced_component` (via `body_types`, see the module docstring)
    is used as a POSITIVE-only signal: True is trusted outright (Fusion
    itself says this body's component was inserted from an external file/
    library, about as certain as this gets). It is NOT trusted when False,
    though - confirmed, on a real project export, to come back False for
    EVERY body, screws included: Fusion only sets it for a component still
    *linked* to an external document, not one inserted as a full local
    copy (e.g. via "Insert McMaster-Carr Component" or "Insert Derive"),
    which is how actual fastener libraries get used in practice. A False
    (or missing) reading is therefore not real evidence of anything, so it
    falls back to the NAME heuristic (`is_fastener_name`) instead of
    asserting "not a fastener" - the previous "trust it either way"
    version wrongly cleared every real screw's fastener status this way,
    making them get real per-body drawings and show up in Per-Body
    Temperatures again.
    """
    entry = body_types.get(name)
    if entry is not None and entry.get('is_referenced_component'):
        return True
    return is_fastener_name(name)


def _bbox_range(bbox, axis):
    return {
        'X': (bbox.xmin, bbox.xmax),
        'Y': (bbox.ymin, bbox.ymax),
        'Z': (bbox.zmin, bbox.zmax),
    }[axis]


def _load_bodies(project_dir):
    # Local import: body_inclusion.py itself imports this module (for
    # `is_fastener`'s default heuristic), so importing it at module level
    # here would be circular.
    import body_inclusion

    ignored = {
        name for name, state in body_inclusion.load_inclusion(project_dir).items()
        if state == 'ignore'
    }
    bodies = []
    for step_path in sorted(glob.glob(os.path.join(project_dir, '*.step'))):
        name = os.path.splitext(os.path.basename(step_path))[0]
        if name in ignored:
            # Blocked before anything happens to this body at all - never
            # loaded, same guarantee a detected fastener already had,
            # just now user-selectable for any body via body_inclusion.py.
            continue
        body = step_loader.load_step(step_path)
        if len(body.faces()) == 0:
            continue  # empty STEP file (referenced/unresolved component) - not a real body
        bodies.append((name, body))
    return bodies


def _perpendicular_overlap(bbox_a, bbox_b, axis):
    """True if `bbox_a` and `bbox_b` genuinely overlap in BOTH axes
    perpendicular to `axis` - the same "don't just check one axis" logic
    bodychain.py's own `_cross_section_overlaps` uses, for the identical
    reason: two bodies whose spans happen to line up along the fastener's
    own axis but sit in completely different XY (say) locations aren't
    actually joined by it.
    """
    for ax in AXES:
        if ax == axis:
            continue
        lo_a, hi_a = _bbox_range(bbox_a, ax)
        lo_b, hi_b = _bbox_range(bbox_b, ax)
        if min(hi_a, hi_b) - max(lo_a, lo_b) <= 0:
            return False
    return True


def _shapes_touch(shape_a, shape_b, tolerance_mm=0.75):
    """Whether two real solids actually come within `tolerance_mm` of each
    other, via OCC's own exact distance query - used to confirm a fastener
    genuinely passes through a candidate body rather than merely sharing a
    bounding-box region with it (see `find_fastener_joints`'s docstring:
    a bounding box is a poor proxy for anything non-rectangular, e.g. a
    circular gear's square bbox covers its corners too, where there's no
    actual material).
    """
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    try:
        # The nearest points of two solids closer than `tolerance_mm` lie
        # inside the overlap of their boxes (grown by the tolerance), so only
        # the faces reaching into that region are fed to the exact (slow)
        # distance solver - same answer, far fewer faces.
        pair = _faces_near_overlap(shape_a, shape_b, tolerance_mm)
        if pair is None:
            return False
        dist = BRepExtrema_DistShapeShape(pair[0], pair[1])
        return dist.IsDone() and dist.Value() <= tolerance_mm
    except Exception:
        return True  # distance query failed - fall back to "assume touching"


def _faces_near_overlap(shape_a, shape_b, tol):
    """(compound_a, compound_b) of each shape's faces whose box reaches the
    region where the two shapes' boxes (grown by `tol`) overlap; None if
    that region is empty or either side has no such face."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Builder
    from OCP.BRepBndLib import BRepBndLib
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Compound

    def box(shape):
        b = Bnd_Box()
        BRepBndLib.Add_s(shape.wrapped if hasattr(shape, 'wrapped') else shape, b, False)
        b.SetGap(0.0)
        return b.Get()  # xmin, ymin, zmin, xmax, ymax, zmax

    ba, bb = box(shape_a), box(shape_b)
    lo = [max(ba[i], bb[i]) - tol for i in range(3)]
    hi = [min(ba[i + 3], bb[i + 3]) + tol for i in range(3)]
    if any(l > h for l, h in zip(lo, hi)):
        return None

    def pick(shape):
        builder, comp, n = BRep_Builder(), TopoDS_Compound(), 0
        builder.MakeCompound(comp)
        ex = TopExp_Explorer(shape.wrapped, TopAbs_FACE)
        while ex.More():
            face = TopoDS.Face_s(ex.Current())
            fb = box(face)
            if all(fb[i] <= hi[i] and fb[i + 3] >= lo[i] for i in range(3)):
                builder.Add(comp, face)
                n += 1
            ex.Next()
        return comp if n else None

    ca, cb = pick(shape_a), pick(shape_b)
    return None if ca is None or cb is None else (ca, cb)


def find_fastener_joints(project_dir, axial_overlap_tolerance_mm=0.5, bodies=None):
    """Every fastener body's own joint: which OTHER (non-fastener) bodies
    it actually passes through/touches, determined along ITS OWN dominant
    (longest bounding-box) axis. Bounding boxes (axial span + perpendicular
    overlap) are used only as a cheap PRE-filter to cut down the candidate
    list; a real solid-to-solid distance query (`_shapes_touch`) then
    confirms actual contact, since a bounding box alone is a poor proxy for
    non-rectangular parts - a circular gear's square bbox covers its own
    corners too, where a screw sitting just outside the gear's actual disc
    would otherwise wrongly register as "touched", producing steps whose
    "bodies it passes through" list drags in parts the fastener never
    actually reaches (confirmed the direct cause of gears showing up in
    hidden-line context for bolt steps that don't actually touch them).

    Returns a list of {'fastener': name, 'axis': 'X'/'Y'/'Z', 'bodies': [...]}.
    """
    if bodies is None:
        bodies = _load_bodies(project_dir)
    body_types = load_body_types(project_dir)
    fasteners = [(n, b) for n, b in bodies if is_fastener(n, body_types)]
    others = [(n, b) for n, b in bodies if not is_fastener(n, body_types)]

    joints = []
    for name, body in fasteners:
        bbox = body.bounding_box
        spans = {ax: _bbox_range(bbox, ax) for ax in AXES}
        axis = max(spans, key=lambda ax: spans[ax][1] - spans[ax][0])
        lo, hi = spans[axis]

        touched = []
        for other_name, other_body in others:
            obbox = other_body.bounding_box
            olo, ohi = _bbox_range(obbox, axis)
            if min(hi, ohi) - max(lo, olo) < -axial_overlap_tolerance_mm:
                continue
            if not _perpendicular_overlap(bbox, obbox, axis):
                continue
            if _shapes_touch(body.shape, other_body.shape):
                touched.append(other_name)
        if touched:
            joints.append({'fastener': name, 'axis': axis, 'bodies': touched})
    return joints


_LAST_ORDER_WARNINGS = []


def _build_step_sequence(project_dir):
    """The single ordered build sequence both the plain-text guide
    (`generate_assembly_guide`) and the visual one
    (`generate_assembly_instructions_pdf`) describe, so the two never
    disagree. It follows the assembly tree from `assembly_order.py` (saved
    by the Assembly Order editor, else a geometry-based proposal):
    - a loose part is a `place` step (identical parts share one step);
    - a sub-assembly, nested to any depth, is built on its own pages first
      (its parts, its inner sub-assemblies, the fasteners that only join
      its own parts) and then installed into its parent as one `merge` step;
    - fasteners of the same type/axis/head level are ONE `fasten` step,
      installed right after the last body they join, inside the smallest
      sub-assembly that contains all of those bodies.

    Returns (bodies, used_real_flag, steps, unplaced_joints):
    - `bodies`: name -> StepBody, for the visual guide's own geometry.
    - `used_real_flag`: whether any body's fastener/non-fastener call came
      from Fusion's real `isReferencedComponent` flag (via `body_types`)
      rather than the name-keyword fallback - see `is_fastener`.
    - `steps`: ordered dicts with `kind` ('place' / 'merge' / 'fasten'),
      `context` (names already shown in gray), `scope` (list of enclosing
      sub-assembly names, [] for the main assembly) and, per kind,
      `names` (parts added), `unit`/`parent`/`names` (merge), or
      `fasteners`/`bodies`/`axis` (fasten).
    - `unplaced_joints`: fastener groups whose joined bodies never made it
      into the order (nothing to install them after).
    Order warnings from the same analysis go to `_LAST_ORDER_WARNINGS`.
    """
    body_list = _load_bodies(project_dir)
    bodies = dict(body_list)
    if not body_list:
        return bodies, False, [], []

    body_types = load_body_types(project_dir)
    used_real_flag = any(
        body_types.get(name, {}).get('is_referenced_component')
        for name, _b in body_list
    )
    import assembly_order
    az = assembly_order.OrderAnalyzer(project_dir, body_list)
    order = az.effective_order()
    _LAST_ORDER_WARNINGS[:] = az.check_order(order)

    def part_key(n):
        bb = az.bbox[n]
        dims = tuple(sorted(round(d, 1) for d in (bb.xmax - bb.xmin, bb.ymax - bb.ymin, bb.zmax - bb.zmin)))
        return assembly_order.type_key(n), dims

    emitted = set()

    def emit_ready(path, placed, context, scope, out):
        """Fastener steps whose innermost enclosing node is `path` and
        whose bodies are all placed by now."""
        for gi, g in enumerate(az.groups):
            if gi in emitted or g.get('path') != path:
                continue
            needed = [b for b in g['bodies'] if b in az.parts]
            if needed and all(b in placed for b in needed):
                emitted.add(gi)
                out.append({'kind': 'fasten', 'fasteners': g['fasteners'], 'bodies': g['bodies'],
                            'axis': g['axis'], 'context': list(context), 'scope': list(scope)})
                context.extend(g['fasteners'])

    def build(nodes, path, scope, parent):
        """Steps for one assembly's children. Returns (steps, everything
        shown when it's finished, parts it contains)."""
        out, ctx, placed, done = [], [], set(), set()

        def place(names, set_name=None):
            out.append({'kind': 'place', 'names': names, 'name': names[0], 'set': set_name,
                        'context': list(ctx), 'scope': list(scope)})
            ctx.extend(names)
            placed.update(names)

        def visit(items):
            loose = [n for n in items if isinstance(n, str)]
            for node in items:
                if assembly_order.is_fasten(node):
                    # hand-placed fastener step (Assembly Order editor)
                    out.append({'kind': 'fasten', 'fasteners': node['fasteners'], 'bodies': node['bodies'],
                                'axis': node.get('axis'), 'context': list(ctx), 'scope': list(scope)})
                    ctx.extend(node['fasteners'])
                    continue
                if isinstance(node, str):
                    if node in done:
                        continue
                    names = [n for n in loose if n not in done and part_key(n) == part_key(node)]
                    done.update(names)
                    place(names)
                elif assembly_order.is_set(node):
                    # not an assembly: its parts go in together, named by the set;
                    # an assembly nested inside it is still built and merged
                    flat = [n for n in assembly_order.flatten([node])
                            if n not in done and not _inside_assembly(node, n)]
                    done.update(flat)
                    if flat:
                        place(flat, node['name'])
                    for ch in node['children']:
                        if isinstance(ch, dict):
                            visit([ch])
                else:
                    sub_scope = scope + [node['name']]
                    sub_steps, sub_all, sub_parts = build(node['children'], path + (id(node),), sub_scope, node['name'])
                    out.extend(sub_steps)
                    out.append({'kind': 'merge', 'unit': node['name'], 'parent': parent, 'names': list(sub_all),
                                'name': node['name'], 'context': list(ctx), 'scope': list(scope)})
                    ctx.extend(sub_all)
                    placed.update(sub_parts)
                if not az.fastener_manual:
                    emit_ready(path, placed, ctx, scope, out)

        visit(nodes)
        return out, ctx, placed

    def _inside_assembly(set_node, name):
        """Whether `name` sits inside an assembly nested in `set_node`
        (those parts belong to that assembly's own build, not the set's)."""
        for ch in set_node['children']:
            if isinstance(ch, dict):
                if assembly_order.is_set(ch):
                    if _inside_assembly(ch, name):
                        return True
                elif name in assembly_order.flatten([ch]):
                    return True
        return False

    steps, _all, _placed = build(az.tree, (), [], None)
    unplaced_joints = [
        g for gi, g in enumerate(az.groups)
        if gi not in emitted and not any(b in order for b in g['bodies'])
    ] if not az.fastener_manual else []
    return bodies, used_real_flag, steps, unplaced_joints


# A real exported instance name ends in " v1-N" (Fusion's own occurrence
# numbering - see the export script's sanitize_filename) - stripped when
# comparing/labeling a group of identical fasteners so "...v1-1" through
# "...v1-7" are recognized as 7 of the SAME part, not 7 different ones.
_INSTANCE_SUFFIX_RE = re.compile(r'\s+v\d+-\d+$', re.IGNORECASE)


def _fastener_label(names):
    """Human-readable label for one or more fastener body names sharing a
    joint - "the one name" for a single fastener, or "Nx <designation>"
    when all of them are really the same part (differing only in Fusion's
    own instance-number suffix), falling back to listing every name (rare
    - would mean genuinely different fastener types share one joint).
    """
    if len(names) == 1:
        return names[0]
    stripped = {_INSTANCE_SUFFIX_RE.sub('', n) for n in names}
    if len(stripped) == 1:
        return f'{len(names)}x {next(iter(stripped))}'
    return ' / '.join(names)


def _place_text(step):
    """'Position ...' line; a set (non-assembly group) is named and its
    parts listed instead of being treated as an assembly."""
    if step.get('set'):
        return f'Position "{step["set"]}": {_part_label(step["names"])}.'
    return f'Position {_part_label(step["names"])}.'


def _scope_label(scope):
    return ' > '.join(scope)


def _merge_text(step):
    where = f'"{step["parent"]}"' if step.get('parent') else 'the main assembly'
    return f'Install the finished sub-assembly "{step["unit"]}" into {where}.'


def _part_label(names):
    if len(names) == 1:
        return f'"{names[0]}"'
    import assembly_order
    base = {assembly_order.type_key(n) for n in names}
    if len(base) == 1:
        return f'{len(names)}x "{next(iter(base))}"'
    return ' + '.join(f'"{n}"' for n in names)


def generate_assembly_guide(project_dir):
    """An ordered, plain-text build sequence - see `_build_step_sequence`
    for how the order and fastener joints are derived.
    """
    bodies, used_real_flag, steps, unplaced_joints = _build_step_sequence(project_dir)
    if not bodies:
        return 'No bodies found in this project.'

    lines = [
        'Assembly Guide (auto-generated - verify against the real design)',
        '=' * 70,
        '',
        'Build order comes from the assembly tree saved in the Assembly Order',
        'editor (configs/assembly_tree.json), or - if none was saved - a',
        'geometry-based proposal (support from below, contact with placed',
        'parts, screw access). Sub-assemblies are built separately, then',
        'installed as one block. Check it against the real design: access',
        'and interference are only approximated from bounding boxes/solids.',
    ]
    base_caveat = (
        'Fastener/hardware bodies are identified mainly by NAME (screw/bolt/'
        '\nnut/washer/hex socket/... keywords) - a heuristic, not a certified'
        '\nproperty - verify each one against the real design before using'
        '\nthis as a work instruction.'
    )
    if used_real_flag:
        lines.append(
            base_caveat + ' Fusion\'s own "inserted from an\nexternal '
            'component" flag additionally confirmed at least one body here.'
        )
    else:
        lines.append(base_caveat)
    lines.append('')

    if not steps:
        lines.append('No non-fastener bodies found - nothing to sequence.')
    step_num = 1
    for step in steps:
        prefix = f'[{_scope_label(step["scope"])}] ' if step.get('scope') else ''
        if step['kind'] == 'place':
            lines.append(f'Step {step_num}: {prefix}{_place_text(step)}')
        elif step['kind'] == 'merge':
            lines.append(f'Step {step_num}: {prefix}{_merge_text(step)}')
        else:
            bodies_str = ' + '.join(step['bodies'])
            lines.append(
                f'Step {step_num}: {prefix}Secure with {_fastener_label(step["fasteners"])} through '
                f'{bodies_str} (along {step["axis"]}).'
            )
        step_num += 1

    order_warnings = list(_LAST_ORDER_WARNINGS)
    if order_warnings:
        lines.append('')
        lines.append('Order warnings:')
        lines.extend(f'  - {w}' for w in order_warnings)

    if unplaced_joints:
        lines.append('')
        lines.append('Fastener groups whose bodies are not in the build order (verify manually):')
        for group in unplaced_joints:
            lines.append(f"  {_fastener_label(group['fasteners'])}")

    if not any(s['kind'] == 'fasten' for s in steps) and not unplaced_joints:
        lines.append('')
        lines.append('No fastener bodies detected in this export.')

    return '\n'.join(lines)


# Colors (0-1 RGB) for the visual assembly guide: bodies already installed
# in a previous step are drawn in gray (context only), the body/fastener
# being added THIS step is drawn in red (what to actually look for).
_INSTALLED_COLOR = (0.55, 0.55, 0.55)
_ADD_NOW_COLOR = (0.82, 0.1, 0.1)


# Bounds on the mesh deflection (mm) the assembly PDF's own HLR pass uses -
# see `_body_deflection` for why it's scaled per body rather than one flat
# value. Tightened (was 0.015-0.12) after the per-body HLR classification
# rewrite made edge detection cheap to get right - the old upper bound let
# a large body's round features facet coarsely enough to misclassify
# visible-vs-hidden at its own silhouette boundary (self-occlusion, not
# occlusion by another body), independent of whichever body/matching
# approach is used downstream.
_MIN_MESH_DEFLECTION_MM = 0.01
_MAX_MESH_DEFLECTION_MM = 0.05


def _body_deflection(body):
    """Mesh deflection (how closely the HLR mesh follows the real curved
    surface, and so how faceted/kinked a curve is allowed to look) scaled
    to THIS body's own finest round feature rather than one flat value
    used for everything.

    A flat deflection coarse enough to keep a large housing's file size
    down is, relative to a small M5 screw's own ~2.5mm radius (or a slim
    motor-mount bar's own radius), coarse enough to visibly facet its
    round shank into what reads as a bent/kinked part - confirmed by
    comparing a screw's and a mount bar's own rendered outline here
    against their STEP files (genuinely straight/round there). Basing it
    on the SMALLEST cylindrical radius actually on the body (its own
    finest curvature, not its overall size/length - a long slim rod has a
    small diagonal-based deflection need but an even smaller radius-based
    one) keeps that curve smooth; a body with no round features at all
    (pure flat/faceted) falls back to a bounding-box-based value, since
    there's no curvature to protect there and a large flat part gains
    nothing from a finer mesh beyond larger file size.
    """
    try:
        radii = [cf.radius for cf in body.cylindrical_faces() if cf.radius > 1e-6]
    except Exception:
        radii = []
    candidates = []
    if radii:
        candidates.append(min(radii) * 0.03)
    # Cross-section-based fallback, always computed (not just when no
    # cylindrical face was found): a screw's threaded shank is often
    # modeled as a swept/helical surface OCC doesn't classify as
    # GeomAbs_Cylinder at all, so `cylindrical_faces()` can come back
    # empty for a real round shaft and silently fall through to the
    # bounding-box-DIAGONAL value below, which for a long thin screw is
    # dominated by its length rather than its actual radius and was
    # confirmed (via a rendered page) to still facet/kink the shank just
    # like the motor-mount-bar bug this was originally written to fix.
    # The two SMALLEST bounding-box dimensions approximate a long thin
    # part's own cross-section (diameter) regardless of whether OCC
    # recognized any face as a true cylinder.
    bb = body.bounding_box
    dims = sorted((bb.xlen, bb.ylen, bb.zlen))
    cross_radius = dims[1] / 2
    if cross_radius > 1e-6:
        candidates.append(cross_radius * 0.06)
    if candidates:
        return min(_MAX_MESH_DEFLECTION_MM, max(_MIN_MESH_DEFLECTION_MM, min(candidates)))
    diag = math.sqrt(bb.xlen ** 2 + bb.ylen ** 2 + bb.zlen ** 2)
    return min(_MAX_MESH_DEFLECTION_MM, max(_MIN_MESH_DEFLECTION_MM, diag * 0.0015))


def _combined_view_per_body(bodies, names, deflection=None):
    """ONE exact hidden-line pass over every body in `names` together (real
    mutual occlusion), split back into each body's own visible/hidden edges.
    Edges stay true vector geometry (see vector_hlr.py). `deflection` is
    unused now (no mesh) and kept for the callers' signature.

    Returns {name: {'visible': [Edge...], 'hidden': [Edge...]}}.
    """
    import drawing_generator as dg
    import vector_hlr

    present = [n for n in names if n in bodies]
    if not present:
        return {}
    direction, x_dir = dg.VIEW_DIRECTIONS['ISO']
    res = vector_hlr.exact_views({n: bodies[n].shape.wrapped for n in present}, direction, x_dir)
    return {n: {'visible': v, 'hidden': h} for n, (v, h) in res.items()}


def _combined_bbox(bodies, names):
    """The ISO-view-local 2D bounding box (same frame the page rendering
    itself uses) of every body's real 3D bounding box in `names`, used
    only to pick one fixed page scale/frame up front (see
    `generate_assembly_instructions_pdf`). Each body's 8 bbox corners are
    projected into the view's own 2D frame (same `project_point`/`gp_Ax2`
    drawing_generator.py's HLR pipeline itself uses) rather than running a
    full HLR pass just to find a bounding box - much cheaper, and a
    bounding box only needs SOME point achieving each extreme, not the
    real outline.
    """
    import drawing_generator as dg

    direction, x_dir = dg.VIEW_DIRECTIONS['ISO']
    ax2 = dg.make_ax2(direction, x_dir)
    xs, ys = [], []
    for n in names:
        if n not in bodies:
            continue
        bb = bodies[n].bounding_box
        for x in (bb.xmin, bb.xmax):
            for y in (bb.ymin, bb.ymax):
                for z in (bb.zmin, bb.zmax):
                    px, py = dg.project_point(ax2, (x, y, z))
                    xs.append(px)
                    ys.append(py)
    if not xs:
        return -1.0, -1.0, 1.0, 1.0
    return min(xs), min(ys), max(xs), max(ys)


_VISIBLE_LINE_WIDTH = 1.0
_HIDDEN_LINE_WIDTH = 0.5


def _draw_iso_group(c, view, color_rgb, to_page, show_hidden=True, show_visible=True):
    """Draw one body's visible (solid) edges and, optionally, its hidden
    (dashed) ones, as real vector paths: straight edges are lines, curves are
    Bezier curves (dashes follow the curve itself, so nothing is polygonal).
    `show_hidden` is turned off by callers drawing many already-installed
    bodies at once for the ones not relevant to the current step."""
    import vector_hlr
    c.setStrokeColorRGB(*color_rgb)

    def paths(edges):
        lines = [e for e in edges if e.is_line]
        curves = vector_hlr.chain_edges([e for e in edges if not e.is_line]) if any(not e.is_line for e in edges) else []
        return lines, curves

    def stroke(segs):
        path = c.beginPath()
        vector_hlr.trace_segs(path, segs, to_page)
        c.drawPath(path)

    if show_hidden:
        # hidden lines: thinner and washed toward white, so they read as
        # secondary to the solid ones
        c.setStrokeColorRGB(*(0.45 * v + 0.55 for v in color_rgb))
        c.setLineWidth(_HIDDEN_LINE_WIDTH)
        dash = (3, 2)
        period = float(sum(dash))
        lines, curves = paths(view['hidden'])
        for e in lines:
            # straight: lock the dash phase to the position along the line
            # (drawn in a canonical direction) so parallel hidden lines
            # lying side by side dash in step
            (x0, y0), (x1, y1) = (to_page(p) for p in e.segs[0][1:3])
            ang = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0
            ang = round(ang * 2) / 2.0 % 180.0
            ux, uy = math.cos(math.radians(ang)), math.sin(math.radians(ang))
            segs = e.segs
            if (x1 - x0) * ux + (y1 - y0) * uy < 0:
                segs = vector_hlr.reverse_segs(segs)
                x0, y0 = x1, y1
            c.setDash(list(dash), (x0 * ux + y0 * uy) % period)
            stroke(segs)
        for segs in curves:
            # curve: canonical start point and direction, so identical or
            # translated copies of a curve dash identically
            c.setDash(list(dash))
            stroke(vector_hlr.canonical_segs(segs))
        c.setDash([])
        c.setStrokeColorRGB(*color_rgb)
        c.setLineWidth(_VISIBLE_LINE_WIDTH)
    if not show_visible:
        return
    lines, curves = paths(view['visible'])
    for e in lines:
        stroke(e.segs)
    for segs in curves:
        stroke(segs)


def generate_assembly_instructions_pdf(project_dir, output_pdf_path):
    """A visual, page-per-step assembly guide: each page shows an ISO
    (pictorial) view of the assembly-so-far, with everything already
    installed drawn in gray and the body/fastener to add THIS step drawn
    in red, next to the same plain-language instruction text
    `generate_assembly_guide` produces for that step.

    Every page shares one fixed scale/frame (the bounding box of every
    body in the export), so the assembly doesn't visibly zoom or drift
    step to step - it's the same real absolute coordinates throughout
    (see the module docstring), just with more of it drawn in each time.
    """
    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    bodies, _used_real_flag, steps, unplaced_joints = _build_step_sequence(project_dir)
    if not steps:
        raise ValueError('No bodies found in this project - nothing to draw.')

    min_x, min_y, max_x, max_y = _combined_bbox(bodies, list(bodies.keys()))
    span_x = max(max_x - min_x, 1e-6)
    span_y = max(max_y - min_y, 1e-6)

    page_w, page_h = landscape(A3)
    margin = 15 * mm
    top_text_height = 45 * mm
    padding = 15 * mm
    box_w = page_w - 2 * margin
    box_h = page_h - margin - top_text_height - margin
    scale = min((box_w - 2 * padding) / span_x, (box_h - 2 * padding) / span_y)
    scale = max(scale, 1e-6)
    origin_x = margin + padding + max(0.0, (box_w - 2 * padding - span_x * scale) / 2)
    origin_y = margin + padding + max(0.0, (box_h - 2 * padding - span_y * scale) / 2)

    def to_page(pt):
        return origin_x + (pt[0] - min_x) * scale, origin_y + (pt[1] - min_y) * scale

    import drawing_generator as dg
    view_dir = dg.VIEW_DIRECTIONS['ISO'][0]

    def depth_of(name):
        # Painting rank: plain parts first, fasteners after them (a screw
        # head sits ON its part - where their edges coincide the fastener's
        # line must win), each group far to near. Depth is the body's
        # NEAREST bounding-box corner toward the viewer, not its centre -
        # a long screw's centre is far behind the head that is in front.
        bb = bodies[name].bounding_box
        near = max(x * view_dir[0] + y * view_dir[1] + z * view_dir[2]
                   for x in (bb.xmin, bb.xmax) for y in (bb.ymin, bb.ymax) for z in (bb.zmin, bb.zmax))
        return (1 if is_fastener_name(name) else 0, near)

    c = canvas.Canvas(output_pdf_path, pagesize=(page_w, page_h))

    total = len(steps)
    for step_num, step in enumerate(steps, start=1):
        installed = list(step['context'])
        if step['kind'] == 'place':
            new_names = list(step['names'])
            instruction = _place_text(step)
        elif step['kind'] == 'merge':
            new_names = list(step['names'])
            instruction = _merge_text(step)
        else:
            new_names = step['fasteners']
            bodies_str = ' + '.join(step['bodies'])
            instruction = (
                f'Secure with {_fastener_label(step["fasteners"])} through {bodies_str} '
                f'(along {step["axis"]}).'
            )

        c.setFont('Helvetica-Bold', 18)
        c.drawString(margin, page_h - margin - 18, f'Step {step_num} of {total}')
        c.setFont('Helvetica', 13)
        if step.get('scope'):
            c.setFont('Helvetica-Bold', 12)
            c.drawString(margin + 140 * mm, page_h - margin - 18, f'Sub-assembly: {_scope_label(step["scope"])} (build separately)')
            c.setFont('Helvetica', 13)
        c.drawString(margin, page_h - margin - 40, instruction)
        c.setFont('Helvetica', 9)
        c.setFillColorRGB(*_INSTALLED_COLOR)
        c.drawString(margin, page_h - margin - 58, 'GRAY = already in place')
        c.setFillColorRGB(*_ADD_NOW_COLOR)
        c.drawString(margin + 130 * mm, page_h - margin - 58, 'RED = added in this step')
        c.setFillColorRGB(0, 0, 0)

        # ONE combined HLR pass over installed + new together, so occlusion
        # is real and mutual: an already-installed body genuinely in front
        # of the new one hides it (and vice versa - a newly-added body
        # dropped inside/in front of already-installed geometry, e.g. a
        # gear now sitting inside a closed housing, correctly occludes that
        # housing's wall too) - see `_combined_view_per_body`'s own
        # docstring. Which side of that occlusion shows as a DASHED hidden
        # line rather than just being dropped is handled separately below:
        # hidden lines are only kept for the new body itself and for
        # installed bodies a fastener actually passes through this step,
        # not for every occluded edge on the page. Body identity for each
        # resulting line comes straight from OCC's own per-shape query, not
        # from re-matching 2D geometry after the fact.
        all_names = installed + new_names
        page_deflection = min(_body_deflection(bodies[n]) for n in all_names if n in bodies)
        full_view = _combined_view_per_body(bodies, all_names, deflection=page_deflection)

        # (body, colour, visible edges, hidden edges) - drawn far to near below
        items = []
        new_present = [n for n in new_names if n in bodies]

        if step['kind'] == 'fasten':
            # The bodies the fastener joins show their full wireframe (their
            # own hidden edges dashed) so you can see where the fastener goes
            # through. Everything else stays opaque: the joined bodies and
            # the new fasteners hide whatever is behind them, so nothing
            # that sits behind a part shows through it.
            joined_names = [n for n in step['bodies'] if n in bodies]
            other_names = [n for n in installed if n in bodies and n not in joined_names]

            for n in new_present:
                items.append((n, _ADD_NOW_COLOR, full_view[n]['visible'], full_view[n]['hidden']))
            for n in joined_names:
                if n in new_present:
                    continue
                items.append((n, _INSTALLED_COLOR, full_view[n]['visible'], full_view[n]['hidden']))
            for n in other_names:
                items.append((n, _INSTALLED_COLOR, full_view[n]['visible'], []))
            # The combined view's hidden half for these other bodies is genuinely
            # hidden behind some OTHER still-opaque installed body even
            # with the joined body excluded - dropped, same as any other
            # installed body's unrelated internal detail, so the page
            # doesn't fill up with every gear's own teeth regardless of
            # relevance to this step.
        else:
            for n in new_present:
                # a fastener that comes along inside a placed part/sub-assembly
                # (e.g. a coupler's own screw) isn't what this step is about:
                # its hidden lines would only clutter, so only its visible
                # edges are drawn. Fastener steps still show them.
                hid = [] if is_fastener_name(n) else full_view[n]['hidden']
                items.append((n, _ADD_NOW_COLOR, full_view[n]['visible'], hid))
            for n in installed:
                if n in bodies and n not in new_present:
                    items.append((n, _INSTALLED_COLOR, full_view[n]['visible'], []))

        # real depth order: farthest body first, nearest last, so where lines
        # cross, whichever part is actually in front is painted on top
        # (colour doesn't decide - a gray part in front covers a red one)
        items.sort(key=lambda it: depth_of(it[0]))
        # one dashed line per place: a hidden edge lying on top of another
        # (same circle seen from two parts, collinear edges) is dropped, so
        # dashes never overprint with different phases into a solid look
        import vector_hlr
        covered = vector_hlr.PointIndex([])
        deduped = {}
        for it in sorted(items, key=lambda it: it[1] != _ADD_NOW_COLOR):
            deduped[it[0]] = vector_hlr.drop_covered(it[3], covered)
        items = [(n, col, vis, deduped[n]) for n, col, vis, _h in items]

        # dashed (hidden) lines underneath, then solid lines in true depth
        # order (far to near) - red or gray, whichever is in front is on top
        for _n, color, vis, hid in items:
            _draw_iso_group(c, {'visible': [], 'hidden': hid}, color, to_page, show_hidden=True, show_visible=False)
        for _n, color, vis, hid in items:
            _draw_iso_group(c, {'visible': vis, 'hidden': []}, color, to_page, show_hidden=False, show_visible=True)

        c.showPage()

    if unplaced_joints:
        c.setFont('Helvetica-Bold', 16)
        c.drawString(margin, page_h - margin - 18, 'Fastener groups not in the build order (verify manually)')
        c.setFont('Helvetica', 12)
        y = page_h - margin - 46
        for group in unplaced_joints:
            c.drawString(margin, y, f'- {_fastener_label(group["fasteners"])}')
            y -= 18
        c.showPage()

    c.save()
