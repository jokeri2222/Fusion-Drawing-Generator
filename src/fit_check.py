"""
Cross-body clearance check via raw nearest-point geometry: for every pair
of bodies close enough to matter, find their actual nearest points -
OpenCascade's own minimum-distance-between-shapes solver
(BRepExtrema_DistShapeShape), not any assumed hole/shaft/matching-feature
relationship - and work out the worst-case gap once each body's own
thermal growth (at ITS OWN operating temperature, which can differ body to
body - see tolerance.ToleranceConfig.for_body) is accounted for.

This is deliberately a different calculation from bodychain.py's linear
stack-up. A stack-up ADDS bodies' lengths along one shared axis, so the
worst case is always "every body pushed toward the same extreme
simultaneously" (see bodychain._worst_case_chain_thermal_swing) - only 2 of
the 2^N corners of the operating-temperature box can ever be the extremum
of a sum of independent linear terms. A gap between two ARBITRARY nearby
bodies is closer to a difference (how much each body's near surface moves
toward or away from the other), so the worst case is generally a MIXED
corner - one body's own coldest/smallest state while the other's own
hottest/largest - e.g. a housing sitting at room temperature next to a
gear that runs hot from friction. All four corners genuinely matter here
and are checked explicitly.
"""

import glob
import itertools
import json
import math
import os

from OCP.BRepExtrema import BRepExtrema_DistShapeShape

import step_loader
import tolerance as tolerance_mod

# Only pairs whose nearest points are within this of each other are worth
# a thermal-fit check at all - bodies genuinely far apart can't interfere
# regardless of thermal growth, and including every pair in a big assembly
# would make the report noise instead of signal.
DEFAULT_MAX_GAP_MM = 5.0


def _bounding_boxes_within(bb_a, bb_b, max_gap_mm):
    """Cheap reject before the expensive exact nearest-point solve: true
    only if the two bodies' bounding boxes (each conceptually expanded by
    `max_gap_mm`) could possibly be this close. Lets most far-apart pairs
    in a larger assembly skip BRepExtrema_DistShapeShape entirely, since
    it's the actual bottleneck (a full solve per pair, tens of ms each).
    """
    return (
        bb_a.xmin - max_gap_mm <= bb_b.xmax and bb_b.xmin - max_gap_mm <= bb_a.xmax and
        bb_a.ymin - max_gap_mm <= bb_b.ymax and bb_b.ymin - max_gap_mm <= bb_a.ymax and
        bb_a.zmin - max_gap_mm <= bb_b.zmax and bb_b.zmin - max_gap_mm <= bb_a.zmax
    )


def find_nearest_gaps(project_dir, max_gap_mm=DEFAULT_MAX_GAP_MM):
    """Every pair of DIFFERENT bodies whose true nearest-point distance is
    <= `max_gap_mm`, computed directly from their real geometry - not from
    any assumed feature-matching relationship. Each entry: {body_a, body_b,
    distance_mm, point_a, point_b, center_a, center_b}, where point_a/b are
    the actual closest points (one on each body, in the shared assembly
    coordinate system every exported STEP file preserves - see
    bodychain.py's own docstring) and center_a/b are each body's bounding-
    box center, used as a stand-in "expands from here" anchor.
    """
    bodies = []
    for step_path in sorted(glob.glob(os.path.join(project_dir, '*.step'))):
        body_name = os.path.splitext(os.path.basename(step_path))[0]
        body = step_loader.load_step(step_path)
        if len(body.faces()) == 0:
            continue  # empty STEP file (referenced/unresolved component) - not a real body
        bb = body.bounding_box
        center = ((bb.xmin + bb.xmax) / 2, (bb.ymin + bb.ymax) / 2, (bb.zmin + bb.zmax) / 2)
        bodies.append((body_name, body, bb, center))

    gaps = []
    for (name_a, body_a, bb_a, center_a), (name_b, body_b, bb_b, center_b) in itertools.combinations(bodies, 2):
        if not _bounding_boxes_within(bb_a, bb_b, max_gap_mm):
            continue
        ext = BRepExtrema_DistShapeShape(body_a.shape.wrapped, body_b.shape.wrapped)
        if not ext.IsDone() or ext.NbSolution() == 0:
            continue
        distance = ext.Value()
        if distance > max_gap_mm:
            continue
        p1 = ext.PointOnShape1(1)
        p2 = ext.PointOnShape2(1)
        gaps.append({
            'body_a': name_a, 'body_b': name_b, 'distance_mm': distance,
            'point_a': (p1.X(), p1.Y(), p1.Z()), 'point_b': (p2.X(), p2.Y(), p2.Z()),
            'center_a': center_a, 'center_b': center_b,
        })
    return gaps


def nearest_gap_per_body(project_dir, max_gap_mm=DEFAULT_MAX_GAP_MM):
    """{body_name: smallest nearest-point distance to any other body, or
    None if none is within max_gap_mm} - feeds the tolerance fit band."""
    out = {}
    for g in find_nearest_gaps(project_dir, max_gap_mm):
        for n in (g['body_a'], g['body_b']):
            if out.get(n) is None or g['distance_mm'] < out[n]:
                out[n] = g['distance_mm']
    return out


