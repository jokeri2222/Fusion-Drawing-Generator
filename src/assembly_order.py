"""
Assembly ORDER and SUB-ASSEMBLIES: a geometry-based baseline, a saved
manual tree on top of it (sub-assemblies nested to any depth), and checks
that flag problems with an order (floating parts, blocked screw access).

Baseline (`OrderAnalyzer.suggest_order`), greedy, one part at a time:
- Start from the part resting on the ground (lowest point of the whole
  assembly); the largest footprint wins ties.
- Next part must TOUCH something already placed (real solid distance
  query), so the assembly grows as one connected piece.
- Among touching candidates prefer ones that are securely held when they
  go in: on the ground, resting on a placed part below them, or joined to
  a placed part by a fastener - then merely touching ones. Lowest first.
- A candidate is deferred while it would sit in the tool-access corridor
  of a fastener that still has to be installed (its head side must stay
  open to be tightened), unless nothing else is left.

Proposed tree: parts bolted together by small fastener joints (<= 3 bodies)
become one sub-assembly; everything else is a loose part. Order = baseline.

Saved tree: `configs/assembly_tree.json` = {"tree": [node, ...]} where a
node is a part name or {"name": ..., "children": [node, ...]} (plus
"type": "set" for a non-assembly group: no build pages, its parts are
placed together by name); groups nest to any depth and the tree's top-to-bottom order IS the assembly order.
Parts missing from it are appended in baseline order; unknown names and
empty groups are dropped. Fastener steps are never ordered by hand: each
is installed right after the last body it joins, inside the smallest
sub-assembly that contains all of those bodies.

`OrderAnalyzer.check_order` reports, for any order: parts that float
(nothing placed supports or fastens them when they go in) and fasteners
whose head side is blocked by an earlier-placed body.
"""

import json
import os

import assembly_guide as ag

_TOUCH_TOL_MM = 0.5
_GROUND_TOL_MM = 0.5
_CORRIDOR_LEN_MM = 2000.0
_CORRIDOR_SHRINK_MM = 0.3


def order_file(project_dir, for_write=False):
    import tolerance
    return tolerance.config_file(project_dir, 'assembly_order.json', for_write)


def tree_file(project_dir, for_write=False):
    import tolerance
    return tolerance.config_file(project_dir, 'assembly_tree.json', for_write)


def load_manual_order(project_dir):
    """Legacy flat order (older versions); only used to rank a proposal."""
    try:
        with open(order_file(project_dir), encoding='utf-8') as f:
            order = json.load(f).get('order')
        return [str(n) for n in order] if isinstance(order, list) else None
    except Exception:
        return None


def load_tree(project_dir):
    """Saved tree (list of nodes) or None if never saved."""
    try:
        with open(tree_file(project_dir), encoding='utf-8') as f:
            tree = json.load(f).get('tree')
        return tree if isinstance(tree, list) else None
    except Exception:
        return None


def load_fasteners_edited(project_dir):
    """True if the saved tree carries hand-placed fastener steps (then the
    automatic fastener placement is off: only the steps in the tree exist)."""
    try:
        with open(tree_file(project_dir), encoding='utf-8') as f:
            return bool(json.load(f).get('fasteners_edited'))
    except Exception:
        return False


def save_tree(project_dir, tree, fasteners_edited=False):
    with open(tree_file(project_dir, for_write=True), 'w', encoding='utf-8') as f:
        json.dump({'tree': tree, 'fasteners_edited': bool(fasteners_edited)}, f, indent=2)


def clear_manual_order(project_dir):
    for path in (tree_file(project_dir), order_file(project_dir)):
        try:
            os.remove(path)
        except OSError:
            pass


def is_fasten(node):
    """A fastener step block: {"type": "fasten", "name", "fasteners": [...],
    "bodies": [...], "axis"}. Has no children and no parts of its own."""
    return isinstance(node, dict) and node.get('type') == 'fasten'


def is_set(node):
    """A group of the 'set' kind is NOT an assembly: no build pages of its
    own, its parts are just placed (named by the set) into the enclosing
    assembly. Default kind is 'assembly'."""
    return isinstance(node, dict) and node.get('type') == 'set'


