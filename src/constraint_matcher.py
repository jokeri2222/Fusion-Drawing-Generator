"""
Match holes/threads from project_data.json (the Fusion API export) to the
actual cylindrical faces found in each body's STEP file.

Name-based matching doesn't work here: project_data.json's `component`
field only identifies the sub-assembly a hole/thread lives in (e.g.
"Pump Gears", "Pump Housing + lids+ motor cage", or a standalone fastener
component like "Hexagon Socket Head Cap Screw ISO 4762 - M5 x 20..."), not
which of the 13 individually-exported bodies it belongs to. So instead this
matches by diameter: each cylindrical face on a body is paired with the
closest-diameter unclaimed hole/thread entry, within a tolerance (cylinder
diameter as modeled can differ slightly from the nominal hole/tap
diameter - e.g. a thread's modeled crest vs. the hole's tap diameter - so
an exact match isn't expected).

Holes/threads on fastener components (screws, etc.) are expected to go
unmatched against our 13 body STEP files, since those parts aren't in the
per-body export - that's normal, not a bug.

Matching is a shared, global process across all bodies in one run (holes
are consumed once matched) so the same real-world hole can't be
double-claimed by two different bodies.
"""

import json
import math
import os
from dataclasses import dataclass, field

from step_loader import load_step, _merge_colinear_fillets
import tolerance as tolerance_mod

DEFAULT_DIAMETER_TOLERANCE_MM = 0.3

# Two cylindrical faces belong to the same physical hole if their axes are
# parallel within this cosine tolerance (1 - cos(angle) < this)...
AXIS_ANGLE_TOLERANCE = 0.02
# ...AND the perpendicular distance between the two (infinite) axis lines is
# under this - i.e. they are actually the same line, not just parallel.
AXIS_LINE_DISTANCE_TOLERANCE_MM = 0.15
# ...AND their segments along that shared line actually touch or overlap
# (within this small slack, for the seam between adjoining counterbore/
# countersink faces) - two holes drilled from opposite faces of a part can
# sit on the exact same infinite axis line while being physically far apart
# (e.g. a front port and a back port symmetric about the part's center), and
# must NOT be merged into one fake stepped-hole stack just because they're
# collinear.
AXIS_SEGMENT_GAP_TOLERANCE_MM = 1.0
# How close a hole cluster's end must sit to the body's true extent along
# the hole axis to count as reaching the outer surface there (THRU) rather
# than stopping short (blind).
THRU_MARGIN_MM = 0.5


@dataclass
class MatchPool:
    """Holds holes/threads not yet claimed by a body, shared across the
    whole matching run.
    """
    holes: list
    threads: list
    claimed_holes: set = field(default_factory=set)
    claimed_threads: set = field(default_factory=set)

    @classmethod
    def from_project_data(cls, project_data):
        return cls(holes=list(project_data.get('holes', [])),
                    threads=list(project_data.get('threads', [])))

    def unclaimed_holes(self):
        return [(i, h) for i, h in enumerate(self.holes) if i not in self.claimed_holes]

    def unclaimed_threads(self):
        return [(i, t) for i, t in enumerate(self.threads) if i not in self.claimed_threads]


def _geometry_pass(step_body, tol_fn=None):
    """Everything about one body that can be determined from its STEP
    geometry alone, with NO pool interaction yet - hole/thread designation
    resolution is deliberately deferred to a single GLOBAL pass across
    every body (see `_resolve_all_labels`), not done per-body here. Doing
    it per-body, in whatever order `glob()` happens to list STEP files,
    let an unrelated plain hole on one body "steal" a thread pool entry
    that diameter-matched it only coarsely, before a much better-fitting
    hole on a later-processed body ever got a chance at it - observed
    directly: a design's two G1/4 (BSP pipe thread) ports were claimed by
    an alphabetically-earlier body's ordinary ~13mm holes instead of the
    body that actually has the G1/4 ports, because 13.00mm sits within
    tolerance of G1/4's ~13.157mm major diameter too.

    NOTE: a true global best-fit resolution (assign the single closest
    (task, pool-entry) pair in the WHOLE design first, regardless of body)
    was tried and reverted - it fixed that specific case but broke several
    already-correct ones, because project_data.json's diameters aren't
    precise enough for pure numerical closeness to reliably pick the RIGHT
    body over a coincidentally-slightly-closer WRONG one; a large, obvious
    "8X" group on the correct body could lose its designation to an
    unrelated 1-off coincidental match elsewhere with a marginally smaller
    diff. Resolution is therefore still done per-body in file order (see
    `match_all_bodies`) - the G1/4-style cross-body mismatch remains a
    known limitation, not solved here.

    Returns {'shape_groups': [...], 'other_matched': [...] (fillets/
    chamfers/slots/pockets - no pool needed), 'unmatched_faces': [...]}.
    """
    # Only internal (concave) cylindrical faces are candidate holes/bores.
    # External (convex) cylinders are round stock, bosses, shafts, etc. -
    # never what a Hole/Thread feature describes, so never worth matching
    # or inferring as a hole.
    all_cyl_faces = step_body.cylindrical_faces()
    internal_all = [f for f in all_cyl_faces if f.is_internal]
    external_faces = [f for f in all_cyl_faces if not f.is_internal]

    # A slot's rounded end is an internal cylindrical face too, but it only
    # wraps part of the circle (is_slot_end) - matching it against
    # project_data.json's round-hole diameters, or reporting it as
    # "inferred hole d=X", would misrepresent a slot as a round hole. Split
    # these off before hole/thread matching and summarize them separately.
    #
    # But NOT every partial-span internal cylinder is a genuine slot end -
    # a real semicircular slot cap wraps close to 180 degrees (see
    # SLOT_END_SPAN_RANGE_DEG). A small internal EDGE-BREAK fillet (e.g.
    # rounding where a pocket's vertical wall meets its floor, or a small
    # transition near a slot) is ALSO a partial-span internal cylinder, but
    # with a much smaller/different span - confirmed on real geometry
    # (Housing had internal partial-span faces at 27.7/78.5 degrees
    # alongside genuine 180-degree slot ends; Motor Mount had several at
    # 43.8-86.1 degrees; Motor Custom Key at 90 degrees) - all of these were
    # being unconditionally reported as "SLOT END", a real mislabeling bug
    # ("these are fillets, not slot ends"). Only faces actually within the
    # semicircular slot-end range go to `_summarize_slots`; everything else
    # with a partial span is routed to fillet detection instead, exactly
    # like the external straight-edge-fillet case.
    internal_faces = [f for f in internal_all if not f.is_slot_end]
    _slot_span_lo, _slot_span_hi = SLOT_END_SPAN_RANGE_DEG
    slot_end_faces = [
        f for f in internal_all
        if f.is_slot_end and _slot_span_lo <= f.angular_span_deg <= _slot_span_hi
    ]
    internal_fillet_faces = [
        f for f in internal_all
        if f.is_slot_end and not (_slot_span_lo <= f.angular_span_deg <= _slot_span_hi)
    ]

    # Group internal faces into real physical holes first: a counterbore or
    # countersink is TWO (or more) cylindrical faces sharing one axis line,
    # not two unrelated holes - matching/reporting them individually used to
    # produce confusing duplicate-looking callouts (e.g. two separate
    # "d=30.2mm" and "d=28.0mm" rows for what is really one stepped hole).
    measurements = [
        _measure_hole_cluster(group, step_body)
        for group in _cluster_faces_by_axis(internal_faces)
    ]
    shape_groups_by_key = {}
    shape_order = []
    for m in measurements:
        key = m['shape_key']
        if key not in shape_groups_by_key:
            shape_groups_by_key[key] = []
            shape_order.append(key)
        shape_groups_by_key[key].append(m)
    shape_groups = [shape_groups_by_key[key] for key in shape_order]

    other_matched = []
    other_matched.extend(_summarize_fillets_and_chamfers(step_body, tol_fn=tol_fn, extra_fillet_faces=internal_fillet_faces))
    other_matched.extend(_summarize_slots(slot_end_faces, step_body.bounding_box, tol_fn=tol_fn))
    other_matched.extend(_summarize_pockets(step_body))

    return {
        'shape_groups': shape_groups,
        'other_matched': other_matched,
        'unmatched_faces': external_faces,
    }