def compute_gap_fit(gap, material_a, config_a, material_b, config_b):
    """Worst-case tightest/loosest gap between two bodies' nearest points,
    once each body's own thermal growth (toward or away from the other, at
    its own operating temperature) is accounted for.

    Models each body as expanding uniformly outward from its own bounding-
    box center: the near point then moves by cte * delta_T * (near_point -
    center), and only the component of that movement ALONG the gap's own
    axis (point_a -> point_b) changes the distance to first order (the
    other two components are lateral). The distance from a body's own
    center to its near point, projected onto that axis, doubles as the
    "manufactured length" its own ISO 2768 base tolerance is looked up
    against - the same way bodychain.py treats each body's own bounding-box
    extent as its stack segment.
    """
    distance0 = gap['distance_mm']
    p_a, p_b = gap['point_a'], gap['point_b']
    axis = tuple(b - a for a, b in zip(p_a, p_b))
    axis_len = math.sqrt(sum(c * c for c in axis)) or 1.0
    axis = tuple(c / axis_len for c in axis)  # unit vector, A -> B

    def project(vec):
        return sum(v * a for v, a in zip(vec, axis))

    # How far each body's own near point sits from ITS OWN center, along
    # the axis pointing toward the other body - positive means the point
    # already sticks out toward the other body.
    length_a = project(tuple(p - c for p, c in zip(p_a, gap['center_a'])))
    length_b = -project(tuple(p - c for p, c in zip(p_b, gap['center_b'])))

    cte_a, _ = tolerance_mod.resolve_cte(material_a)
    cte_b, _ = tolerance_mod.resolve_cte(material_b)

    def growth_toward_other(length, cte, mfg_temp_c, op_temp_c):
        # Positive means this body's near point has moved further toward
        # the other body (shrinking the gap); negative means it moved away.
        return length * cte * (op_temp_c - mfg_temp_c)

    # Both bodies independently at either of their own two operating
    # extremes - a difference/gap's worst case can land at any of the four
    # corners (unlike a stack-up's sum, where only 2 of 2^N corners ever
    # matter - see the module docstring), so all four are checked directly.
    combos = itertools.product(
        (config_a.operating_min_c, config_a.operating_max_c),
        (config_b.operating_min_c, config_b.operating_max_c),
    )
    gap_swings = [
        -growth_toward_other(length_a, cte_a, config_a.manufacturing_temp_c, t_a)
        - growth_toward_other(length_b, cte_b, config_b.manufacturing_temp_c, t_b)
        for t_a, t_b in combos
    ]
    max_swing, min_swing = max(gap_swings), min(gap_swings)

    # Same asymmetric-clamping idea as tolerance.compute_tolerance_asymmetric:
    # if this gap can only ever grow (or only ever shrink) across every
    # corner, the OTHER direction gets no thermal contribution at all.
    thermal_plus = max(max_swing, 0.0)
    thermal_minus = max(-min_swing, 0.0)

    return {
        'nominal_distance_mm': distance0,
        'loosest_distance_mm': distance0 + thermal_plus,
        'tightest_distance_mm': distance0 - thermal_minus,
        'length_a_mm': length_a, 'length_b_mm': length_b,
        'thermal_growth_mm': thermal_plus, 'thermal_shrink_mm': thermal_minus,
    }


def generate_fit_report(project_dir, config=None, max_gap_mm=DEFAULT_MAX_GAP_MM):
    """Human-readable report of every body pair whose real geometry comes
    within `max_gap_mm` of each other, their nominal gap, and the worst-
    case loosest/tightest gap once each body's own operating temperature
    (see ToleranceConfig.for_body) is accounted for. `config` defaults to
    a plain ToleranceConfig() (no thermal swing) - same opt-in-by-default
    spirit as the bodychain report.
    """
    config = config or tolerance_mod.ToleranceConfig()
    with open(os.path.join(project_dir, 'project_data.json'), encoding='utf-8') as f:
        materials = json.load(f).get('materials', {})

    gaps = find_nearest_gaps(project_dir, max_gap_mm)
    if not gaps:
        return (f'No body pairs found within {max_gap_mm:.1f} mm of each other - '
                f'nothing close enough for a thermal clearance check to matter.')

    gaps.sort(key=lambda g: g['distance_mm'])
    lines = [
        'Cross-Body Nearest-Point Clearance Check',
        f'Manufacturing: {config.manufacturing_temp_c}°C  '
        f'Operating: {config.operating_min_c}°C to {config.operating_max_c}°C  '
        f'|  Max gap considered: {max_gap_mm:.1f} mm',
        '=' * 70,
    ]
    for gap in gaps:
        config_a = config.for_body(gap['body_a'])
        config_b = config.for_body(gap['body_b'])
        result = compute_gap_fit(
            gap, materials.get(gap['body_a']), config_a,
            materials.get(gap['body_b']), config_b,
        )
        lines.append(f"\n{gap['body_a']} <-> {gap['body_b']}")
        lines.append(f"  Nominal nearest-point gap: {result['nominal_distance_mm']:.3f} mm")
        lines.append(f"  Worst case across operating range: "
                      f"{result['tightest_distance_mm']:.3f} mm to {result['loosest_distance_mm']:.3f} mm")
        if result['tightest_distance_mm'] < 0:
            lines.append('  WARNING: worst case is INTERFERENCE (negative gap) - these bodies '
                          'may physically collide across the full operating range as currently '
                          'toleranced.')
        for label, name, body_config in (('', gap['body_a'], config_a), ('', gap['body_b'], config_b)):
            if (body_config.operating_min_c, body_config.operating_max_c) != \
                    (config.operating_min_c, config.operating_max_c):
                lines.append(f"    {name} own operating range: "
                              f"{body_config.operating_min_c}°C to {body_config.operating_max_c}°C")
    return '\n'.join(lines)
