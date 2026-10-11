"""
Dimensions derived from a view's own geometry.

Hole callouts only cover features the matcher recognised as holes, so a part
like a shaft-and-gear (no holes to speak of) came out with only its two
overall sizes. This reads the projected edges of a view and proposes:

* chain ordinates - the X (or Y) positions of the axis-aligned straight edges
  that make up the outline and its steps (shoulders, hubs, flanges, ribs...),
  so the step lengths get dimensioned;
* diameter callouts - the full circles seen face-on (bosses, bores, hubs,
  gear blanks), one per distinct centre/diameter.

Everything is measured from the edges themselves; the only inputs are the
tunables in `FeatureDimensionSettings`.
"""
import math
import json
import os
from dataclasses import asdict, dataclass, fields


@dataclass
class FeatureDimensionSettings:
    """Thresholds for the geometry-derived dimensions. All are neutral by
    default (no tolerance, no minimum, no cap) - nothing is tuned to any part;
    raise them to merge near-equal values or thin out a crowded view."""
    # coordinates closer than this (mm) are one ordinate
    coord_tolerance_mm: float = 0.0
    # an edge only counts as a step if it spans this fraction of the view extent
    min_edge_fraction: float = 0.0
    # ordinates closer than this fraction of the view extent are not both kept
    min_ordinate_gap_fraction: float = 0.0
    # at most this many geometry-derived ordinates per axis (heaviest first); 0 = no limit
    max_ordinates_per_axis: int = 0
    # smallest / most numerous diameter callouts per view
    min_diameter_mm: float = 0.0
    # (0 = no limit)
    max_diameter_callouts: int = 0
    # a circle within this (mm) of a hole's centre and radius is already called out
    hole_match_tolerance_mm: float = 0.0
    # a circle concentric with a hole and within this fraction of its radius is its chamfer/edge ring
    hole_radius_match_fraction: float = 0.0
    # circles within this of each other (mm, centre and radius) are one callout
    circle_merge_tolerance_mm: float = 0.0
    # arcs of one circle must reach this fraction of its angular sectors
    min_arc_fraction: float = 0.0


# What each setting does, in plain words - shown beside the field in the GUI.
SETTING_HELP = {
    'coord_tolerance_mm': "Edges whose X (or Y) differ by less than this many mm count as lined up and share one dimension. 0 = only exactly equal values merge.",
    'min_edge_fraction': "An outline edge only creates a dimension if it is at least this fraction (0-1) of the view size. Raise it to ignore small steps.",
    'min_ordinate_gap_fraction': "Two dimension positions closer than this fraction (0-1) of the view size are not both kept, so their labels do not pile up.",
    'max_ordinates_per_axis': "Most geometry-derived dimension positions kept per axis in one view (the longest edges win). 0 = no limit.",
    'min_diameter_mm': "Circles with a smaller diameter (mm) get no diameter callout.",
    'max_diameter_callouts': "Most diameter callouts per view (largest first). 0 = no limit.",
    'hole_match_tolerance_mm': "A circle this close (mm) to a hole's centre and radius is that hole, already called out, so it is skipped.",
    'hole_radius_match_fraction': "A circle concentric with a hole and within this fraction (0-1) of its radius is the hole's chamfer/edge ring and is skipped.",
    'circle_merge_tolerance_mm': "Circles whose centre and radius differ by less than this (mm) are one callout.",
    'min_arc_fraction': "Hidden-line removal splits circles into arcs; a circle only gets a diameter if its arcs reach this fraction (0-1) of its 12 angular sectors. Rounded slot ends reach about 0.5, full circles 1.",
}

SETTINGS_FILE = 'drawing_settings.json'


def _settings_path(project_dir, for_write=False):
    import tolerance
    return tolerance.config_file(project_dir, SETTINGS_FILE, for_write)