def _resolve_all_groups(all_groups, pool, tolerance):
    """Resolve thread/hole designations for EVERY hole-cluster shape group
    in the whole design at once, partitioned by designation (for threads)
    or exact diameter (for holes) - within each partition, the globally
    closest-fitting group(s) claim that partition's pool entries first,
    regardless of which body they belong to or what order bodies happen to
    be processed in.

    This is deliberately NOT a single fully-global best-fit across every
    partition at once (tried and reverted - see git history/session notes):
    that let a coincidentally-tight match in one partition's competition
    (e.g. a hole diameter that happens to sit unusually close to some
    unrelated thread's expected size) starve a completely different,
    already-obviously-correct match in another partition, because both
    competed on raw diff in one pooled ranking. Partitioning by designation/
    diameter first means M6x1's competition never affects M8x1.25's, and
    neither affects G1/4's - each designation only competes against itself,
    which is exactly the scope where "closest wins" is actually meaningful
    (concretely verified: within G1/4's own diameter, Housing's real port
    has a smaller diff (0.1525mm) than the other bodies' coincidental
    matches (0.1570mm) - global-within-partition correctly picks Housing).

    A group of N physical instances claims up to N matching pool entries,
    not just 1 - once project_data.json correctly records multiple real
    instances of a repeated feature, claiming only one left the other
    real entries sitting unclaimed as bait for an unrelated body's
    coincidentally-close diameter to steal (observed directly: a plain
    8-hole bolt pattern got mislabeled with a leftover thread designation
    this way before this was fixed).

    Returns {(id(group), segment_index): label}.
    """
    tasks = []  # each: (group, segment_index, diameter)
    for group in all_groups:
        rep = group[0]
        for i, seg in enumerate(rep['order']):
            tasks.append((group, i, seg['diameter']))

    labels = {}

    def task_key(t_idx):
        group, seg_i, _diameter = tasks[t_idx]
        return (id(group), seg_i)

    # --- Threads, partitioned by designation - each designation's pool
    # entries only ever compete against tasks eligible for THAT designation.
    designations = {}
    for idx, item in enumerate(pool.threads):
        d = item.get('designation')
        if d:
            designations.setdefault(d, []).append(idx)

    for designation, entry_indices in designations.items():
        expected_diams = _expected_diameters_from_designation(designation)
        if not expected_diams:
            continue
        candidates = []
        for t_idx, (group, seg_i, diameter) in enumerate(tasks):
            if task_key(t_idx) in labels:
                continue
            best_diff = min((abs(diameter - e) for e in expected_diams), default=None)
            if best_diff is not None and best_diff <= tolerance:
                candidates.append((best_diff, t_idx))
        candidates.sort(key=lambda c: c[0])

        remaining = [idx for idx in entry_indices if idx not in pool.claimed_threads]
        claimed_groups = set()
        for _diff, t_idx in candidates:
            group, seg_i, _diameter = tasks[t_idx]
            if task_key(t_idx) in labels or id(group) in claimed_groups or not remaining:
                continue
            need = len(group)
            take, remaining = remaining[:need], remaining[need:]
            pool.claimed_threads.update(take)
            labels[task_key(t_idx)] = designation
            claimed_groups.add(id(group))

    # --- Holes, partitioned by exact diameter value - same reasoning.
    hole_value_groups = {}
    for idx, item in enumerate(pool.holes):
        value = item.get('hole_diameter_mm')
        if value is not None:
            hole_value_groups.setdefault(value, []).append(idx)

    for diam_value, entry_indices in hole_value_groups.items():
        candidates = []
        for t_idx, (group, seg_i, diameter) in enumerate(tasks):
            if task_key(t_idx) in labels:
                continue
            diff = abs(diameter - diam_value)
            if diff <= tolerance:
                candidates.append((diff, t_idx))
        candidates.sort(key=lambda c: c[0])

        remaining = [idx for idx in entry_indices if idx not in pool.claimed_holes]
        claimed_groups = set()
        for _diff, t_idx in candidates:
            group, seg_i, _diameter = tasks[t_idx]
            if task_key(t_idx) in labels or id(group) in claimed_groups or not remaining:
                continue
            need = len(group)
            take, remaining = remaining[:need], remaining[need:]
            pool.claimed_holes.update(take)
            rep_data = pool.holes[take[0]]
            if rep_data.get('is_tapped') and rep_data.get('tap_designation'):
                label = rep_data['tap_designation']
            else:
                label = f'Ø{tasks[t_idx][2]:.3f}'
            labels[task_key(t_idx)] = label
            claimed_groups.add(id(group))

    # A thread/hole can legitimately span an assembly boundary - a shared
    # clearance hole through two mating parts, threaded once but exported
    # as separate per-body STEP files, is a real feature on BOTH bodies,
    # not a "duplicate" one of them is stealing. Likewise a body can be
    # independently modified or deleted later without the JSON being
    # regenerated to match. So project_data.json's entry COUNT for a given
    # designation is a useful signal for resolving genuinely AMBIGUOUS
    # cases above, but shouldn't be treated as a hard ceiling once it runs
    # out: a task still unresolved here because its designation's pool
    # count was exhausted, but whose diameter is a near-exact match (much
    # tighter than the general tolerance - this is deliberately stricter,
    # to avoid re-introducing the coincidental cross-body mismatches the
    # partitioning above exists to prevent), gets that designation anyway.
    VERY_CONFIDENT_TOLERANCE = 0.05
    for t_idx, (group, seg_i, diameter) in enumerate(tasks):
        key = task_key(t_idx)
        if key in labels:
            continue
        best = None
        for designation in designations:
            for e in _expected_diameters_from_designation(designation):
                diff = abs(diameter - e)
                if diff <= VERY_CONFIDENT_TOLERANCE and (best is None or diff < best[1]):
                    best = (designation, diff)
        labels[key] = best[0] if best else f'Ø{diameter:.3f}'

    # Two segments of the SAME physical hole can share the same diameter
    # (e.g. a straight bore reported as two segments either side of a small
    # relief) - each was resolved as its own independent task above, so
    # only one of them may have actually won a designation while its
    # identical-diameter sibling fell back to a bare "Ø" label. Propagate
    # a resolved designation to any bare sibling in the same group with
    # the same rounded diameter - they're clearly the same real feature.
    for group in all_groups:
        order = group[0]['order']
        designation_by_diameter = {}
        for i, seg in enumerate(order):
            label = labels[(id(group), i)]
            if not label.startswith('Ø'):
                designation_by_diameter.setdefault(round(seg['diameter'], 2), label)
        for i, seg in enumerate(order):
            key = (id(group), i)
            if labels[key].startswith('Ø'):
                match = designation_by_diameter.get(round(seg['diameter'], 2))
                if match:
                    labels[key] = match

    # Display-format any thread designation (e.g. raw BSP pipe sizes) - done
    # last, after every resolution/propagation pass above, since those all
    # compare/store the RAW designation (needed for _expected_diameters_from_designation
    # and the startswith('Ø') checks above to keep working).
    for key, label in labels.items():
        if not label.startswith('Ø'):
            labels[key] = _format_thread_designation(label)

    return labels


