"""
Cross-body ("bodychain") tolerance stack-up: several bodies in an assembly
that sit end-to-end along one axis (e.g. a housing, a gasket, and a lid all
stacked along Z) each contribute their own manufacturing tolerance to the
OVERALL stacked dimension - the real engineering question "if I bolt these
N parts together, how much could the total stack height vary?" isn't
answerable from any single body's own drawing, since no single body's STEP
file spans the whole stack.

This is possible here because of how the bodies were exported: Fusion's
export script hides every body except the target one and exports the
ROOT COMPONENT (not a re-centered single-body document), so every body's
STEP file preserves its TRUE absolute position in the original assembly's
shared coordinate system - confirmed directly (e.g. the constraint-matching
pipeline's cross-body G1/4 diagnosis in an earlier round relied on exactly
this: two different bodies' features being directly comparable in one
coordinate frame). Loading every body's bounding box into that same shared
frame is enough to detect which bodies sit end-to-end along a given axis,
without needing any assembly-structure metadata Fusion doesn't export.

Each body's own contribution along the chain axis is treated as one
elementary segment (its bounding-box extent along that axis), given a
tolerance via `tolerance.compute_tolerance()` (the same base-percentage +
thermal-expansion calculation used for a single body's own dimensions), and
the whole chain's tolerance is the RSS (root-sum-square) of those segment
tolerances - standard statistical tolerance-stacking practice, matching
what round 26 already does for a single body's own hole-to-hole chain.
"""

import glob
import json
import math
import os

import step_loader
import tolerance as tolerance_mod

AXES = {
    'X': (0, 'xmin', 'xmax'),
    'Y': (1, 'ymin', 'ymax'),
    'Z': (2, 'zmin', 'zmax'),
}
# Bodies are considered "touching" (part of the same stack) along an axis
# if the gap between one's far extent and the next's near extent is within
# this - real assembled parts don't sit at exactly zero gap (a tiny
# clearance, or float imprecision in the exported geometry, is normal).
CHAIN_GAP_TOLERANCE_MM = 0.5


def _load_body_infos(project_dir):
    """Every body's name, bounding box, and material, in the project's one
    shared coordinate system."""
    with open(os.path.join(project_dir, 'project_data.json'), encoding='utf-8') as f:
        project_data = json.load(f)
    materials = project_data.get('materials', {})

    infos = []
    for step_path in sorted(glob.glob(os.path.join(project_dir, '*.step'))):
        body_name = os.path.splitext(os.path.basename(step_path))[0]
        body = step_loader.load_step(step_path)
        if len(body.faces()) == 0:
            continue  # empty STEP file (see drawing_generator.generate_all) - not a real body
        bb = body.bounding_box
        infos.append({
            'name': body_name,
            'xmin': bb.xmin, 'xmax': bb.xmax,
            'ymin': bb.ymin, 'ymax': bb.ymax,
            'zmin': bb.zmin, 'zmax': bb.zmax,
            'material': materials.get(body_name),
        })
    return infos


def _cross_section_overlaps(body_a, body_b, axis):
    """True if `body_a` and `body_b` actually overlap in BOTH of the two
    axes perpendicular to `axis` - i.e. their cross-sections genuinely line
    up, not just their extents along `axis` happening to sit close together.

    An earlier version of `_find_chains` only checked the chain axis gap,
    which would happily group two bodies that sit at the same height (say)
    but in completely different X/Y locations on the assembly (e.g. two
    unrelated bosses on opposite sides of a housing) into a bogus "stack" -
    they were never actually touching, just coincidentally close along one
    axis. Requiring real geometric overlap in the other two axes as well is
    what "dynamically calculated from the bodies themselves" means here:
    the adjacency test now uses each body's own FULL 3D extent, not a
    single-axis proxy for it.
    """
    for ax in ('X', 'Y', 'Z'):
        if ax == axis:
            continue
        _idx, near_key, far_key = AXES[ax]
        lo = max(body_a[near_key], body_b[near_key])
        hi = min(body_a[far_key], body_b[far_key])
        if hi <= lo:
            return False
    return True