def flatten(tree):
    """Part names in assembly order (depth-first)."""
    out = []
    for n in tree:
        if isinstance(n, str):
            out.append(n)
        else:
            out.extend(flatten(n.get('children', [])))
    return out


def normalize_tree(tree, parts, fasteners=None):
    """Drop unknown/duplicate parts and empty groups. Returns (tree, missing)."""
    pset, seen = set(parts), set()
    fset = set(fasteners) if fasteners is not None else None
    fseen = set()

    def clean(nodes):
        out = []
        for n in nodes:
            if isinstance(n, str):
                if n in pset and n not in seen:
                    seen.add(n)
                    out.append(n)
            elif is_fasten(n):
                fs = [f for f in n.get('fasteners', []) if (fset is None or f in fset) and f not in fseen]
                bs = [b for b in n.get('bodies', []) if b in pset]
                if fs and bs:
                    fseen.update(fs)
                    out.append({'type': 'fasten', 'name': n.get('name', 'Fasteners'), 'fasteners': fs,
                                'bodies': bs, 'axis': n.get('axis')})
            elif isinstance(n, dict):
                kids = clean(n.get('children', []))
                if kids:
                    node = {'name': str(n.get('name', 'Sub-assembly')), 'children': kids}
                    if n.get('type') == 'set':
                        node['type'] = 'set'
                    out.append(node)
        return out

    out = clean(tree)
    return out, [p for p in parts if p not in seen]


def _boxes_overlap(a, b, margin):
    return (a.xmin - margin <= b.xmax and b.xmin - margin <= a.xmax and
            a.ymin - margin <= b.ymax and b.ymin - margin <= a.ymax and
            a.zmin - margin <= b.zmax and b.zmin - margin <= a.zmax)