def _axis_signed_range(face, origin, direction):
    """(lo, hi) signed distance of `face`'s finite extent along `direction`,
    measured from `origin` - lets two faces' segments be compared for actual
    overlap/adjacency, not just membership on the same infinite line.
    """
    s1 = sum((face.start_point[k] - origin[k]) * direction[k] for k in range(3))
    s2 = sum((face.end_point[k] - origin[k]) * direction[k] for k in range(3))
    return min(s1, s2), max(s1, s2)


def _cluster_faces_by_axis(faces):
    """Group cylindrical faces that share one physical axis line (parallel
    direction AND coincident, not just parallel elsewhere) AND whose
    segments along that line are actually contiguous - the faces of a
    single stepped hole (through-bore + counterbore + tapped section, etc).

    Collinearity alone isn't enough: two holes drilled from opposite faces
    of a part (e.g. a front G1/4 port and a back G1/4 port, symmetric about
    the part's center) can sit on the exact same infinite axis line while
    being physically unrelated, far-apart holes - merging them would fuse
    two real features into one bogus multi-segment "stack" and silently
    drop one of them from its own callout/cross entirely.
    """
    clusters = []
    used = [False] * len(faces)
    for i, f in enumerate(faces):
        if used[i]:
            continue
        group = [f]
        group_lo, group_hi = _axis_signed_range(f, f.axis_location, f.axis_direction)
        used[i] = True
        # Re-scan from the start each time a face is added, since a later
        # face might only become contiguous with the (now-extended) group
        # range, not with the original seed face directly.
        changed = True
        while changed:
            changed = False
            for j in range(i + 1, len(faces)):
                if used[j]:
                    continue
                g = faces[j]
                dot = sum(a * b for a, b in zip(f.axis_direction, g.axis_direction))
                if abs(dot) < 1 - AXIS_ANGLE_TOLERANCE:
                    continue
                diff = tuple(g.axis_location[k] - f.axis_location[k] for k in range(3))
                along = sum(diff[k] * f.axis_direction[k] for k in range(3))
                perp_sq = sum((diff[k] - along * f.axis_direction[k]) ** 2 for k in range(3))
                if math.sqrt(perp_sq) > AXIS_LINE_DISTANCE_TOLERANCE_MM:
                    continue
                g_lo, g_hi = _axis_signed_range(g, f.axis_location, f.axis_direction)
                if g_lo > group_hi + AXIS_SEGMENT_GAP_TOLERANCE_MM or g_hi < group_lo - AXIS_SEGMENT_GAP_TOLERANCE_MM:
                    continue
                group.append(g)
                used[j] = True
                group_lo, group_hi = min(group_lo, g_lo), max(group_hi, g_hi)
                changed = True
        clusters.append(group)
    return clusters