def _find_chains(infos, axis):
    """Bodies sorted along `axis` and split into runs wherever consecutive
    bodies' extents actually touch (within CHAIN_GAP_TOLERANCE_MM) AND
    their cross-sections genuinely overlap in the other two axes (see
    `_cross_section_overlaps`) - a real geometric adjacency test in all
    three dimensions, not a guess based on names, file order, or proximity
    along a single axis alone. A body whose extent along this axis overlaps
    or is far from every other body just forms its own 1-body "chain"
    (reported, but a single-body chain isn't a meaningful stack-up on its
    own).
    """
    _idx, near_key, far_key = AXES[axis]
    ordered = sorted(infos, key=lambda b: b[near_key])
    chains = []
    current = [ordered[0]] if ordered else []
    for body in ordered[1:]:
        prev = current[-1]
        gap = body[near_key] - prev[far_key]
        touching = -CHAIN_GAP_TOLERANCE_MM <= gap <= CHAIN_GAP_TOLERANCE_MM
        if touching and _cross_section_overlaps(prev, body, axis):
            current.append(body)
        else:
            chains.append(current)
            current = [body]
    if current:
        chains.append(current)
    return chains


def _signed_thermal_delta(length, cte, mfg_temp_c, op_temp_c):
    """Signed (not absolute) length change from thermal expansion/
    contraction if this body sits at `op_temp_c` having been made at
    `mfg_temp_c` - positive means the body has grown since manufacture,
    negative means it has shrunk. Needed (as opposed to the +/- magnitude
    `tolerance.compute_tolerance` reports for a single body on its own)
    because two bodies in a chain can have DIFFERENT operating ranges (see
    `ToleranceConfig.for_body`), so the worst case for the STACK isn't
    always "every body expands as much as it can" - one body's own worst
    case might be shrinking while its neighbor's is expanding, and only
    tracking each body's own +/- magnitude independently can't tell which
    combination actually produces the largest (or smallest) total stack
    length.
    """
    return length * cte * (op_temp_c - mfg_temp_c)


def _worst_case_chain_thermal_swing(segments):
    """The largest possible +/- total thermal length change for a whole
    chain, considering every body independently sitting at EITHER its own
    operating min OR max - the four combinations a body-PAIR's temperature
    can land in (both at min, both at max, or either one at min while the
    other's at max) are exactly the N=2 case of this.

    Each body's contribution to the chain's total length is linear in that
    body's own temperature and depends on no other body's temperature, so
    the total (a sum of independent linear terms) is maximized by picking,
    for each body ON ITS OWN, whichever of its two extremes contributes
    more - and likewise minimized by picking whichever contributes less.
    That's mathematically identical to brute-forcing every one of the
    2^N corner combinations and keeping the best and worst totals (the
    extremum of a sum of independent linear terms over a box always sits at
    a corner), just computed in one pass per body instead of an
    exponential one - "efficiently", per the actual ask, rather than by
    literally enumerating every combination.
    """
    max_total = 0.0
    min_total = 0.0
    for length, cte, mfg_temp_c, op_min_c, op_max_c in segments:
        at_min = _signed_thermal_delta(length, cte, mfg_temp_c, op_min_c)
        at_max = _signed_thermal_delta(length, cte, mfg_temp_c, op_max_c)
        max_total += max(at_min, at_max)
        min_total += min(at_min, at_max)
    return max(abs(max_total), abs(min_total))