class OrderAnalyzer:
    def __init__(self, project_dir, body_list=None):
        self.project_dir = project_dir
        if body_list is None:
            body_list = ag._load_bodies(project_dir)
        self.bodies = dict(body_list)
        body_types = ag.load_body_types(project_dir)
        self.fasteners = [n for n, _b in body_list if ag.is_fastener(n, body_types)]
        fset = set(self.fasteners)
        self.parts = [n for n, _b in body_list if n not in fset]
        self.bbox = {n: b.bounding_box for n, b in body_list}
        self._cache = self._load_cache()
        if 'joints' in self._cache:
            self.joints = self._cache['joints']
        else:
            self.joints = ag.find_fastener_joints(project_dir, bodies=body_list)
            self._cache['joints'] = self.joints
            self._save_cache()
        self._touch = {tuple(k.split('|', 1)): v for k, v in self._cache.get('touch', {}).items()}
        self._access = {}
        self.groups = self._group_joints(None)
        self.fastener_manual = load_fasteners_edited(project_dir) and load_tree(project_dir) is not None
        self.tree = self._build_tree()
        self.groups = self._group_joints(self._body_paths())

    # ---- disk cache (exact solid-distance queries take minutes) -----------
    def _signature(self):
        import glob
        sig = {}
        for path in sorted(glob.glob(os.path.join(self.project_dir, '*.step'))):
            st = os.stat(path)
            sig[os.path.basename(path)] = [st.st_size, int(st.st_mtime)]
        return sig

    def _cache_path(self):
        import tolerance
        return tolerance.config_file(self.project_dir, '.assembly_cache.json', for_write=True)

    def _load_cache(self):
        try:
            with open(self._cache_path(), encoding='utf-8') as f:
                data = json.load(f)
            if data.get('signature') == self._signature():
                return data
        except Exception:
            pass
        return {'signature': self._signature()}

    def _save_cache(self):
        try:
            self._cache['touch'] = {'|'.join(k): v for k, v in self._touch.items()} if hasattr(self, '_touch') else {}
            with open(self._cache_path(), 'w', encoding='utf-8') as f:
                json.dump(self._cache, f)
        except Exception:
            pass

    # ---- fastener groups / access ------------------------------------
    def _group_joints(self, unit_of=None):
        """Fasteners of the same type, axis and head level that join bodies
        of the same sub-assemblies are ONE installation step, even when each
        screw touches a slightly different set of bodies (bodies = union)."""
        paths = unit_of or {}
        grouped = {}
        for j in self.joints:
            f = j['fastener']
            level = round(self.fastener_level(f, j['axis']) / 2.0)
            units = frozenset(paths[b] for b in j['bodies'] if paths.get(b))
            key = (type_key(f), j['axis'], level, units)
            g = grouped.setdefault(key, {'fasteners': [], 'bodies': [], 'axis': j['axis']})
            g['fasteners'].append(f)
            for b in j['bodies']:
                if b not in g['bodies']:
                    g['bodies'].append(b)
        groups = list(grouped.values())
        for g in groups:
            g['path'] = _common_prefix([paths.get(b, ()) for b in g['bodies']]) if paths else ()
        return groups

    def fastener_level(self, fastener, axis):
        idx = 'XYZ'.index(axis)
        bb = self.bbox[fastener]
        sign = self._head_direction(fastener, axis)
        if sign > 0:
            return hi_of(bb, idx)
        if sign < 0:
            return lo_of(bb, idx)
        return (lo_of(bb, idx) + hi_of(bb, idx)) / 2

    def _proposed_tree(self):
        """Parts bolted together by small joints form one sub-assembly;
        order = baseline (or the legacy flat order if one was saved)."""
        pset = set(self.parts)
        order = load_manual_order(self.project_dir) or self.suggest_order()
        order = [n for n in order if n in pset] + [n for n in self.suggest_order() if n not in order]
        rank = {n: i for i, n in enumerate(order)}
        parent = {n: n for n in self.parts}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for g in self.groups:
            bs = [b for b in g['bodies'] if b in pset]
            if 2 <= len(bs) <= 3:
                for other in bs[1:]:
                    parent[find(other)] = find(bs[0])
        clusters = {}
        for n in self.parts:
            clusters.setdefault(find(n), []).append(n)
        nodes = []
        for members in clusters.values():
            members.sort(key=lambda n: rank[n])
            if len(members) > 1:
                big = max(members, key=lambda n: (self.bbox[n].xmax - self.bbox[n].xmin) *
                          (self.bbox[n].ymax - self.bbox[n].ymin) * (self.bbox[n].zmax - self.bbox[n].zmin))
                nodes.append((rank[members[0]], {'name': f'{big} assembly', 'children': list(members)}))
            else:
                nodes.append((rank[members[0]], members[0]))
        nodes.sort(key=lambda t: t[0])
        return [n for _r, n in nodes]

    def proposed_tree(self):
        return self._proposed_tree()

    def _build_tree(self):
        saved = load_tree(self.project_dir)
        if saved is None:
            return self._proposed_tree()
        tree, missing = normalize_tree(saved, self.parts, self.fasteners)
        if missing:
            base = self.suggest_order()
            tree.extend(sorted(missing, key=lambda n: base.index(n) if n in base else 1e9))
        return tree

    def _body_paths(self):
        """part -> path of its innermost enclosing ASSEMBLY, as a tuple of
        node ids (() for a part directly in the main assembly). Sets are
        transparent: their parts belong to the enclosing assembly."""
        out = {}

        def walk(nodes, path):
            for n in nodes:
                if isinstance(n, str):
                    out[n] = path
                elif is_fasten(n):
                    continue
                elif is_set(n):
                    walk(n['children'], path)
                else:
                    walk(n['children'], path + (id(n),))

        walk(self.tree, ())
        return out

    def _head_direction(self, fastener, axis):
        """+1 / -1 if the head (wider end) is at the high / low end along
        `axis`, or 0 when both ends look alike (stud, unknown)."""
        idx = 'XYZ'.index(axis)
        pts = self.bodies[fastener]._all_vertex_points()
        if not pts:
            return 0
        vals = [p[idx] for p in pts]
        lo, hi = min(vals), max(vals)
        span = hi - lo
        if span < 1e-6:
            return 0
        others = [i for i in range(3) if i != idx]

        def spread(sel):
            if not sel:
                return 0.0
            return max(max(p[i] for p in sel) - min(p[i] for p in sel) for i in others)

        low = spread([p for p in pts if p[idx] <= lo + 0.2 * span])
        high = spread([p for p in pts if p[idx] >= hi - 0.2 * span])
        if abs(high - low) < 0.15 * max(high, low, 1e-6):
            return 0
        return 1 if high > low else -1

    def _corridor(self, group):
        """Tool-access box beyond the head of the group's fasteners (a real
        solid, so non-rectangular parts are tested on actual material)."""
        key = (tuple(group['fasteners']), group['axis'])
        if key in self._access:
            return self._access[key]
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.gp import gp_Pnt
        axis = group['axis']
        idx = 'XYZ'.index(axis)
        boxes = []
        for f in group['fasteners']:
            sign = self._head_direction(f, axis)
            if sign == 0:
                continue
            bb = self.bbox[f]
            lo = [bb.xmin, bb.ymin, bb.zmin]
            hi = [bb.xmax, bb.ymax, bb.zmax]
            for i in range(3):
                if i != idx:
                    lo[i] += _CORRIDOR_SHRINK_MM
                    hi[i] -= _CORRIDOR_SHRINK_MM
                    if hi[i] <= lo[i]:
                        c = (lo[i] + hi[i]) / 2
                        lo[i], hi[i] = c - 0.01, c + 0.01
            if sign > 0:
                lo[idx], hi[idx] = hi_of(bb, idx) + 0.1, hi_of(bb, idx) + _CORRIDOR_LEN_MM
            else:
                lo[idx], hi[idx] = lo_of(bb, idx) - _CORRIDOR_LEN_MM, lo_of(bb, idx) - 0.1
            shape = BRepPrimAPI_MakeBox(gp_Pnt(*lo), gp_Pnt(*hi)).Shape()
            boxes.append((shape, lo, hi))
        self._access[key] = boxes
        return boxes

    def blockers(self, group, placed):
        """Names in `placed` (not part of the joint) sitting in the access
        corridor of the group's fasteners."""
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape
        out = []
        for name in placed:
            if name in group['bodies']:
                continue
            ob = self.bbox[name]
            for shape, lo, hi in self._corridor(group):
                if not (ob.xmin <= hi[0] and ob.xmax >= lo[0] and ob.ymin <= hi[1] and ob.ymax >= lo[1]
                        and ob.zmin <= hi[2] and ob.zmax >= lo[2]):
                    continue
                try:
                    d = BRepExtrema_DistShapeShape(shape, self.bodies[name].shape.wrapped)
                    hit = d.IsDone() and d.Value() < 1e-3
                except Exception:
                    hit = True
                if hit:
                    out.append(name)
                    break
        return out

    # ---- contact / support -------------------------------------------
    def touches(self, a, b):
        key = (a, b) if a < b else (b, a)
        if key not in self._touch:
            ok = _boxes_overlap(self.bbox[a], self.bbox[b], _TOUCH_TOL_MM) and \
                ag._shapes_touch(self.bodies[a].shape, self.bodies[b].shape, _TOUCH_TOL_MM)
            self._touch[key] = ok
            self._save_cache()
        return self._touch[key]

    def _mid_z(self, n):
        bb = self.bbox[n]
        return (bb.zmin + bb.zmax) / 2

    def supports(self, lower, upper):
        return self.touches(lower, upper) and self._mid_z(lower) < self._mid_z(upper) - 1e-6

    def _ground_z(self):
        return min(self.bbox[n].zmin for n in self.parts) if self.parts else 0.0

    def is_ground(self, n):
        return self.bbox[n].zmin <= self._ground_z() + _GROUND_TOL_MM

    def fastened_to(self, n, placed):
        for g in self.groups:
            if n in g['bodies'] and any(b in placed for b in g['bodies'] if b != n):
                return True
        return False

    def hold_tier(self, n, placed):
        """0 = on the ground / resting on a placed part, 1 = fastened to a
        placed part, 2 = only touching, 3 = not touching anything placed."""
        if self.is_ground(n) or any(self.supports(p, n) for p in placed):
            return 0
        if self.fastened_to(n, placed):
            return 1
        if any(self.touches(p, n) for p in placed):
            return 2
        return 3

    # ---- ordering ----------------------------------------------------
    def suggest_order(self):
        remaining = set(self.parts)
        if not remaining:
            return []

        def footprint(n):
            bb = self.bbox[n]
            return (bb.xmax - bb.xmin) * (bb.ymax - bb.ymin)

        free = [n for n in remaining if not self._enclosed_by_unplaced(n, remaining)]
        first = min(free or remaining, key=lambda n: (self.bbox[n].zmin, -footprint(n), n))
        order = [first]
        remaining.discard(first)
        while remaining:
            placed = set(order)
            scored = []
            for n in remaining:
                tier = self.hold_tier(n, placed)
                scored.append((tier, self.bbox[n].zmin, n))
            scored.sort()
            best_tier = scored[0][0]
            pool = [s for s in scored if s[0] == best_tier]
            pick = None
            for tier, _z, n in scored:
                if not self._would_block_pending(n, placed) and                         not self._enclosed_by_unplaced(n, remaining - {n}, placed):
                    pick = n
                    break
            if pick is None:
                pick = pool[0][2]
            order.append(pick)
            remaining.discard(pick)
        return order

    def _enclosed_by_unplaced(self, n, unplaced, placed=frozenset()):
        """Whether an unplaced part's bounding box wraps `n`'s (a housing
        around a gear): the container goes in first, contents after."""
        b = self.bbox[n]
        vol_n = (b.xmax - b.xmin) * (b.ymax - b.ymin) * (b.zmax - b.zmin)
        for m in unplaced:
            if m == n:
                continue
            c = self.bbox[m]
            vol_m = (c.xmax - c.xmin) * (c.ymax - c.ymin) * (c.zmax - c.zmin)
            t = _TOUCH_TOL_MM
            if (vol_m > vol_n * 1.2 and c.xmin - t <= b.xmin and c.xmax + t >= b.xmax and
                    c.ymin - t <= b.ymin and c.ymax + t >= b.ymax and
                    c.zmin - t <= b.zmin and c.zmax + t >= b.zmax and self.touches(m, n)
                    and not self._would_block_pending(m, set(placed))):
                return True
        return False

    def _would_block_pending(self, name, placed):
        """Whether placing `name` now would obstruct a fastener step that
        hasn't happened yet (some joined body still unplaced)."""
        after = placed | {name}
        for g in self.groups:
            if name in g['bodies']:
                continue
            if all(b in after or b not in self.parts for b in g['bodies']):
                continue
            if self.blockers(g, [name]):
                return True
        return False

    def effective_order(self):
        """Flat part order = the tree read top to bottom."""
        return flatten(self.tree)

    # ---- checks ------------------------------------------------------
    def check_order(self, order):
        """Human-readable warnings for `order` (non-fastener names)."""
        warnings = []
        placed = []
        done_groups = set()
        for i, n in enumerate(order, start=1):
            tier = self.hold_tier(n, set(placed))
            if i == 1:
                if not self.is_ground(n):
                    warnings.append(f'Part {i} "{n}" is not on the ground - the first part has nothing under it.')
            elif tier == 3:
                warnings.append(f'Part {i} "{n}" floats: it touches nothing already placed.')
            elif tier == 2:
                warnings.append(f'Part {i} "{n}" only touches placed parts from the side/above and is not '
                                f'fastened to any yet - it may not hold on its own.')
            placed.append(n)
            pset = set(placed)
            for gi, g in enumerate(self.groups):
                if gi in done_groups or not all(b in pset or b not in self.parts for b in g['bodies']):
                    continue
                done_groups.add(gi)
                blockers = self.blockers(g, placed)
                if blockers:
                    label = ag._fastener_label(g['fasteners'])
                    warnings.append(
                        f'{label} (installed after part {i}) cannot be tightened: head side is blocked by '
                        + ', '.join(f'"{b}"' for b in blockers) + '.')
        missing = [n for n in self.parts if n not in order]
        for n in missing:
            warnings.append(f'"{n}" is not in the order.')
        return warnings


def _common_prefix(paths):
    if not paths:
        return ()
    first = paths[0]
    n = 0
    while n < len(first) and all(len(p) > n and p[n] == first[n] for p in paths):
        n += 1
    return tuple(first[:n])


def type_key(name):
    """Name with Fusion's trailing instance numbering removed, so identical
    parts/fasteners (...v1-1, ...v1-2 / Bar 1, Bar 2) compare equal."""
    import re
    n = re.sub(r'\s+v\d+-\d+$', '', name, flags=re.IGNORECASE)
    return re.sub(r'[\s_-]*\d+$', '', n)


def lo_of(bb, idx):
    return (bb.xmin, bb.ymin, bb.zmin)[idx]


def hi_of(bb, idx):
    return (bb.xmax, bb.ymax, bb.zmax)[idx]