def _measure_hole_cluster(group, step_body):
    """Turn one axis-aligned group of cylindrical faces into pure geometric
    measurements: real per-segment depth (from the cylinder surface's own V
    parameter, which is a true length, not a normalized 0-1 value), and
    THRU-vs-blind (by comparing the cluster's axial span to the body's own
    extent along that same axis). Deliberately does NOT touch the
    thread/hole pool yet - see _label_hole_group for why that has to
    happen after grouping identical measurements, not before.
    """
    ref = group[0]
    ref_dir = ref.axis_direction
    ref_point = ref.axis_location
    ref_offset = sum(ref_point[k] * ref_dir[k] for k in range(3))

    def signed(pt):
        return sum((pt[k] - ref_point[k]) * ref_dir[k] for k in range(3))

    segments = []
    for f in group:
        s1, s2 = signed(f.start_point), signed(f.end_point)
        segments.append({'face': f, 'lo': min(s1, s2), 'hi': max(s1, s2), 'diameter': f.diameter})
    segments.sort(key=lambda s: s['lo'])

    cluster_lo, cluster_hi = segments[0]['lo'], segments[-1]['hi']

    body_lo_abs, body_hi_abs = step_body.extent_along_direction(ref_dir)
    body_lo, body_hi = body_lo_abs - ref_offset, body_hi_abs - ref_offset

    near_lo = abs(cluster_lo - body_lo) < THRU_MARGIN_MM
    near_hi = abs(cluster_hi - body_hi) < THRU_MARGIN_MM
    is_thru = near_lo and near_hi
    entry_at_hi = near_hi and not near_lo

    order = list(reversed(segments)) if entry_at_hi else segments
    anchor_v = cluster_hi if entry_at_hi else cluster_lo
    anchor_point = tuple(ref_point[k] + anchor_v * ref_dir[k] for k in range(3))

    # Rounded (diameter, depth) per segment, in entry-to-bottom order - this
    # is what makes two physically-identical holes (e.g. 8 instances of the
    # same tapped hole, all created by ONE Fusion feature from 8 selected
    # sketch points at once) group together, entirely from geometry, before
    # either has been matched against project_data.json.
    #
    # Deliberately does NOT include is_thru: that classification compares
    # against the body's GLOBAL height extent (`extent_along_direction`),
    # not the LOCAL material thickness at each hole's own XY position - a
    # pattern of otherwise-identical holes spread across a part with
    # non-uniform thickness (a thin flange next to a thick boss) could see
    # one instance correctly read as THRU and another, at a spot that
    # doesn't reach the part's single tallest point elsewhere, misread as
    # blind. Letting that split otherwise-identical holes into separate
    # groups was losing their "NX" consolidation (and the other instances'
    # center-cross marks) over what's a classification edge case, not a
    # real geometric difference - grouping only by the diameter/depth
    # stack is more robust; is_thru for the merged group's callout still
    # comes from its own representative instance (see _finalize_hole_group).
    shape_key = tuple((round(s['diameter'], 2), round(s['hi'] - s['lo'], 2)) for s in order)

    return {
        'axis_point': anchor_point,
        'axis_direction': ref_dir,
        'order': order,
        'is_thru': is_thru,
        'max_diameter_mm': max(s['diameter'] for s in segments),
        # The diameter of the segment at the visible opening - what the
        # leader's arrow should actually touch (the true edge of the
        # circle you see in the drawing), not the anchor point's own
        # (usually much smaller) center dot.
        'entry_diameter_mm': order[0]['diameter'],
        'shape_key': shape_key,
    }


def _finalize_hole_group(measurements, labels_by_segment, tol_fn=None):
    """Build the final callout(s) for a group of geometrically-identical
    hole-cluster measurements (same diameter/depth stack, grouped by
    _measure_hole_cluster's shape_key) from ALREADY-RESOLVED per-segment
    labels (see _resolve_all_labels) - "NX <callout>" if there's more than
    one instance, applying the one resolution to the whole group.

    Returns a LIST of one `hole_cluster` entry PER SEGMENT (diameter/depth
    step) - a counterbore's spot face, bore, and thread relief are each
    their own numbered balloon, not lines stacked under one shared number,
    per explicit request ("each feature should be separate" extends to a
    single stepped hole's own segments too, not just to different
    features). All segments of one physical hole share the same
    `axis_point`/`axis_direction` (they're the same axis line - in the
    view that hole is actually shown in, looking straight down that axis,
    every segment's circle projects to the exact same 2D center regardless
    of its depth, so there's nothing to gain from a separate 3D anchor
    point per segment), but each gets its OWN `entry_diameter_mm` (that
    segment's own diameter, not always the outermost one) so its leader
    touches ITS OWN circle - the outer visible one for the entry segment,
    an inner hidden (dashed) one for a step further down - instead of
    every segment's arrow bunching up on the single outermost circle.

    Grouping (by geometry) happens before label resolution, and label
    resolution happens once GLOBALLY across the whole design before this
    runs - see _geometry_pass's docstring for why both of those matter:
    project_data.json records a single Hole/Thread feature as ONE entry
    even when it was created from several selected sketch points at once,
    and a per-body/per-instance-first-come resolution let an unrelated
    hole on one body steal a pool slot meant for a better match elsewhere.

    `tol_fn(value_mm) -> tolerance_mm`, if given, appends a "±X.XX" next
    to each real dimension value (a bare diameter, or a segment's depth -
    never a thread designation string itself, e.g. "M6x1" has no tolerance
    of its own here, only its depth does).
    """
    rep = measurements[0]
    order = rep['order']
    is_thru = rep['is_thru']
    other_points = [m['axis_point'] for m in measurements[1:]]

    entries = []
    for i, seg in enumerate(order):
        is_last = i == len(order) - 1
        depth = seg['hi'] - seg['lo']
        label = labels_by_segment[i]
        is_diameter = label.startswith('Ø')
        if tol_fn and is_diameter:
            label = f'{label} ±{tol_fn(seg["diameter"]):.3f}'
        if is_diameter:
            label += ' mm'
        if is_last and is_thru:
            line = f'{label}  THRU ALL'
        else:
            depth_text = f'{depth:.3f}' + (f' ±{tol_fn(depth):.3f}' if tol_fn else '') + ' mm'
            line = f'{label}  {depth_text} DEEP'

        if len(measurements) > 1:
            line = f'{len(measurements)}X  {line}'

        entries.append({
            'kind': 'hole_cluster',
            'axis_point': rep['axis_point'],
            'axis_direction': rep['axis_direction'],
            'callout_lines': [line],
            'is_thru': is_thru and is_last,
            'max_diameter_mm': rep['max_diameter_mm'],
            'entry_diameter_mm': seg['diameter'],
            'other_instance_points': other_points,
        })
    return entries