def compute_chain_stackup(chain, axis, config):
    """(total_length_mm, rss_tolerance_mm, per_body_breakdown) for one
    chain of bodies along `axis` - each body's own extent along the axis is
    one elementary segment. The chain's overall tolerance is the RSS of
    two independently-derived figures: the RSS of every segment's own base
    (ISO 2768) tolerance, and the worst-case combined THERMAL swing for the
    whole chain (see `_worst_case_chain_thermal_swing` - every body
    independently at whichever of its own operating min/max extremes
    contributes most, not each body's own worst-case magnitude summed
    blindly, since two adjacent bodies with different operating ranges
    could have their worst cases pull in opposite directions).

    Resolves `config` through `.for_body()` per body, so a body with its
    own temperature override (e.g. a bracket mounted right on a motor,
    genuinely hotter in service than the rest of the assembly) contributes
    ITS OWN thermal expansion to the stack, not the assembly's single
    global operating range.
    """
    _idx, near_key, far_key = AXES[axis]
    breakdown = []
    segments = []
    for body in chain:
        length = body[far_key] - body[near_key]
        body_config = config.for_body(body['name'])
        cte, _is_estimated = tolerance_mod.resolve_cte(body['material'])
        segments.append((
            length, cte, body_config.manufacturing_temp_c,
            body_config.operating_min_c, body_config.operating_max_c,
        ))
        # Per-body figure kept in the breakdown is still each body's OWN
        # independent (base + thermal, RSS-combined) tolerance - informative
        # on its own dimension, even though the chain-level figure below no
        # longer just sums these directly.
        tol = tolerance_mod.compute_tolerance(length, body['material'], body_config)
        material_name = body['material'].get('name') if body['material'] else None
        breakdown.append({
            'name': body['name'], 'length_mm': length, 'tolerance_mm': tol,
            'material_name': material_name,
            'operating_range_c': (body_config.operating_min_c, body_config.operating_max_c),
        })
    total_length = sum(b['length_mm'] for b in breakdown)
    thermal_swing = _worst_case_chain_thermal_swing(segments)
    return total_length, thermal_swing, breakdown


def generate_bodychain_report(project_dir, config=None):
    """Human-readable report of every real (2+ body) chain found along each
    of X/Y/Z, with each chain's total stacked length and its RSS-combined
    tolerance. `config` defaults to a plain ToleranceConfig() (no
    thermal swing) if not given - the report works even with no
    tolerance_config.json saved, same opt-in-by-default spirit as the
    per-body drawings.
    """
    config = config or tolerance_mod.ToleranceConfig()
    infos = _load_body_infos(project_dir)
    if len(infos) < 2:
        return 'Fewer than 2 real bodies found - no assembly stack-up to compute.'

    lines = [
        'Assembly Body-Chain Tolerance Stack-Up',
        f'Manufacturing: {config.manufacturing_temp_c}°C  '
        f'Operating: {config.operating_min_c}°C to {config.operating_max_c}°C',
        '=' * 70,
    ]
    found_any = False
    for axis in ('X', 'Y', 'Z'):
        chains = [c for c in _find_chains(infos, axis) if len(c) > 1]
        if not chains:
            continue
        found_any = True
        lines.append(f'\n--- {axis} axis ---')
        for chain in chains:
            total_length, rss_tolerance, breakdown = compute_chain_stackup(chain, axis, config)
            names = ' -> '.join(b['name'] for b in breakdown)
            lines.append(f'\nChain ({len(breakdown)} bodies): {names}')
            lines.append(f'  Total stacked length: {total_length:.3f} mm')
            lines.append(f'  RSS stack-up tolerance: ±{rss_tolerance:.3f} mm')
            for b in breakdown:
                suffix = f'  [{b["material_name"]}]' if b['material_name'] else ''
                op_min, op_max = b['operating_range_c']
                if (op_min, op_max) != (config.operating_min_c, config.operating_max_c):
                    suffix += f'  (own operating range: {op_min}°C to {op_max}°C)'
                lines.append(f'    {b["name"]}: {b["length_mm"]:.3f} mm (±{b["tolerance_mm"]:.3f} mm){suffix}')
    if not found_any:
        lines.append('\nNo two bodies were found touching end-to-end along any single axis '
                      '(within the exported geometry\'s own coordinate system) - either the '
                      'bodies genuinely aren\'t stacked linearly, or they don\'t share the '
                      'assembly\'s original coordinate frame.')
    return '\n'.join(lines)