def load_settings(project_dir):
    """Saved settings for a project, falling back per-field to the defaults."""
    settings = FeatureDimensionSettings()
    if not project_dir:
        return settings
    try:
        with open(_settings_path(project_dir), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return settings
    for fld in fields(settings):
        if fld.name in data:
            try:
                value = type(getattr(settings, fld.name))(data[fld.name])
            except (TypeError, ValueError):
                continue
            if fld.name in ('max_ordinates_per_axis', 'max_diameter_callouts') and value >= 10**9:
                value = 0   # older files stored "no limit" as a huge number
            setattr(settings, fld.name, value)
    return settings


def save_settings(project_dir, settings):
    path = _settings_path(project_dir, for_write=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(asdict(settings), f, indent=2)
    return path


def _axis_aligned_lines(edges, settings):
    """[(axis, coordinate, length, endpoint_a, endpoint_b)] for every edge that
    runs parallel to the view's X (axis=1: constant y) or Y (axis=0: constant
    x) - straight edges, and curves that project edge-on to a straight run
    (a circle seen from the side)."""
    tol = settings.coord_tolerance_mm
    out = []
    for e in edges:
        if len(e) < 2:
            continue
        xs = [p[0] for p in e]
        ys = [p[1] for p in e]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if h <= tol and w > tol:
            lo = min(e, key=lambda p: p[0])
            hi = max(e, key=lambda p: p[0])
            out.append((1, sum(ys) / len(ys), w, lo, hi))
        elif w <= tol and h > tol:
            lo = min(e, key=lambda p: p[1])
            hi = max(e, key=lambda p: p[1])
            out.append((0, sum(xs) / len(xs), h, lo, hi))
    return out


def _select_ordinates(lines, axis, extent, settings):
    """Pick the coordinates worth dimensioning along `axis`: grouped, weighted
    by total edge length, thinned so neighbours are not crowded together."""
    items = sorted((l for l in lines if l[0] == axis), key=lambda l: l[1])
    groups = []
    for l in items:
        if groups and abs(l[1] - groups[-1]['coord']) <= settings.coord_tolerance_mm:
            g = groups[-1]
            g['weight'] += l[2]
            g['lines'].append(l)
        else:
            groups.append({'coord': l[1], 'weight': l[2], 'lines': [l]})
    if not groups:
        return []
    min_span = settings.min_edge_fraction * extent
    groups = [g for g in groups if g['weight'] >= min_span]
    # keep the heaviest, honouring a minimum separation, then restore order
    min_gap = settings.min_ordinate_gap_fraction * extent
    kept = []
    for g in sorted(groups, key=lambda g: -g['weight']):
        if settings.max_ordinates_per_axis and len(kept) >= settings.max_ordinates_per_axis:
            break
        if all(abs(g['coord'] - k['coord']) >= min_gap for k in kept):
            kept.append(g)
    return sorted(kept, key=lambda g: g['coord'])


def chain_points(visible_edges, extent_x, extent_y, settings=None):
    """(points_for_x_chain, points_for_y_chain): real (x, y) points on the
    outline, one per edge in each selected ordinate group."""
    settings = settings or FeatureDimensionSettings()
    lines = _axis_aligned_lines(visible_edges, settings)
    xs, ys = [], []
    for g in _select_ordinates(lines, 0, extent_x, settings):
        for _ax, _c, _len, a, b in g['lines']:
            q = min((a, b), key=lambda p: p[1])            # end nearest the bottom row
            xs.append((g['coord'], q[1]))
    for g in _select_ordinates(lines, 1, extent_y, settings):
        for _ax, _c, _len, a, b in g['lines']:
            q = max((a, b), key=lambda p: p[0])            # end nearest the right column
            ys.append((q[0], g['coord']))
    return [tuple(p[:2]) for p in xs], [tuple(p[:2]) for p in ys]


_SECTORS = 12   # angular bins used to tell a round feature from a rounded end


def diameter_callouts(visible_edges, hole_circles=(), settings=None):
    """[(x, y, radius_mm, diameter_mm)] for distinct circles in the view.

    HLR often returns one circle as several arcs, so arcs are grouped by
    centre and radius and a group counts once its arcs reach at least
    `min_arc_fraction` of the circle's angular sectors (a half-round end of
    a slot or obround does not). `hole_circles` is
    [(x, y, radius_mm)] for circles the hole callouts already describe.
    Larger circles come first, capped at `max_diameter_callouts`.
    """
    settings = settings or FeatureDimensionSettings()
    tol = settings.circle_merge_tolerance_mm
    hole_tol = settings.hole_match_tolerance_mm
    groups = []   # [cx, cy, r, covered_length]
    for e in visible_edges:
        circ = getattr(e, 'circle', None)
        if not circ:
            continue
        cx, cy, r, _full = circ
        if 2 * r < settings.min_diameter_mm:
            continue
        sectors = {int(((math.atan2(p[1] - cy, p[0] - cx) % (2 * math.pi)) / (2 * math.pi)) * _SECTORS) % _SECTORS
                   for p in e}
        for g in groups:
            if math.hypot(cx - g[0], cy - g[1]) <= tol and abs(r - g[2]) <= tol:
                g[3] |= sectors
                break
        else:
            groups.append([cx, cy, r, sectors])
    out = []
    for cx, cy, r, sectors in groups:
        if len(sectors) < settings.min_arc_fraction * _SECTORS:
            continue
        if any(math.hypot(cx - hx, cy - hy) <= hole_tol
               and abs(r - hr) <= max(hole_tol,
                                      settings.hole_radius_match_fraction * hr)
               for hx, hy, hr in hole_circles):
            continue
        out.append((cx, cy, r, 2 * r))
    out.sort(key=lambda t: -t[2])
    return out[:settings.max_diameter_callouts] if settings.max_diameter_callouts else out