def _summarize_slots(slot_end_faces, bbox, tol_fn=None):
    """Group slot-end partial-cylinder faces by rounded radius AND axis
    orientation (same reasoning as fillets - two same-radius slot ends on
    differently-oriented edges are physically distinct features, and each
    needs its own representative leader in whichever view actually shows
    it, not just whichever happened to be first). A slot has exactly two
    rounded ends, so roughly half the face count of a given radius/
    orientation is the number of slots of that radius (an estimate - a
    design with an odd number of matching partial cylinders for unrelated
    reasons would throw this off slightly, but for ordinary obround slots
    this holds). Gets a real in-view leader like a hole/fillet, not just a
    title-block-only summary - there is no title block summary any more,
    so this is now every slot's only representation in the drawing.
    """
    if not slot_end_faces:
        return []
    by_key = {}
    for f in slot_end_faces:
        view = _best_view_for_point(f.surface_midpoint, f.axis_direction, bbox)
        key = (round(f.radius, 2), view)
        by_key.setdefault(key, []).append(f)
    entries = []
    for (radius, view), faces in sorted(by_key.items()):
        rep = faces[0]
        anchor = rep.surface_midpoint
        estimated = max(1, round(len(faces) / 2))
        label = f'R{radius:.3f}'
        if tol_fn:
            label += f' ±{tol_fn(radius):.3f}'
        label += ' mm SLOT END' + (' TYP' if len(faces) > 1 else '')
        other_points = [f.surface_midpoint for f in faces[1:]]
        length_pairs = _pair_slot_ends(faces)
        entries.append({
            'kind': 'slot_summary',
            'axis_point': anchor,
            'axis_direction': rep.axis_direction,
            'forced_view': view,
            'visible_views': _visible_views_for_feature(anchor, rep.axis_direction, bbox),
            'callout_lines': [label],
            'data': {'radius_mm': radius, 'face_count': len(faces), 'estimated_slots': estimated},
            'other_instance_points': other_points,
            # A slot's own overall LENGTH (center-to-center between its two
            # rounded ends) is a real dimension a machinist needs, exactly
            # like a hole's diameter - a bare end-radius callout alone
            # never conveyed how long the slot actually is. Pair up this
            # radius/view group's faces into physical slots (each real slot
            # contributes exactly 2 end faces) by simple nearest-neighbor
            # matching - robust regardless of the slot's orientation in
            # this view, since it works directly off real 3D distance
            # rather than assuming which coordinate varies along the slot's
            # length.
            'slot_length_pairs': length_pairs,
            # The slot's own WIDTH (perpendicular to its length) - for a
            # true obround/stadium slot (straight sides tangent to both
            # round ends), that width is always exactly the end radius x2,
            # a geometric certainty that needs no extra face detection
            # (the groove's straight side walls are planar, not
            # cylindrical, so they're invisible to cylindrical_faces() -
            # this sidesteps needing to find them at all). Per explicit
            # request: a long slot/groove only ever got a length dimension
            # before, never conveying how WIDE it actually is.
            'slot_width_pairs': _slot_width_pairs(length_pairs, radius, rep.axis_direction),
        })
    return entries


def _slot_width_pairs(length_pairs, radius, axis_direction):
    """For each (p1, p2, length) slot-length pair, the corresponding width
    dimension's own two endpoints: centered on the slot's midpoint, offset
    by `radius` along the direction perpendicular to both the slot's own
    cylinder axis (`axis_direction`, its "depth" direction) and its length
    axis (p2 - p1) - i.e. straight across the groove, in the same plane the
    length dimension already lies in.
    """
    results = []
    for p1, p2, length_mm in length_pairs:
        length_dir = tuple((b - a) / length_mm for a, b in zip(p1, p2)) if length_mm > 1e-9 else (1, 0, 0)
        # cross(axis_direction, length_dir) - perpendicular to both, so it
        # points straight across the slot's own width within its cut plane.
        ax, ay, az = axis_direction
        lx, ly, lz = length_dir
        cross = (ay * lz - az * ly, az * lx - ax * lz, ax * ly - ay * lx)
        cross_len = math.sqrt(sum(c * c for c in cross)) or 1.0
        width_dir = tuple(c / cross_len for c in cross)
        mid = tuple((a + b) / 2 for a, b in zip(p1, p2))
        w1 = tuple(m - radius * d for m, d in zip(mid, width_dir))
        w2 = tuple(m + radius * d for m, d in zip(mid, width_dir))
        results.append((w1, w2, 2 * radius))
    return results


# A genuine obround slot's rounded END is a true semicircular cap - close
# to 180 degrees of arc. A partial-cylinder face well short of that (e.g.
# ~30-80 degrees) is something else geometrically - typically a long
# rounded edge transition running the full length of a feature, caught by
# the same "less than a full circle" classification `is_slot_end` uses,
# but NOT a real end-to-end-pairable slot cap. Pairing those and reporting
# the perpendicular distance between two unrelated nearby edges as a "slot
# length" would be a real, actively misleading fabricated dimension - worse
# than just not showing a length at all - so only faces in this range are
# even considered for length-pairing.
SLOT_END_SPAN_RANGE_DEG = (150.0, 210.0)


def _pair_slot_ends(faces):
    """Greedy nearest-neighbor pairing of same-radius/orientation slot-end
    faces into physical slots (each real slot has exactly 2 ends) -
    returns [(point1, point2, length_mm), ...]. Faces that end up
    unpaired (an odd count, from an unrelated partial-cylinder coincidence)
    are simply left out - no length claim is made without a real pair.
    """
    lo, hi = SLOT_END_SPAN_RANGE_DEG
    remaining = [f for f in faces if lo <= f.angular_span_deg <= hi]
    pairs = []
    while len(remaining) >= 2:
        a = remaining.pop(0)
        pa = a.surface_midpoint
        best_i, best_dist = None, None
        for i, b in enumerate(remaining):
            pb = b.surface_midpoint
            dist = math.dist(pa, pb)
            if best_dist is None or dist < best_dist:
                best_dist, best_i = dist, i
        if best_i is not None:
            b = remaining.pop(best_i)
            pairs.append((pa, b.surface_midpoint, best_dist))
    return pairs


def _summarize_pockets(step_body):
    """Recessed planar faces (pockets, pads, stepped shoulders, counterbore
    floors not already claimed as a real hole/counterbore, etc.) found by
    `StepBody.pocket_faces()`'s heuristic. Grouped by axis orientation (the
    face's own normal) rather than lumped into one entry - a design usually
    has pockets facing multiple directions (e.g. some milled from the top,
    some from the side), so this naturally spreads the callout across
    whichever views actually show each group face-on, instead of reporting
    everything as a single instance-less note. Each group gets one
    representative leader (largest-area-first pick is arbitrary but stable)
    with a count + area range for that group - the heuristic can't
    distinguish a genuine pocket from an incidental stepped face, so this
    still isn't presented with false per-feature precision.
    """
    pockets = step_body.pocket_faces()
    if not pockets:
        return []
    bbox = step_body.bounding_box
    by_view = {}
    for p in pockets:
        view = _best_view_for_point(p['centroid'], p['normal'], bbox)
        by_view.setdefault(view, []).append(p)
    entries = []
    for view, faces in sorted(by_view.items()):
        rep = faces[0]
        areas = [p['area_mm2'] for p in faces]
        label = f'POCKET x{len(faces)}  {min(areas):.3f}-{max(areas):.3f} mm²'
        entries.append({
            'kind': 'pocket_summary',
            'axis_point': rep['centroid'],
            'axis_direction': rep['normal'],
            'forced_view': view,
            'visible_views': _visible_views_for_feature(rep['centroid'], rep['normal'], bbox),
            'callout_lines': [label],
            'data': {'count': len(faces), 'min_area_mm2': min(areas), 'max_area_mm2': max(areas)},
            'other_instance_points': [p['centroid'] for p in faces[1:]],
        })
    return entries


