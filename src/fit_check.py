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
import multiprocessing
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


class _Box:
    """A body's bounding box as plain numbers (so it can come back from a worker process)."""
    def __init__(self, values):
        self.xmin, self.xmax, self.ymin, self.ymax, self.zmin, self.zmax = values


# ---- worker processes: each loads a STEP file at most once and keeps the shape ----
_WORKER_SHAPES = {}


def _worker_init():
    """Make a worker quit by itself when the process that started it has gone (a cancelled job is
    killed outright, which would otherwise leave its workers running)."""
    import threading
    import time

    def watch():
        parent = multiprocessing.parent_process()
        while parent is not None and parent.is_alive():
            time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def _worker_shape(path):
    if path not in _WORKER_SHAPES:
        _WORKER_SHAPES[path] = step_loader.load_step(path)
    return _WORKER_SHAPES[path]


def _worker_info(path):
    """(face count, bounding box values) of one STEP file."""
    body = _worker_shape(path)
    if len(body.faces()) == 0:
        return 0, None
    bb = body.bounding_box
    return len(body.faces()), (bb.xmin, bb.xmax, bb.ymin, bb.ymax, bb.zmin, bb.zmax)


def _worker_pair(args):
    """(distance, point on A, point on B) between two STEP files, or None when it is over the limit."""
    path_a, path_b, max_gap_mm = args
    ext = BRepExtrema_DistShapeShape(_worker_shape(path_a).shape.wrapped, _worker_shape(path_b).shape.wrapped)
    if not ext.IsDone() or ext.NbSolution() == 0:
        return None
    distance = ext.Value()
    if distance > max_gap_mm:
        return None
    p1, p2 = ext.PointOnShape1(1), ext.PointOnShape2(1)
    return distance, (p1.X(), p1.Y(), p1.Z()), (p2.X(), p2.Y(), p2.Z())


def _make_pool(workers):
    """A pool of worker processes. The caller may itself be a background job process (those are
    daemons, which may not have children), so that flag is lifted while the pool starts."""
    ctx = multiprocessing.get_context('spawn')
    me = multiprocessing.current_process()
    config = getattr(me, '_config', None)
    previous = config.get('daemon') if config is not None else None
    if config is not None and previous:
        config['daemon'] = False
    try:
        return ctx.Pool(workers, initializer=_worker_init)
    finally:
        if config is not None and previous:
            config['daemon'] = previous


# ---- results are kept on disk, keyed on the STEP files, so a repeat run needs no measuring ----
def _cache_key(step_paths, max_gap_mm, only_involving):
    return {
        'version': 2, 'max_gap_mm': max_gap_mm,
        'only': sorted(only_involving) if only_involving else None,
        'files': [[os.path.basename(f), os.path.getsize(f), int(os.path.getmtime(f))] for f in step_paths],
    }


def _read_cache(project_dir, key):
    try:
        with open(tolerance_mod.config_file(project_dir, 'gap_cache.json'), encoding='utf-8') as f:
            data = json.load(f)
        if data.get('key') != key:
            return None
        return [{**g, 'point_a': tuple(g['point_a']), 'point_b': tuple(g['point_b']),
                 'center_a': tuple(g['center_a']), 'center_b': tuple(g['center_b'])} for g in data['gaps']]
    except Exception:
        return None


def _write_cache(project_dir, key, gaps):
    try:
        with open(tolerance_mod.config_file(project_dir, 'gap_cache.json', for_write=True), 'w',
                  encoding='utf-8') as f:
            json.dump({'key': key, 'gaps': gaps}, f)
    except Exception:
        pass


def find_nearest_gaps(project_dir, max_gap_mm=DEFAULT_MAX_GAP_MM, progress=None, only_involving=None):
    """Every pair of DIFFERENT bodies whose true nearest-point distance is
    <= `max_gap_mm`, computed directly from their real geometry - not from
    any assumed feature-matching relationship. Each entry: {body_a, body_b,
    distance_mm, point_a, point_b, center_a, center_b}, where point_a/b are
    the actual closest points (one on each body, in the shared assembly
    coordinate system every exported STEP file preserves - see
    bodychain.py's own docstring) and center_a/b are each body's bounding-
    box center, used as a stand-in "expands from here" anchor.

    `only_involving`, if given, is a set of body names: only pairs that include at least one of
    them are measured (the others can't change anything the caller uses). The exact solves run in
    parallel worker processes, and the result is cached on disk until a STEP file changes.
    `progress(fraction, message)` is called as the work advances.
    """
    def report(fraction, message):
        if progress is not None:
            progress(fraction, message)

    step_paths = sorted(glob.glob(os.path.join(project_dir, '*.step')))
    key = _cache_key(step_paths, max_gap_mm, only_involving)
    cached = _read_cache(project_dir, key)
    if cached is not None:
        return cached

    names = [os.path.splitext(os.path.basename(f))[0] for f in step_paths]
    workers = max(1, min((os.cpu_count() or 2) - 2, 12))
    pool = None
    if workers > 1 and len(step_paths) > 3:
        try:
            pool = _make_pool(workers)
        except Exception:
            pool = None
    try:
        report(0.0, 'Reading the parts...')
        infos = pool.map(_worker_info, step_paths) if pool else [_worker_info(f) for f in step_paths]
        bodies = []
        for name, path, (faces, box) in zip(names, step_paths, infos):
            if faces == 0:
                continue  # empty STEP file (referenced/unresolved component) - not a real body
            bb = _Box(box)
            center = ((bb.xmin + bb.xmax) / 2, (bb.ymin + bb.ymax) / 2, (bb.zmin + bb.zmax) / 2)
            bodies.append((name, path, bb, center))

        pairs = []
        for first, second in itertools.combinations(bodies, 2):
            if only_involving and first[0] not in only_involving and second[0] not in only_involving:
                continue
            if _bounding_boxes_within(first[2], second[2], max_gap_mm):
                pairs.append((first, second))

        jobs = [(a[1], b[1], max_gap_mm) for a, b in pairs]
        results = []
        if pool:
            for index, result in enumerate(pool.imap(_worker_pair, jobs, chunksize=1), start=1):
                results.append(result)
                report(0.1 + 0.9 * index / max(len(jobs), 1), f'Measuring gaps between parts ({index}/{len(jobs)})')
        else:
            for index, job in enumerate(jobs, start=1):
                results.append(_worker_pair(job))
                report(0.1 + 0.9 * index / max(len(jobs), 1), f'Measuring gaps between parts ({index}/{len(jobs)})')
    finally:
        if pool:
            pool.terminate()

    gaps = []
    for ((name_a, _pa, _ba, center_a), (name_b, _pb, _bb, center_b)), result in zip(pairs, results):
        if result is None:
            continue
        distance, point_a, point_b = result
        gaps.append({'body_a': name_a, 'body_b': name_b, 'distance_mm': distance,
                     'point_a': point_a, 'point_b': point_b, 'center_a': center_a, 'center_b': center_b})
    _write_cache(project_dir, key, gaps)
    return gaps


def nearest_gap_per_body(project_dir, max_gap_mm=DEFAULT_MAX_GAP_MM, progress=None, only_involving=None):
    """{body_name: smallest nearest-point distance to any other body, or
    None if none is within max_gap_mm} - feeds the tolerance fit band."""
    out = {}
    for g in find_nearest_gaps(project_dir, max_gap_mm, progress, only_involving):
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