# The six orthographic views' own camera directions, duplicated (not
# imported) from drawing_generator.py's VIEW_DIRECTIONS to avoid a circular
# import (drawing_generator.py already imports match_all_bodies from this
# module) - only the direction vectors are needed here, not the page x_dir.
_ORTHO_VIEW_AXES = {
    # Must exactly match drawing_generator.py's own VIEW_DIRECTIONS (kept
    # duplicated here only to avoid a circular import - drawing_generator.py
    # already imports match_all_bodies from this module). TOP/BOTTOM were
    # swapped there since Fusion exports no "which way is up" metadata and
    # the original guess landed backwards for this project's real parts -
    # this copy was NOT updated at the same time, silently reintroducing
    # the exact same inverted-TOP/BOTTOM bug for every fillet/chamfer/slot/
    # pocket/hole view assignment (all of which go through
    # _best_view_for_point() below), even after the rendering itself was
    # fixed - a real, previously undiagnosed source of "features in the
    # wrong view" reports.
    'TOP': (0, 0, 1), 'BOTTOM': (0, 0, -1),
    'FRONT': (0, -1, 0), 'BACK': (0, 1, 0),
    'RIGHT': (1, 0, 0), 'LEFT': (-1, 0, 0),
}


# How close (as a fraction of the body's own extent along that axis) a
# feature's centroid must sit to a given side's outer face to count as
# genuinely visible from that side's view - see `_visible_views_for_feature`.
# A feature that's a true surface feature (fillet/chamfer/pocket/slot) is
# ALWAYS this close to at least one side (the one `_best_view_for_point`
# already picks); this only decides whether it's ALSO close enough to a
# second (or third) side - e.g. a corner fillet blending three faces at
# once - to earn a callout on that view too, instead of just its single
# "primary" one.
VISIBLE_VIEW_MARGIN_FRACTION = 0.2


def _visible_views_for_feature(centroid, direction, bbox):
    """Every orthographic view a fillet/chamfer/slot/pocket feature is
    plausibly visible from - not just the single "best" one. Per explicit
    request, these features should be named/dimensioned in every view where
    they actually show up (e.g. a corner fillet blending a top face and a
    side face is legitimately visible from both TOP and the side view), not
    confined to one arbitrarily-chosen view the way a hole_cluster still is.

    Always includes `_best_view_for_point`'s own pick (the feature's primary
    axis-dominant view - unchanged, still empirically verified in round 36).
    Additionally includes any OTHER view whose side the centroid sits within
    `VISIBLE_VIEW_MARGIN_FRACTION` of that side's own outer face - i.e. the
    feature is close enough to that face too to plausibly be part of its
    visible silhouette. This is a position-based approximation (no full
    per-face HLR occlusion test), but a feature that's genuinely only
    findable in its one primary view is unaffected: it will sit near the
    middle, not the extreme, of every OTHER axis, so no extra views get
    added for it.
    """
    primary = _best_view_for_point(centroid, direction, bbox)
    views = {primary}
    cx, cy, cz = centroid
    spans_and_coords = {
        'RIGHT': (bbox.xmax, bbox.xmax - bbox.xmin, cx),
        'LEFT': (bbox.xmin, bbox.xmax - bbox.xmin, cx),
        'BACK': (bbox.ymax, bbox.ymax - bbox.ymin, cy),
        'FRONT': (bbox.ymin, bbox.ymax - bbox.ymin, cy),
        'TOP': (bbox.zmax, bbox.zmax - bbox.zmin, cz),
        'BOTTOM': (bbox.zmin, bbox.zmax - bbox.zmin, cz),
    }
    for view, (extreme, span, coord) in spans_and_coords.items():
        if span <= 0:
            continue
        if abs(coord - extreme) <= VISIBLE_VIEW_MARGIN_FRACTION * span:
            views.add(view)
    return views


def _best_view_for_point(centroid, direction, bbox):
    """Which single orthographic view a fillet/chamfer/slot/pocket face is
    actually visible from - decided by the face's POSITION relative to the
    body, not by its reported axis/normal direction's sign.

    A torus/cylinder's "axis direction" (as OCC reports it) or a plane's
    normal both have a sign, but that sign isn't a reliable "which way does
    this face point" signal here: two physically-identical faces on
    opposite sides of a part (a fillet on the top edge vs. the bottom edge)
    can end up with the same or opposite reported signs depending on
    internal parametrization/construction order, not on which side they're
    actually on - grouping/assigning by that sign in an earlier version put
    a bottom-only feature's leader in the TOP view too, which is wrong: a
    face only recessed on the bottom face is genuinely NOT visible from
    TOP at all, so it must never get a leader there.

    `direction` only picks WHICH PAIR of opposite views is relevant (an
    axis mostly along Z means the TOP/BOTTOM pair, mostly Y means
    FRONT/BACK, mostly X means RIGHT/LEFT); which ONE of that pair the
    face actually belongs to is then decided by simple position - whichever
    of the two views' cameras the centroid sits nearer to.

    "Nearer" here means the LARGEST dot product with that view's own
    `VIEW_DIRECTIONS` vector (from `_ORTHO_VIEW_AXES`, kept in sync with
    drawing_generator.py's real ones), taken AFTER re-centering `centroid`
    on `bbox`'s own middle - confirmed empirically (not assumed) that the
    SIGN/direction part of this is right: a synthetic test box with a blind
    hole drilled from only one face, rendered through the real HLR pipeline
    for each of the six views, showed the hole's rim as VISIBLE precisely
    under the view whose direction vector's sign matches that face (e.g. a
    hole drilled from the -Y face rendered visible under FRONT, whose own
    direction is (0,-1,0) - the LARGER, not smaller, dot product). An
    earlier version of this function used `min()`, silently inverting every
    axis's near/far assignment (confirmed the same way, and the same way
    the very first, incorrect "TOP/BOTTOM must be swapped" diagnosis was
    reached) - `min()` was never correct for any of the three axis pairs,
    not just Z.

    The re-centering step was missing until a real part (a Housing whose
    STEP-exported Z bounding box was [-0.25, 51.95], preserving its true
    assembly position rather than being centered at the origin - expected,
    see bodychain.py's own docstring on why bodies keep their real absolute
    position) exposed it: comparing the raw dot product against world
    origin is only equivalent to "nearer to which face" when the body's own
    center happens to sit at that axis's zero - true for a synthetic test
    box built at the origin, false for essentially any real body exported
    from an assembly. A groove at Z=17.125 on that Housing (nowhere near
    either face - 17.4mm from BOTTOM, 34.8mm from TOP) was being sent to
    TOP purely because 17.125 > 0, even though it sits in the bottom HALF
    of the actual material. Subtracting the body's own bbox center first
    fixes this while leaving the already-verified sign/direction logic
    itself untouched - it only changes what "the two views' cameras" are
    measured relative to, not which one wins a given comparison.
    """
    ax, ay, az = (abs(c) for c in direction)
    axis_index = max(range(3), key=lambda i: (ax, ay, az)[i])
    center = ((bbox.xmin + bbox.xmax) / 2, (bbox.ymin + bbox.ymax) / 2, (bbox.zmin + bbox.zmax) / 2)
    centroid = tuple(c - m for c, m in zip(centroid, center))
    # axis_index 0/1/2 = X/Y/Z dominant -> RIGHT/LEFT, FRONT/BACK, TOP/BOTTOM
    # respectively - matches the (x, y, z) component order used above.
    pair = (('RIGHT', 'LEFT'), ('FRONT', 'BACK'), ('TOP', 'BOTTOM'))[axis_index]
    return max(pair, key=lambda name: sum(c * d for c, d in zip(centroid, _ORTHO_VIEW_AXES[name])))


def _summarize_fillets_and_chamfers(step_body, tol_fn=None, extra_fillet_faces=()):
    """Fillets and chamfers have no project_data.json counterpart at all
    (Fusion's Fillet/Chamfer features aren't scanned by the export script,
    and there'd be nothing to match by diameter anyway) - these come
    entirely from geometry. A part can carry dozens of individual fillet
    faces (one per blended edge), so rather than one callout per face,
    group them by rounded radius/angle and call out ONE representative
    instance per group with a real in-view leader (like a hole's), suffixed
    " TYP" when there's more than one - the standard drafting convention
    for "this dimension is typical for all similar unlabeled instances",
    e.g. Fusion/SolidWorks showing "R2.00 TYP" once instead of stamping the
    same radius on every rounded corner. `axis_point`/`axis_direction` let
    the same leader-routing/view-assignment machinery used for holes place
    these too, instead of being confined to a title-block-only summary.

    `extra_fillet_faces` are internal (concave) partial-span cylindrical
    faces `_geometry_pass` identified as edge-break fillets rather than
    genuine slot ends (see its own docstring) - real fillets that happen to
    be internal/concave (e.g. rounding where a pocket's wall meets its
    floor) instead of the external/convex kind `StepBody.fillet_faces()`
    itself already covers. Merged into the SAME radius-grouping pass (and
    the SAME colinear-patch dedup) as the torus/external-cylinder fillets,
    since they're the identical kind of feature, just concave.
    """
    entries = []
    bbox = step_body.bounding_box

    fillets = step_body.fillet_faces() + [
        {
            'face': f.face,
            'radius_mm': f.radius,
            'centroid': f.surface_midpoint,
            'axis_direction': f.axis_direction,
        }
        for f in extra_fillet_faces
    ]
    fillets = _merge_colinear_fillets(fillets)
    by_radius = {}
    for f in fillets:
        view = _best_view_for_point(f['centroid'], f['axis_direction'], bbox)
        key = (round(f['radius_mm'], 2), view)
        by_radius.setdefault(key, []).append(f)
    for (radius, view), faces in sorted(by_radius.items()):
        rep = faces[0]
        label = f'R{radius:.3f}'
        if tol_fn:
            label += f' ±{tol_fn(radius):.3f}'
        label += ' mm' + (' TYP' if len(faces) > 1 else '')
        entries.append({
            'kind': 'fillet_summary',
            'axis_point': rep['centroid'],
            'axis_direction': rep['axis_direction'],
            'forced_view': view,
            'visible_views': _visible_views_for_feature(rep['centroid'], rep['axis_direction'], bbox),
            'callout_lines': [label],
            'data': {'radius_mm': radius, 'count': len(faces)},
            'other_instance_points': [f['centroid'] for f in faces[1:]],
        })

    # A chamfer callout needs BOTH the angle and the linear leg length (e.g.
    # "1.000 x 45.0° CHAMFER") - an angle alone doesn't tell a machinist how
    # much material to remove. Grouped by (angle, depth, view) together,
    # since two chamfers sharing an angle but cut to a different depth are
    # physically distinct specs, not the same feature reported twice.
    chamfers = step_body.chamfer_faces()
    by_angle = {}
    for ch in chamfers:
        view = _best_view_for_point(ch['centroid'], ch['axis_direction'], bbox)
        key = (round(ch['angle_deg'], 1), round(ch['depth_mm'], 2), view)
        by_angle.setdefault(key, []).append(ch)
    for (angle, depth, view), faces in sorted(by_angle.items()):
        rep = faces[0]
        label = f'{depth:.3f}'
        if tol_fn:
            label += f' ±{tol_fn(depth):.3f}'
        label += f' mm x {angle:.1f}° CHAMFER' + (' TYP' if len(faces) > 1 else '')
        entries.append({
            'kind': 'chamfer_summary',
            'axis_point': rep['centroid'],
            'axis_direction': rep['axis_direction'],
            'forced_view': view,
            'visible_views': _visible_views_for_feature(rep['centroid'], rep['axis_direction'], bbox),
            'callout_lines': [label],
            'data': {'angle_deg': angle, 'depth_mm': depth, 'count': len(faces)},
            'other_instance_points': [f['centroid'] for f in faces[1:]],
        })

    return entries


# Plausible geometric diameters (mm) for a thread designation like "M6x1",
# "M5x0.8", or "G13.157x1.337" (a G1/4 BSP pipe thread): the major (nominal)
# diameter, for the rare case of a fully 3D-modeled thread whose
# cylindrical face sits at nominal size, AND the tap-drill diameter
# (major - pitch, the standard ~75%-engagement approximation - used as a
# reasonable proxy for BSP/pipe threads too, lacking a full tap-drill
# table) for the much more common case - a cosmetic thread applied over an
# ordinary drilled hole, whose actual cylindrical face is at drill size,
# not major size. For M6x1 those are 6.0mm and 5.0mm - almost 1mm apart,
# well outside any diameter-matching tolerance that's tight enough to be
# useful elsewhere. Comparing against ONLY the major diameter (the
# original implementation) meant an ordinary tapped hole's geometry never
# matched its own thread's designation at all, silently falling back to a
# bare, unlabeled diameter callout instead - "some holes have threads but
# show as holes only" in the drawing.
#
# Originally hardcoded to designations starting with "M" (metric) only -
# this silently rejected every BSP/pipe thread ("G1/4" exports from Fusion
# as "G13.157x1.337": G-prefixed, major x pitch, same shape as a metric
# designation) before ever comparing a diameter, regardless of tolerance.
# Confirmed as the reason a design's G1/4 ports never got their thread
# designation: any single-letter-prefixed "<letter><major>x<pitch>"
# designation is now accepted the same way, not just "M...".
# Standard ISO 228 (BSPP/"G") pipe thread sizes: fractional-inch callout ->
# (major diameter mm, pitch mm). Fusion's threadDesignation API returns pipe
# threads as a raw "<major>x<pitch>" geometric size (e.g. "G13.157x1.337")
# rather than the fractional-inch designation ("G1/4") a real drawing uses -
# metric designations ("M6x1") are already in standard display form and
# don't need this.
_BSP_PIPE_SIZES = [
    ('1/16', 7.723), ('1/8', 9.728), ('1/4', 13.157), ('3/8', 16.662),
    ('1/2', 20.955), ('5/8', 22.911), ('3/4', 26.441), ('7/8', 30.201),
    ('1', 33.249), ('1 1/8', 37.897), ('1 1/4', 41.910), ('1 3/8', 44.323),
    ('1 1/2', 47.803), ('1 3/4', 53.746), ('2', 59.614),
]


def _format_thread_designation(designation):
    """Convert a raw Fusion pipe-thread designation ("G13.157x1.337") into
    its standard fractional-inch callout ("G1/4\"") for display - the
    trailing `"` marks the size as inches, standard practice for a pipe
    thread's fractional callout (it's what distinguishes "1/4 inch" from a
    unitless fraction). Anything that isn't a "G<major>x<pitch>"-shaped BSP
    designation (metric "M6x1", or already a bare "Ø" diameter label)
    passes through unchanged - metric designations have no inch size to
    mark at all.
    """
    if not designation or not designation.upper().startswith('G'):
        return designation
    try:
        major = float(designation[1:].split('x')[0])
    except (ValueError, IndexError):
        return designation
    for frac, ref_major in _BSP_PIPE_SIZES:
        if abs(major - ref_major) < 0.05:
            return f'G{frac}"'
    return designation


def _expected_diameters_from_designation(designation):
    if not designation or not designation[0].isalpha():
        return []
    try:
        parts = designation[1:].split('x')
        major = float(parts[0])
    except (ValueError, IndexError):
        return []
    candidates = [major]
    if len(parts) > 1:
        try:
            pitch = float(parts[1])
            candidates.append(major - pitch)
        except ValueError:
            pass
    return candidates


def _tol_fn_for_body(step_path, project_data, tolerance_config):
    """Build this body's `f(value_mm) -> tolerance_mm` closure from its own
    material (`project_data['materials'][body_name]`, added by the export
    script) and the given `ToleranceConfig` - or None if no tolerance
    config was supplied at all (tolerance display is opt-in; a caller that
    doesn't pass one gets drawings exactly as before, no tolerance text).

    Resolves the config through `ToleranceConfig.for_body()` first, so a
    body with its own temperature override (e.g. a bracket mounted right on
    a motor, running hotter in service than the rest of the assembly) gets
    ITS OWN thermal contribution, not the assembly's single global one.
    """
    if tolerance_config is None:
        return None
    body_name = os.path.splitext(os.path.basename(step_path))[0]
    material_entry = project_data.get('materials', {}).get(body_name)
    return tolerance_mod.make_tolerance_fn(material_entry, tolerance_config.for_body(body_name))


def match_all_bodies(step_paths, project_data, tolerance=DEFAULT_DIAMETER_TOLERANCE_MM, tolerance_config=None):
    """Run matching across every body's STEP file against one shared pool.

    Two phases: (1) pure-geometry pass per body - cluster cylindrical faces
    into physical holes, group identical ones, no pool interaction yet
    (`_geometry_pass`); (2) ONE resolution pass across every body's groups
    at once, partitioned per designation/diameter so unrelated features
    never compete with each other (`_resolve_all_groups` - see its
    docstring for why this isn't simply per-body-in-file-order, and isn't a
    single fully-global ranking either).

    `tolerance_config` (a `tolerance.ToleranceConfig`), if given, makes
    every hole/thread depth+diameter and fillet/slot radius callout carry
    its own "±X.XX" tolerance, computed per-body from that body's own
    material (base percentage band + thermal expansion/contraction across
    the configured manufacturing/operating temperatures).

    Returns {step_path: match_result} plus the pool's final unclaimed
    holes/threads (features that belong to fasteners or other
    non-exported parts - expected, not an error).
    """
    pool = MatchPool.from_project_data(project_data)

    geo_by_path = {
        step_path: _geometry_pass(load_step(step_path), tol_fn=_tol_fn_for_body(step_path, project_data, tolerance_config))
        for step_path in step_paths
    }
    all_groups = [g for geo in geo_by_path.values() for g in geo['shape_groups']]
    labels_by_key = _resolve_all_groups(all_groups, pool, tolerance)

    results = {}
    for step_path, geo in geo_by_path.items():
        tol_fn = _tol_fn_for_body(step_path, project_data, tolerance_config)
        matched = []
        for group in geo['shape_groups']:
            labels_list = [labels_by_key[(id(group), i)] for i in range(len(group[0]['order']))]
            matched.extend(_finalize_hole_group(group, labels_list, tol_fn=tol_fn))
        matched.extend(geo['other_matched'])
        results[step_path] = {'matched': matched, 'unmatched_faces': geo['unmatched_faces']}

    unmatched_holes = [h for i, h in enumerate(pool.holes) if i not in pool.claimed_holes]
    unmatched_threads = [t for i, t in enumerate(pool.threads) if i not in pool.claimed_threads]

    return {
        'per_body': results,
        'unmatched_holes': unmatched_holes,
        'unmatched_threads': unmatched_threads,
    }


