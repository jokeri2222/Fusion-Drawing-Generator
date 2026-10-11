"""
Fusion 360 Script: Export Project For Drawings

For the active design, exports everything the drawing generator needs:
- One STEP file per originally-visible solid body (name matches the body)
- One Fusion Archive (.f3d) of the whole design
- One project_data.json with parameters, thread features, tap/hole info,
  and sketch dimensions/constraints pulled directly from the Fusion API
  (the F3D archive's internal format is an undocumented, Zstd-compressed
  binary blob - not something worth reverse-engineering - so this reads
  the same data straight out of the live model instead)

Only exports bodies that were visible when the script started.
For each visible body: hide all others, export, restore visibility.
If the progress dialog is cancelled, every file created this run is deleted.
"""

import adsk.core
import adsk.fusion
import os
import json
import re
import traceback

def sanitize_filename(name):
    """Filename-safe version of `name` - each run of one or more invalid
    characters becomes a single '-', not simply DELETED. Fusion's own
    occurrence names carry meaningful separators that aren't valid
    filename characters - notably the colon in its own auto-numbered
    instance suffix, e.g. "Hexagon Socket Head Cap Screw ... v1:2" for the
    2nd of several screw occurrences sharing one component. Deleting
    (rather than replacing) that colon used to silently glue "v1" and "2"
    together into "v12" - indistinguishable from a genuinely different
    part revision instead of clearly "instance 2 of v1", and a real
    problem for telling several identical fasteners apart in the export.
    """
    valid_chars = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_()')
    result = []
    prev_was_replaced = False
    for c in name:
        if c in valid_chars:
            result.append(c)
            prev_was_replaced = False
        elif not prev_was_replaced:
            result.append('-')
            prev_was_replaced = True
    return ''.join(result).strip(' -')

# Every body inside an inserted standard-hardware component (a McMaster-Carr
# fastener, say) is named by Fusion's own generic auto-numbering - "Body1",
# "Solid2" - regardless of what the fastener actually is; the real,
# descriptive name lives one level up, on the component/occurrence. Matches
# that placeholder pattern so `effective_body_name` knows when to look
# there instead of trusting the body's own name.
GENERIC_BODY_NAME_PATTERN = re.compile(r'^(Body|Solid)\s*\d*$', re.IGNORECASE)

# Fusion's own default name for an un-renamed base feature (an Insert-into-
# current-design / multi-body import operation) - "Base1", "Base2" - as
# opposed to one the user has actually renamed (e.g. "Motor"). Matches
# `GENERIC_BODY_NAME_PATTERN`'s role, just for `BRepBody.baseFeature.name`
# instead of the body's own name.
GENERIC_BASE_FEATURE_NAME_PATTERN = re.compile(r'^Base\s*\d*$', re.IGNORECASE)

def base_feature_group_name(base_feature):
    """The real browser-visible group name for a base feature, e.g. "Motor",
    or None if there isn't one worth using.

    Turns out renaming the base feature itself (`BaseFeature.name`, "Base1"
    -> something else) is NOT what actually produces a "<Name> (N)" folder
    in the Bodies browser - that name stays "Base1" even after the bodies
    are visibly grouped as "Motor". What DOES produce it: selecting the
    base feature's timeline entries and using the timeline's own "Group"
    command, which wraps them in a `TimelineGroup` - a distinct object
    reachable via `TimelineObject.parentGroup` - and IT is what the user
    renamed to "Motor". `Feature.timelineObject.parentGroup.name` is the
    real source. Both this and the feature's own name are checked (feature
    name preferred if somehow both are meaningfully renamed, group name as
    the main path) since either could in principle be the real answer
    depending on how a given design was built.
    """
    bf_name = (safe_value(lambda bf=base_feature: bf.name, '') or '').strip()
    if bf_name and not GENERIC_BASE_FEATURE_NAME_PATTERN.match(bf_name):
        return bf_name
    timeline_obj = safe_value(lambda bf=base_feature: bf.timelineObject, None)
    parent_group = safe_value(lambda t=timeline_obj: t.parentGroup, None) if timeline_obj is not None else None
    if parent_group is not None:
        group_name = (safe_value(lambda g=parent_group: g.name, '') or '').strip()
        if group_name and not GENERIC_BASE_FEATURE_NAME_PATTERN.match(group_name):
            return group_name
    return None

def effective_body_name(body, owning_component, occurrence_chain):
    """The name to actually use for this body in exports/filenames/JSON
    keys: its own name, unless that's one of Fusion's generic placeholders
    (see `GENERIC_BODY_NAME_PATTERN`) - e.g. every fastener body is named
    "Body1" no matter which real fastener it is - in which case:
    - the name of the timeline GROUP the body's base feature belongs to is
      used, if this body was produced by a base feature (`BRepBody.
      baseFeature`) that's part of one (see `base_feature_group_name`).
      This is what a browser "<Name> (N)" grouping of several generic
      bodies under the Bodies folder actually is - e.g. inserting a
      multi-body "Motor" STEP/IGES file directly into the design creates
      one base feature, then grouping that feature's timeline entry (and
      usually siblings) under a timeline "Group" named "Motor" is what
      makes the Bodies folder show "Motor (26)"; there's no separate
      "browser group" object independent of the timeline, and the group's
      name is readable via `baseFeature.timelineObject.parentGroup.name`.
    - otherwise, the deepest occurrence's own (real, descriptive) name, e.g.
      "Hexagon Socket Head Cap Screw M5x0.8x16mm" rather than "Body1" (the
      fastener case - no base feature involved, just an inserted component).
    Falls back further up the occurrence chain, then to the owning
    component's own name, in case even the immediate occurrence's name is
    itself generic; only gives up and returns the body's own (placeholder)
    name if nothing better is found anywhere.
    """
    raw_name = safe_value(lambda: body.name, None)
    name = (raw_name or '').strip()
    if name and not GENERIC_BODY_NAME_PATTERN.match(name):
        return raw_name
    base_feature = safe_value(lambda b=body: b.baseFeature, None)
    if base_feature is not None:
        group_name = base_feature_group_name(base_feature)
        if group_name:
            return group_name
    for occ in reversed(occurrence_chain):
        occ_name = (safe_value(lambda o=occ: o.name, '') or '').strip()
        if occ_name and not GENERIC_BODY_NAME_PATTERN.match(occ_name):
            return occ_name
    comp_name = (safe_value(lambda: owning_component.name, '') or '').strip()
    if comp_name and not GENERIC_BODY_NAME_PATTERN.match(comp_name):
        return comp_name
    return raw_name or 'Body'

def unique_export_names(entries):
    """entries: list of (bodies, owning_component, occurrence_chain) export
    units (`bodies` a list of one or more bodies - see
    `group_into_export_units`). Returns a parallel list of unique,
    filename-safe export names - each one `effective_body_name`'s pick
    (based on the unit's first/representative body), sanitized, with a
    numeric suffix appended whenever two entries would otherwise resolve to
    the same name (the common case for fasteners: several distinct
    physical screw occurrences all sharing one real component name, e.g.
    three "Hexagon Socket Head Cap Screw M5x16" instances). Computed ONCE,
    up front, and threaded through to every place that needs "this unit's
    name" (the STEP filename, the materials dict key in project_data.json,
    ...) so they can't independently disambiguate the same collision two
    different ways and end up disagreeing with each other.
    """
    used = {}
    names = []
    for bodies, owning_component, occ_chain in entries:
        base = sanitize_filename(effective_body_name(bodies[0], owning_component, occ_chain)) or 'Body'
        count = used.get(base, 0)
        used[base] = count + 1
        names.append(base if count == 0 else f'{base}_{count}')
    return names

def find_all_bodies_recursive(component, parent_occurrences=None, seen_paths=None):
    """Recursively find all (body, owning_component, occurrence_chain)
    tuples.

    A component that is instanced multiple times (e.g. a bolt/screw or
    other standard-hardware component inserted several times - real case:
    a "Fasteners" folder with several occurrences of the same "Hexagon
    Socket Head..." component, one per physical screw) is reached once per
    occurrence, but `component.bRepBodies` returns the SAME underlying
    body objects every time regardless of which occurrence you reached
    them through. The ORIGINAL version of this function deduped by the
    body's own `entityToken` alone for exactly that reason - reasonable
    for a body that's genuinely reached twice via a redundant traversal
    path, but wrong for genuinely distinct physical instances: it threw
    away every occurrence past the first entirely, silently reducing "3
    screws" to "1 screw" with no error or warning.

    The fix here is ONLY the dedup key, not the body objects themselves:
    now keyed on the full (occurrence path, body token) pair, so the SAME
    instance reached twice via a redundant traversal path still collapses
    to one, while N genuinely different occurrences of the same component
    correctly produce N distinct entries. (Two other approaches - reading
    per-instance PROXIED bodies via `occurrence.bRepBodies`, and via
    `BRepBody.createForAssemblyContext(occurrence)` - were tried here
    first; both handed back objects that threw `InternalValidationError`
    the moment almost any property was read on them, for bodies deep
    inside a real referenced/library sub-assembly. Proxies turn out not
    to be needed at all for this script's purposes anyway: the actual
    STEP export works entirely off Fusion's own VISIBILITY STATE at
    export time - see `set_instance_visible` - not off which Python
    object reference is held, so the plain, always-reliable
    `component.bRepBodies` objects are both simpler and safer here.)
    """
    if parent_occurrences is None:
        parent_occurrences = []
    if seen_paths is None:
        seen_paths = set()

    all_bodies = []
    path_key = tuple(safe_value(lambda o=occ: o.entityToken, id(occ)) for occ in parent_occurrences)

    for body in component.bRepBodies:
        token = safe_value(lambda: body.entityToken, None)
        key = (path_key, token)
        if token is not None:
            if key in seen_paths:
                continue
            seen_paths.add(key)
        all_bodies.append((body, component, list(parent_occurrences)))

    for occurrence in component.occurrences:
        if occurrence.component:
            all_bodies.extend(
                find_all_bodies_recursive(occurrence.component, parent_occurrences + [occurrence], seen_paths)
            )

    return all_bodies

def _entity_key(entity):
    """A stable dict key for one Fusion entity (body or occurrence) -
    entityToken when readable, else Python object identity."""
    token = safe_value(lambda: entity.entityToken, None)
    return token if token is not None else id(entity)

def group_into_export_units(entries):
    """entries: list of (body, owning_component, occurrence_chain) tuples
    (already filtered to real, exportable geometry). Returns a parallel
    list of (bodies, owning_component, occurrence_chain) "export units",
    where `bodies` is a list of ONE OR MORE bodies that should be exported
    together as a single STEP file / single project_data.json entry.

    A "<Name> (N)" grouping of several bodies under one component's Bodies
    folder in the browser (e.g. inserting a multi-body "Motor" STEP/IGES
    file directly into the design, rather than as its own component) is NOT
    a separate "browser group" object with no API - every body produced by
    that single insert operation shares one BASE FEATURE (`BRepBody.
    baseFeature`), which is exactly what the browser clusters under,
    regardless of what that feature or its timeline group ends up named
    (see `base_feature_group_name` / `effective_body_name` for where the
    real "Motor" display name actually comes from - it's not necessarily
    the base feature's own name). So: bodies that (a) still carry one of
    Fusion's generic placeholder names ("Body1", "Body2", ... - see
    GENERIC_BODY_NAME_PATTERN, because nobody renamed 26 individual
    sub-solids of one imported part by hand) and (b) share the same owning
    base feature (when there is one) - or, lacking a base feature, the same
    owning component AND occurrence, a defensive fallback for stray generic
    bodies with no recorded creation feature - are treated as one export
    unit. Two DIFFERENT occurrences of the same multi-body component still
    group separately, one export per physical instance. A component with
    only one generically-named body (nothing to actually group) is left as
    its own ordinary unit, same as before this existed.
    """
    order = []
    clusters = {}
    for body, owning_component, occ_chain in entries:
        raw_name = (safe_value(lambda b=body: b.name, '') or '').strip()
        is_generic = bool(raw_name) and bool(GENERIC_BODY_NAME_PATTERN.match(raw_name))
        if not is_generic:
            order.append(('single', len(order)))
            clusters[('single', order[-1][1])] = ([body], owning_component, occ_chain)
            continue
        occ_key = tuple(safe_value(lambda o=occ: o.entityToken, id(occ)) for occ in occ_chain)
        base_feature = safe_value(lambda b=body: b.baseFeature, None)
        if base_feature is not None:
            bf_key = safe_value(lambda bf=base_feature: bf.entityToken, id(base_feature))
            key = ('basefeature', bf_key, occ_key)
        else:
            comp_key = safe_value(lambda c=owning_component: c.entityToken, id(owning_component))
            key = ('generic-group', comp_key, occ_key)
        if key not in clusters:
            order.append(key)
            clusters[key] = ([], owning_component, occ_chain)
        clusters[key][0].append(body)

    units = []
    for key in order:
        bodies, owning_component, occ_chain = clusters[key]
        units.append((bodies, owning_component, occ_chain))
    return units

def set_instance_visible(body, occurrence_chain, visible):
    """Toggle the visibility of one specific INSTANCE - BOTH the body's own
    visibility AND every occurrence in its chain, not just one or the
    other. Fusion effectively ANDs these together (something only shows if
    its own body flag AND every ancestor occurrence are all visible), and
    the two failure modes this has to avoid are different:

    - Toggling ONLY body-level would show/hide every occurrence that
      shares that body's definition at once (wrong for N instances of the
      same fastener component - showing "the" body shows all N).
    - Toggling ONLY occurrence-level would show/hide every OTHER body that
      happens to live in the same occurrence along with it (wrong for a
      sub-assembly occurrence containing several distinct bodies, e.g.
      several real parts grouped under one "Pump Housing + lids..."
      occurrence - opening that occurrence to reveal one body reveals all
      of them, which is exactly what made every exported STEP file
      contain the whole design instead of one part).

    Setting BOTH avoids either failure: hiding everything sets every body
    AND every occurrence to invisible; showing one target then opens ONLY
    that target's own occurrence chain (leaving sibling occurrences - and
    so every OTHER body inside them - still hidden) and ONLY that target
    body's own flag (leaving every other body sharing that same chain
    still hidden). Swallows errors per-entity so one bad instance can't
    stop every other body's own hide/show/export turn.
    """
    try:
        body.isVisible = visible
    except Exception:
        pass
    for occ in occurrence_chain:
        try:
            occ.isVisible = visible
        except Exception:
            pass

def collect_original_visibility(all_body_entries):
    """{entity_key: original isVisible} for every body AND every
    occurrence appearing in any body's chain - covers every entity
    `set_instance_visible` might touch, so `restore_all_visibility` can put
    everything back exactly as found rather than just the leaf occurrence.
    """
    original = {}
    for body, _owning_component, occ_chain in all_body_entries:
        bkey = _entity_key(body)
        if bkey not in original:
            original[bkey] = safe_value(lambda b=body: b.isVisible, True)
        for occ in occ_chain:
            okey = _entity_key(occ)
            if okey not in original:
                original[okey] = safe_value(lambda o=occ: o.isVisible, True)
    return original

def restore_all_visibility(all_body_entries, original):
    for body, _owning_component, occ_chain in all_body_entries:
        try:
            body.isVisible = original.get(_entity_key(body), True)
        except Exception:
            pass
        for occ in occ_chain:
            try:
                occ.isVisible = original.get(_entity_key(occ), True)
            except Exception:
                pass

def is_effectively_visible(body, owning_component, occurrence_chain):
    """A body is only really visible if:
    - the body itself is visible
    - the Bodies folder it lives in (separate light bulb from the body itself) is on
    - the occurrence containing it is visible

    For the last check, use the deepest occurrence's `.isVisible` (Fusion's
    own aggregate visibility, which already accounts for the WHOLE ancestor
    chain and any occurrence-specific visibility override) rather than
    manually walking each occurrence's `.isLightBulbOn` and ANDing them
    together. The two are not equivalent: `.isLightBulbOn` is only that one
    occurrence's own browser-tree toggle, so a body hidden via an
    occurrence-specific override elsewhere in the path - or via a bulb
    state `.isLightBulbOn` doesn't fully capture - could still slip through
    a manual isLightBulbOn walk while `.isVisible` correctly reports it as
    hidden. This was found from a real case: three bodies belonging to
    newly-inserted hardware components (a motor, a shaft coupling) were
    hidden in the Fusion browser, yet still got exported - the manual
    isLightBulbOn walk missed it; `.isVisible` on the deepest occurrence is
    Fusion's own answer to "is this actually shown", so trust that instead.

    `body` may be an assembly-context PROXY (see `find_all_bodies_recursive`)
    rather than the plain underlying body - properties read on a proxy can,
    in rare cases, throw instead of returning a value (a real crash seen in
    practice: `.isVisible` on one raised `InternalValidationError`), so
    every property read here goes through `safe_value` - a body whose own
    visibility genuinely can't be determined is treated as NOT visible
    (skip it) rather than crashing the whole export or silently including
    something nobody could confirm is actually shown.
    """
    if not safe_value(lambda: body.isVisible, False):
        return False
    if not safe_value(lambda: owning_component.isBodiesFolderLightBulbOn, False):
        return False
    if occurrence_chain and not safe_value(lambda: occurrence_chain[-1].isVisible, False):
        return False
    return True

def step_file_has_geometry(path):
    """Sanity-check an exported STEP file actually contains a real solid,
    not just header/structural entities. Needed because a body can pass
    every live-Fusion check (isValid, non-zero face count, non-zero volume)
    and still export as a completely empty STEP file - observed with
    bodies belonging to a referenced/linked sub-component (an inserted
    motor assembly), where Fusion's STEP translator apparently couldn't
    resolve the actual geometry even though the body looked entirely
    normal in the browser. A real solid's STEP always contains
    ADVANCED_FACE/MANIFOLD_SOLID_BREP entities; their total absence means
    the file is geometrically empty regardless of its file size (an empty
    export still carries several KB of header/context entities, so file
    size alone doesn't catch this).
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        return 'ADVANCED_FACE' in content or 'MANIFOLD_SOLID_BREP' in content
    except Exception:
        return True  # can't read it back - don't false-flag as empty

def has_real_geometry(body):
    """Skip unstitched/degenerate bodies that carry no actual geometry.

    `isValid`/`faces.count > 0` alone isn't enough - a real case: three
    bodies belonging to a referenced/linked sub-component (an inserted
    motor assembly) reported valid, non-zero faces in Fusion, yet exported
    as completely empty STEP files (void bounding box, no resolvable
    geometry at all) - almost certainly an external reference that wasn't
    fully resolved at STEP-translation time. A zero-volume check catches
    the more common version of this (a body that's really just an open/
    unstitched shell) before ever attempting to export it; it's a
    necessary check but not sufficient by itself for the referenced-
    component case (see the post-export validation in `run()`, which
    catches what this can't).
    """
    try:
        if not body.isValid or body.faces.count == 0:
            return False
        if safe_value(lambda: body.isSolid, False):
            volume = safe_value(lambda: body.physicalProperties.volume, 0.0) or 0.0
            return volume > 1e-6
        # Non-solid (open/unstitched shell, surface body): no volume to
        # check by definition - fall back to surface area instead, so a
        # genuinely degenerate/zero-area shell is still rejected while a
        # real surface body (now exportable per explicit request) is not.
        area = safe_value(lambda: body.physicalProperties.area, 0.0) or 0.0
        return area > 1e-6
    except Exception:
        return False

def export_isolated_body_copy(export_manager, root_comp, bodies, occurrence_chain, output_file):
    """Export one or more bodies' geometry (`bodies`, a non-empty list -
    see `group_into_export_units` for when this is more than one: several
    generically-named bodies of the same Bodies-folder browser group,
    exported and merged into a single STEP file together) at its correct
    world-space transform, completely independent of Fusion's scene
    visibility, reference-document structure, or pattern/multi-occurrence
    bookkeeping.

    Both earlier approaches - hide-everything-but-one-instance then export
    root_comp (see the long comment at this function's call site), and
    passing the target Occurrence itself straight to
    `createSTEPExportOptions` - were confirmed (by counting solids in the
    exported STEP files with an outside tool) to still bundle every sibling
    occurrence of a shared/referenced component into one file. Both routes
    ultimately ask Fusion's STEP translator to resolve "this one occurrence
    of a component that's also referenced/placed elsewhere", and for a
    referenced (McMaster-Carr-style inserted) component specifically, the
    translator appears to always pull in the full reference structure
    rather than isolating the single placement asked for.

    This sidesteps that entirely by never asking the translator to resolve
    an occurrence of a REFERENCED component at all:
    1. `TemporaryBRepManager.copy(body)` makes a raw B-Rep copy that has no
       link back to any referenced document or shared component
       definition - just geometry.
    2. That copy is moved to its correct assembly position by applying the
       source occurrence chain's cumulative world transform (each
       occurrence's own `.transform`/`.transform2` is relative to its
       immediate parent occurrence, so the chain is composed leaf-to-root -
       matches `bodychain.py`'s documented requirement that exported bodies
       preserve their true absolute assembly position).
    3. The copy is dropped into a brand-new, throwaway, NOT-referenced
       component (`root_comp.occurrences.addNewComponent`), which is then
       what actually gets exported - a plain local component containing
       exactly one body, nothing else in the document to bundle in.
    4. The throwaway component is deleted again immediately after, whether
       the export succeeded or not.

    Returns (True, None) on success (the caller should treat `output_file`
    as written), or (False, reason) on any failure, `reason` a short
    diagnostic string - the caller should fall back to a different export
    route and may surface `reason` to the user, since a silently-swallowed
    exception here is exactly what made an earlier, less diagnosable
    version of this same bug hard to pin down.
    """
    temp_occ = None
    try:
        temp_mgr = adsk.fusion.TemporaryBRepManager.get()

        def compose(prefer_new):
            """World transform of the chain, composed leaf-to-root
            (`Matrix3D.transformBy` pre-multiplies: `this = m * this`, so
            leaf-first gives `root * ... * leaf`). `prefer_new` picks
            `.transform2` (documented as the fix for `.transform`'s known
            bad results) over the old `.transform`; either falls back to
            the other if unreadable."""
            m = adsk.core.Matrix3D.create()
            for occ in reversed(occurrence_chain):
                names = ('transform2', 'transform') if prefer_new else ('transform', 'transform2')
                t = None
                for attr in names:
                    t = safe_value(lambda o=occ, a=attr: getattr(o, a), None)
                    if t is not None:
                        break
                if t is not None:
                    m.transformBy(t)
            return m

        def candidate_bbox(matrix):
            """Union world bbox of every body in this unit under `matrix`,
            or None if it can't be computed."""
            lo = [float('inf')] * 3
            hi = [float('-inf')] * 3
            for body in bodies:
                c = temp_mgr.copy(body)
                if c is None or not temp_mgr.transform(c, matrix):
                    return None
                bb = c.boundingBox
                for i, (a, b) in enumerate(zip(bb.minPoint.asArray(), bb.maxPoint.asArray())):
                    lo[i] = min(lo[i], a)
                    hi[i] = max(hi[i], b)
            return lo + hi

        combined_transform = compose(True)
        chosen = 'transform2'
        score_lines = []
        # `.transform2` is the documented-correct property, but it was
        # confirmed wrong (rotation AND offset) for at least one occurrence
        # - a spider coupler nested two levels deep - where the old
        # `.transform` was right (placed it exactly centered between its
        # two hubs, with the rotation matching). Rather than trust either
        # blindly, when the two disagree anywhere in the chain, check each
        # candidate against Fusion's own world-space bounding box for this
        # occurrence (from `allOccurrences`, which resolves the full path)
        # and keep whichever lands closer.
        try:
            differs = any(
                (a is not None and b is not None and any(abs(x - y) > 1e-6 for x, y in zip(a, b)))
                for a, b in (
                    (safe_value(lambda o=o: o.transform2.asArray(), None), safe_value(lambda o=o: o.transform.asArray(), None))
                    for o in occurrence_chain
                )
            )
            if differs and occurrence_chain:
                path = '+'.join(safe_value(lambda o=o: o.name, '') for o in occurrence_chain)
                truth = None
                for proxy in root_comp.allOccurrences:
                    if safe_value(lambda p=proxy: p.fullPathName, None) == path:
                        pb = safe_value(lambda p=proxy: p.boundingBox, None)
                        if pb is not None:
                            truth = list(pb.minPoint.asArray()) + list(pb.maxPoint.asArray())
                        break
                alt = compose(False)
                bb_a, bb_b = candidate_bbox(combined_transform), candidate_bbox(alt)
                if truth and bb_a and bb_b:
                    score_a = sum(abs(x - y) for x, y in zip(bb_a, truth))
                    score_b = sum(abs(x - y) for x, y in zip(bb_b, truth))
                    score_lines.append(f'  bbox distance to Fusion world bbox: transform2={score_a:.4f} transform={score_b:.4f}')
                    if score_b < score_a - 0.01:
                        combined_transform = alt
                        chosen = 'transform'
                else:
                    score_lines.append(f'  bbox check unavailable (proxy found={truth is not None})')
        except Exception as e:
            score_lines.append(f'  bbox check failed: {e}')

        try:
            lines = [f'== {os.path.basename(output_file)}']
            for occ in occurrence_chain:
                t2 = safe_value(lambda o=occ: o.transform2.asArray(), None)
                t1 = safe_value(lambda o=occ: o.transform.asArray(), None)
                lines.append(
                    f'  {safe_value(lambda o=occ: o.name, "?")} '
                    f'transform2={[round(v, 5) for v in t2] if t2 else None} '
                    f'  transform(old)={[round(v, 5) for v in t1] if t1 else None}'
                )
            lines.extend(score_lines)
            lines.append(f'  chosen={chosen}')
            lines.append(f'  combined={[round(v, 5) for v in combined_transform.asArray()]}')
            with open(os.path.join(os.path.dirname(output_file), '_transform_diagnostics.txt'), 'a', encoding='utf-8') as f:
                f.write(chr(10).join(lines) + chr(10))
        except Exception:
            pass

        copies = []
        for body in bodies:
            copied = temp_mgr.copy(body)
            if copied is None:
                return False, 'TemporaryBRepManager.copy() returned None'
            if not temp_mgr.transform(copied, combined_transform):
                return False, 'TemporaryBRepManager.transform() rejected the occurrence transform (non-rigid/mirrored?)'
            copies.append(copied)

        temp_occ = root_comp.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        temp_component = temp_occ.component

        # In a parametric design, `BRepBodies.add()` on a plain body copy
        # fails with "A valid targetBaseFeature is required" (RuntimeError
        # 3) unless it happens inside a base-feature edit - direct-modeling-
        # style body insertion needs an explicit base feature to attach to,
        # even in a component that itself has no feature timeline of its
        # own yet (this new throwaway component still inherits the parent
        # design's parametric capability setting). All copies share ONE
        # base feature/edit so a multi-body group ends up as one STEP file,
        # not several.
        base_feature = temp_component.features.baseFeatures.add()
        base_feature.startEdit()
        try:
            for copied in copies:
                temp_component.bRepBodies.add(copied, base_feature)
        finally:
            base_feature.finishEdit()

        step_options = export_manager.createSTEPExportOptions(output_file, temp_component)
        export_manager.execute(step_options)
        return True, None
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'
    finally:
        if temp_occ is not None:
            try:
                temp_occ.deleteMe()
            except Exception:
                pass

def find_all_components_recursive(component, seen=None):
    """Recursively collect every unique component (design + all sub-components)"""
    if seen is None:
        seen = []
    if component not in seen:
        seen.append(component)
    for occurrence in component.occurrences:
        if occurrence.component:
            find_all_components_recursive(occurrence.component, seen)
    return seen

def safe_value(getter, default=None):
    try:
        return getter()
    except Exception:
        return default

def first_attr(obj, attr_names, default=None):
    """Try several possible attribute names in order, returning the first
    that exists and isn't None. Used for API objects (ThreadInfo /
    HoleFeature's thread-related properties) where the exact property name
    isn't verifiable from outside a live Fusion session - a wrong single
    guess wrapped in `safe_value` would silently return None forever
    (AttributeError swallowed, no error surfaced, no obvious sign anything
    was wrong other than the field being empty downstream). Trying several
    plausible names is a defensive hedge against exactly that: some tapped
    holes' thread designation was reportedly showing up empty in the
    generated drawings (falling back to a bare diameter callout) while
    others worked - consistent with one of the two thread-data code paths
    (ThreadFeature vs. HoleFeature's built-in tap option) using a property
    name that doesn't actually exist on that object.
    """
    if obj is None:
        return default
    for name in attr_names:
        try:
            val = getattr(obj, name)
        except Exception:
            continue
        if val is not None:
            return val
    return default

def real_value(design, raw_value, unit):
    """ModelParameter.value (and dimension/hole values) come back in Fusion's
    INTERNAL units (cm for length, radians for angle), never in the unit shown
    by `unit`/`expression`. Convert to that display unit so numbers are usable
    directly, instead of silently being off by a factor of 10 (cm->mm) or
    57.3 (rad->deg).
    """
    if raw_value is None:
        return None
    try:
        if unit == 'deg':
            return design.unitsManager.convert(raw_value, 'rad', 'deg')
        if not unit:
            return raw_value
        return design.unitsManager.convert(raw_value, 'cm', unit)
    except Exception:
        return raw_value

HOLE_TYPE_NAMES = {
    0: 'SimpleHole',
    1: 'CounterboreHole',
    2: 'CounterdrillHole',
    3: 'CountersinkHole',
}

def hole_type_name(hole_type_enum):
    try:
        return HOLE_TYPE_NAMES.get(int(hole_type_enum), str(hole_type_enum))
    except Exception:
        return str(hole_type_enum)

def collect_parameters(design):
    """Split into user-created (named, meaningful) vs. Fusion's auto-generated
    internal dimension-backing parameters (d123, d456, ...). The user ones are
    what actually carries design intent (e.g. "HousingBore"); the model ones
    are bulk plumbing but still needed to resolve expressions like "d696".
    """
    user_param_names = set()
    user_parameters = []
    for p in design.userParameters:
        user_param_names.add(p.name)
        user_parameters.append({
            'name': p.name,
            'value': real_value(design, p.value, p.unit),
            'unit': p.unit,
            'expression': p.expression,
            'comment': p.comment,
        })

    model_parameters = []
    for p in design.allParameters:
        if p.name in user_param_names:
            continue
        model_parameters.append({
            'name': p.name,
            'value': real_value(design, p.value, p.unit),
            'unit': p.unit,
            'expression': p.expression,
            'comment': p.comment,
        })

    return user_parameters, model_parameters

# How many faces ONE hole instance contributes, by hole type - needed to
# turn a HoleFeature's total `.faces.count` (which covers every position
# the feature was created at, since a single Hole feature can be created
# from multiple selected sketch points at once) back into an actual
# instance count. A plain drilled hole is 1 face; a counterbore/countersink
# adds a second face per instance; a counterdrill (drill + counterdrill,
# both cylindrical, no separate flat) is approximated as 2 as well - exact
# behavior for that type isn't verifiable without a live Fusion session.
FACES_PER_HOLE_INSTANCE = {
    0: 1,  # SimpleHole
    1: 2,  # CounterboreHole
    2: 2,  # CounterdrillHole (approximate)
    3: 2,  # CountersinkHole
}

def collect_thread_features(design, component):
    """Thread features applied to faces in this component.

    A single ThreadFeature can be created against MULTIPLE selected faces
    at once (the equivalent of the Hole tool's multi-point selection, for
    the Thread command) - it stays ONE feature object with one name, but
    `tf.faces.count` reflects every face it actually threaded. Recording
    just one JSON entry per ThreadFeature silently dropped every instance
    but the first from the exported data - e.g. a thread applied to 8
    holes at once showed up as "1 thread", and downstream diameter-based
    matching then only had one instance to hand out to whichever of the 8
    physical holes happened to claim it first, leaving the other 7 with no
    thread data at all and falling back to a bare, unlabeled diameter.
    Emitting one entry per face fixes this at the source.
    """
    threads = []
    try:
        thread_features = component.features.threadFeatures
    except Exception:
        return threads

    for tf in thread_features:
        info = safe_value(lambda: tf.threadInfo, None)
        raw_length = safe_value(lambda: tf.threadLength.value if tf.threadLength else None, None)
        designation = first_attr(info, ['threadDesignation', 'designation', 'fullDesignation'], None)
        instance_count = safe_value(lambda: tf.faces.count, 1) or 1
        thread_data = {
            'feature_name': tf.name,
            'component': component.name,
            'designation': designation,
            'thread_type': first_attr(info, ['threadType', 'type'], None),
            'is_internal': safe_value(lambda: tf.isInternal, None),
            'is_full_length': safe_value(lambda: tf.isFullLength, None),
            'is_modeled': safe_value(lambda: tf.isModeled, None),
            'thread_length_mm': real_value(design, raw_length, 'mm'),
        }
        if designation is None:
            # Surfaced instead of silently empty - if this shows up in the
            # exported JSON, none of the guessed attribute names on
            # ThreadInfo were right, and whoever's debugging this needs to
            # check the real property name in the Fusion API for their
            # installed version.
            thread_data['_designation_lookup_failed'] = True
        for _ in range(max(1, int(instance_count))):
            threads.append(dict(thread_data))
    return threads

def collect_hole_features(design, component):
    """Hole features - diameter, depth, and tap info for tapped holes.

    Same multi-instance issue as threads (see collect_thread_features):
    one HoleFeature can be created from several selected sketch points at
    once, staying one feature object while `hf.faces.count` covers every
    position it actually drilled. Divide by FACES_PER_HOLE_INSTANCE for
    this hole's type to recover the real instance count (a counterbore/
    countersink hole contributes 2 faces per position, not 1), and emit
    one JSON entry per instance instead of one per feature.
    """
    holes = []
    try:
        hole_features = component.features.holeFeatures
    except Exception:
        return holes

    for hf in hole_features:
        raw_diameter = safe_value(lambda: hf.holeDiameter.value, None)
        raw_tip_angle = safe_value(lambda: hf.tipAngle.value if hf.tipAngle else None, None)
        hole_type_enum = safe_value(lambda: hf.holeType, None)
        hole_data = {
            'feature_name': hf.name,
            'component': component.name,
            'hole_diameter_mm': real_value(design, raw_diameter, 'mm'),
            'hole_type': hole_type_name(hole_type_enum),
            'tip_angle_deg': real_value(design, raw_tip_angle, 'deg'),
        }
        try:
            if hf.isTapped:
                hole_data['is_tapped'] = True
                hole_data['tap_thread_type'] = first_attr(hf, ['threadType', 'tapThreadType'], None)
                # The nested thread-info object's own property name isn't
                # verifiable outside a live Fusion session either - same
                # defensive multi-name lookup as the ThreadFeature path
                # above (see first_attr's docstring for why this matters).
                thread_info_obj = first_attr(hf, ['threadDataInfo', 'threadInfo', 'tapThreadInfo'], None)
                designation = first_attr(thread_info_obj, ['threadDesignation', 'designation', 'fullDesignation'], None)
                hole_data['tap_designation'] = designation
                if designation is None:
                    hole_data['_tap_designation_lookup_failed'] = True
            else:
                hole_data['is_tapped'] = False
        except Exception:
            hole_data['is_tapped'] = False

        total_faces = safe_value(lambda: hf.faces.count, 1) or 1
        faces_per_instance = FACES_PER_HOLE_INSTANCE.get(hole_type_enum, 1) if hole_type_enum is not None else 1
        instance_count = max(1, round(total_faces / faces_per_instance))
        for _ in range(instance_count):
            holes.append(dict(hole_data))
    return holes

def collect_fillet_features(design, component):
    """Fillet features (constant-radius edge blends) in this component.

    The drawing pipeline currently has to INFER every fillet purely from
    STEP geometry (surface type + radius + angular span), which is a real,
    ongoing source of misclassification bugs (an edge-break fillet
    confused for a slot end, a straight-edge fillet missed entirely because
    it's geometrically a plain cylinder, two adjacent face patches of one
    edge double-counted as two features, etc.) - problems that don't exist
    for holes/threads because those DO have this kind of authoritative
    Fusion-side data to fall back on. Exporting the real feature list here
    doesn't replace the STEP geometry pass (matching a specific exported
    face back to the exact FilletFeature that created it isn't reliably
    possible across the Fusion API / STEP-translator boundary), but gives
    the pipeline a real nominal radius per feature to snap noisy
    STEP-measured radii to, and a genuine feature count to cross-check
    geometry-based grouping against.

    Confirmed against a live Fusion session (a diagnostic dump of a real
    FilletFeature) that `FilletFeature` has NEITHER a direct `.radius` NOR
    a `.parameters` collection (both raise AttributeError) - the actual
    radius lives one level down, on each of the feature's `.edgeSets`
    (a `FilletEdgeSets` collection). A constant-radius edge set exposes its
    own `.radius` (a ModelParameter); a variable-radius or chord-length
    edge set does not, so trying `.radius` and catching the failure is
    enough to tell them apart without needing to identify the edge set's
    exact subclass by name. A feature can have MULTIPLE edge sets (e.g.
    different radii applied to different edge groups in one feature) - the
    first constant-radius set found is reported as this feature's radius
    (matching the overwhelmingly common case of one edge set per feature);
    edge counts are summed across every set.
    """
    fillets = []
    try:
        fillet_features = component.features.filletFeatures
    except Exception:
        return fillets

    for ff in fillet_features:
        edge_sets = safe_value(lambda: list(ff.edgeSets), []) or []
        radius_raw, radius_unit = None, 'mm'
        edge_count = 0
        for es in edge_sets:
            edge_count += safe_value(lambda: es.edges.count, 0) or 0
            if radius_raw is None:
                radius_param = safe_value(lambda: es.radius, None)
                if radius_param is not None:
                    radius_raw = safe_value(lambda: radius_param.value, None)
                    radius_unit = safe_value(lambda: radius_param.unit, None) or 'mm'
        if not edge_sets:
            edge_count = safe_value(lambda: ff.faces.count, None)

        fillet_data = {
            'feature_name': ff.name,
            'component': component.name,
            'radius_mm': real_value(design, radius_raw, radius_unit) if radius_raw is not None else None,
            'is_rolling_ball_corner': safe_value(lambda: ff.isRollingBallCorner, None),
            'edge_set_count': len(edge_sets),
            'edge_count': edge_count,
            '_variable_radius': radius_raw is None,
        }
        if radius_raw is None:
            fillet_data['_radius_lookup_failed'] = True
        fillets.append(fillet_data)
    return fillets

def collect_chamfer_features(design, component):
    """Chamfer features (flat angled edge breaks) in this component - see
    `collect_fillet_features` for why this is exported even though it can't
    be matched face-for-face back to the STEP geometry: a real nominal
    distance/angle to snap noisy STEP-measured values to, and a genuine
    feature count to cross-check against.

    Confirmed against a live Fusion session: `ChamferFeature` has no
    `.parameters` either, and its own `.edges` can raise an error entirely
    ("Didn't roll editing feature back") even though the feature itself is
    perfectly valid - use `.faces.count` for the edge/face count instead,
    or sum each edge set's own `.edges.count` from `.edgeSets`. The actual
    distance/angle values live on `.chamferTypeDefinition`, whose concrete
    type depends on how the chamfer was specified:
      - EqualDistanceChamferTypeDefinition: one `.distance`
      - TwoDistancesChamferTypeDefinition: `.distanceOne` / `.distanceTwo`
      - DistanceAndAngleChamferTypeDefinition: `.distance` / `.angle`
    Tried in order below via safe_value rather than an explicit type check,
    since a wrong attribute access on the wrong concrete type just fails
    over to the next guess.
    """
    chamfers = []
    try:
        chamfer_features = component.features.chamferFeatures
    except Exception:
        return chamfers

    for cf in chamfer_features:
        type_def = safe_value(lambda: cf.chamferTypeDefinition, None)
        type_def_name = safe_value(lambda: type(type_def).__name__, None)

        distance_raw, distance_unit = None, 'mm'
        for getter in (lambda: type_def.distance, lambda: type_def.distanceOne):
            param = safe_value(getter, None)
            if param is not None:
                distance_raw = safe_value(lambda: param.value, None)
                distance_unit = safe_value(lambda: param.unit, None) or 'mm'
                if distance_raw is not None:
                    break

        angle_raw, angle_unit = None, 'deg'
        angle_param = safe_value(lambda: type_def.angle, None)
        if angle_param is not None:
            angle_raw = safe_value(lambda: angle_param.value, None)
            angle_unit = safe_value(lambda: angle_param.unit, None) or 'deg'

        edge_sets = safe_value(lambda: list(cf.edgeSets), []) or []
        edge_count = sum(safe_value(lambda: es.edges.count, 0) or 0 for es in edge_sets)
        if not edge_sets:
            edge_count = safe_value(lambda: cf.faces.count, None)

        chamfer_data = {
            'feature_name': cf.name,
            'component': component.name,
            'distance_mm': real_value(design, distance_raw, distance_unit) if distance_raw is not None else None,
            'angle_deg': real_value(design, angle_raw, angle_unit) if angle_raw is not None else None,
            'chamfer_type': type_def_name,
            'edge_count': edge_count,
        }
        if distance_raw is None:
            chamfer_data['_distance_lookup_failed'] = True
        chamfers.append(chamfer_data)
    return chamfers

def collect_sketch_data(design, component):
    """Sketch dimensions (nominal values) and geometric constraint counts per sketch"""
    sketches = []
    for sk in component.sketches:
        dims = []
        for d in sk.sketchDimensions:
            unit = safe_value(lambda: d.parameter.unit, None)
            raw_value = safe_value(lambda: d.parameter.value, None)
            # Not every SketchDimension subtype exposes .name (e.g.
            # SketchOffsetCurvesDimension doesn't) - fall back to the driving
            # parameter's name so it can still be traced back to `parameters`.
            dim_name = safe_value(lambda: d.name, None) or safe_value(lambda: d.parameter.name, None)
            dims.append({
                'name': dim_name,
                'value': real_value(design, raw_value, unit),
                'unit': unit,
                'expression': safe_value(lambda: d.parameter.expression, None),
            })
        sketches.append({
            'name': sk.name,
            'component': component.name,
            'is_fully_constrained': safe_value(lambda: sk.isFullyConstrained, None),
            'dimensions': dims,
            'constraint_count': safe_value(lambda: sk.geometricConstraints.count, None),
        })
    return sketches

def collect_material(design, body):
    """Each body's assigned physical material: name, density, and (if this
    installed Fusion API version exposes it) the thermal expansion
    coefficient - needed downstream to compute a safe manufacturing
    tolerance that accounts for thermal expansion/contraction between the
    manufacturing temperature and the part's operating temperature range.

    The exact property path for a material's thermal expansion coefficient
    isn't verifiable without a live Fusion session (Autodesk's API has
    changed which object exposes physical properties like this across
    versions), so - same defensive pattern as `first_attr()` above - several
    plausible lookups are tried in order. `density_kg_m3` comes from the
    BODY's own physicalProperties (already used elsewhere for the has-
    volume check), which is reliable regardless of the material property
    API's exact shape; the coefficient, if no lookup succeeds, is left None
    and `_cte_lookup_failed` is stamped so this is visible in the JSON
    rather than silently missing, and the drawing pipeline can fall back to
    its own name-based lookup table instead.
    """
    try:
        mat = body.material
    except Exception:
        mat = None

    # Fusion's physicalProperties.density is in kg/cm^3 (Fusion's internal
    # length unit is cm regardless of the document's own display unit) -
    # confirmed against a real export: Steel came back as 0.00785, exactly
    # kg/cm^3 for steel (~7.85 g/cm^3), not the g/cm^3 or kg/m^3 this was
    # first assumed to be. 1 m^3 = 1e6 cm^3, so multiply by 1e6 for kg/m^3.
    density_kg_cm3 = safe_value(lambda: body.physicalProperties.density)
    density = density_kg_cm3 * 1e6 if density_kg_cm3 is not None else None

    if mat is None:
        return {
            'name': None,
            'density_kg_m3': density,
            'thermal_expansion_coefficient_per_c': None,
            '_cte_lookup_failed': True,
        }

    name = safe_value(lambda: mat.name)

    cte_raw = None
    for getter in (
        lambda: mat.materialProperties.itemByName('Thermal Expansion Coefficient').value,
        lambda: mat.materialProperties.itemByName('Coefficient of Thermal Expansion').value,
        lambda: mat.thermalExpansionCoefficient,
    ):
        cte_raw = safe_value(getter)
        if cte_raw is not None:
            break

    # Confirmed against a real export: Fusion's "Steel" library material
    # came back as the raw number 12.0 - exactly the textbook CTE for steel
    # expressed as 12 micro-strain/degC (i.e. 12 um/m/degC, the standard
    # human-readable engineering convention for this property, sometimes
    # written 12 ppm/degC), NOT already-converted units of 1/degC (which
    # would be 0.000012). Storing the raw 12.0 as if it meant 1/degC would
    # make every downstream thermal-expansion calculation six orders of
    # magnitude too large. Converting here once, at the source, means every
    # consumer of this field can treat it as true 1/degC with no further
    # guessing.
    cte = cte_raw * 1e-6 if cte_raw is not None else None

    return {
        'name': name,
        'density_kg_m3': density,
        'thermal_expansion_coefficient_per_c': cte,
        '_cte_lookup_failed': cte is None,
    }


def build_assembly_tree(root_name, entries, export_names):
    """Nests `entries` (body, owning_component, occurrence_chain) - keyed by
    their parallel `export_names` - into a tree that mirrors the real
    Fusion occurrence hierarchy (root component -> occurrence -> child
    occurrence -> ... -> the bodies living directly at that level), for the
    Python-side body-inclusion selector to render as a `ttk.Treeview`
    instead of a flat list. Each node: {'name', 'bodies': [export names
    directly at this level], 'children': [child nodes]}. Grouped by
    occurrence entityToken (falling back to the occurrence's name when no
    token is readable) so two distinct occurrences that happen to share a
    display name still become two separate nodes, not one merged node.
    """
    root = {'name': root_name, 'bodies': [], 'children': {}}
    for (_bodies, _owning_component, occ_chain), export_name in zip(entries, export_names):
        node = root
        for occ in occ_chain:
            occ_name = (safe_value(lambda o=occ: o.name, '') or 'Component').strip() or 'Component'
            key = safe_value(lambda o=occ: o.entityToken, occ_name)
            child = node['children'].get(key)
            if child is None:
                child = {'name': occ_name, 'bodies': [], 'children': {}}
                node['children'][key] = child
            node = child
        node['bodies'].append(export_name)

    def _to_plain(node):
        return {
            'name': node['name'],
            'bodies': node['bodies'],
            'children': [_to_plain(c) for c in node['children'].values()],
        }

    return _to_plain(root)

def build_project_data(design, visible_body_entries=None, export_names=None, initial_visibility_by_key=None):
    root_comp = design.rootComponent
    all_components = find_all_components_recursive(root_comp)

    threads = []
    holes = []
    fillets = []
    chamfers = []
    sketches = []
    for comp in all_components:
        threads.extend(collect_thread_features(design, comp))
        holes.extend(collect_hole_features(design, comp))
        fillets.extend(collect_fillet_features(design, comp))
        chamfers.extend(collect_chamfer_features(design, comp))
        sketches.extend(collect_sketch_data(design, comp))

    user_parameters, model_parameters = collect_parameters(design)

    materials = {}
    body_types = {}
    if visible_body_entries:
        for (bodies, owning_component, occ_chain), safe_name in zip(visible_body_entries, export_names):
            materials[safe_name] = collect_material(design, bodies[0])
            # `isReferencedComponent` is Fusion's own, real signal for "this
            # component's definition was inserted from an external file/
            # library" (McMaster-Carr fasteners, other purchased hardware,
            # anything from Insert > Insert McMaster-Carr Component or a
            # derived/linked reference) - True for essentially every
            # off-the-shelf part, False for anything natively modeled in
            # THIS design, custom parts included. A real API property
            # instead of guessing from the body's name (which previously
            # swept up a body literally named "Motor Custom Key" as if it
            # were hardware, purely because "key" is also a fastener
            # keyword - a natively-designed part is never a referenced
            # component regardless of what it's named).
            is_referenced = safe_value(lambda c=owning_component: c.isReferencedComponent, False)
            occurrence_name = safe_value(lambda: occ_chain[-1].name, None) if occ_chain else None
            initially_visible = True
            if initial_visibility_by_key is not None:
                initially_visible = any(
                    initial_visibility_by_key.get(_entity_key(b), True) for b in bodies
                )
            body_types[safe_name] = {
                'is_referenced_component': is_referenced,
                'component_name': safe_value(lambda c=owning_component: c.name, None),
                'occurrence_name': occurrence_name,
                'initially_visible': initially_visible,
                'merged_body_count': len(bodies),
            }

    root_name = safe_value(lambda: design.rootComponent.name, 'Root') or 'Root'
    assembly_tree = build_assembly_tree(root_name, visible_body_entries or [], export_names or [])

    return {
        'user_parameters': user_parameters,
        'model_parameters': model_parameters,
        'threads': threads,
        'holes': holes,
        'fillets': fillets,
        'chamfers': chamfers,
        'sketches': sketches,
        'materials': materials,
        'body_types': body_types,
        'assembly_tree': assembly_tree,
    }

def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        design = adsk.fusion.Design.cast(app.activeProduct)

        if not design:
            ui.messageBox('Please open a Fusion 360 design file first')
            return

        root_comp = design.rootComponent
        all_body_entries = find_all_bodies_recursive(root_comp)

        # Every body is now a candidate for export regardless of its
        # visibility at the time the script is run - initial visibility is
        # still recorded (see `initial_visibility_by_key` below) and used
        # only as a HINT for the body-inclusion selector's default
        # (`body_inclusion.default_inclusion`), never to exclude a body
        # from being exported at all. Per explicit request: "export all
        # bodies but mark some to be assembly only" - a body being hidden
        # in Fusion at export time no longer silently removes it from the
        # whole pipeline the way it used to.
        #
        # Still excluded here (genuinely nothing to export, independent of
        # any inclusion choice): degenerate/unresolved geometry only - a
        # non-solid surface/open-shell body IS exportable (STEP supports
        # open shells fine) and is no longer rejected just for not being
        # solid, per explicit request.
        #
        # Also records WHY each rejected body was rejected (not just that it
        # was) - surfaced in the final summary dialog, so a body missing
        # from the export is a diagnosis, not a guess.
        exportable_body_entries = []
        rejected_bodies = []
        initial_visibility_by_key = {}
        for body, owning_component, occ_chain in all_body_entries:
            name = safe_value(lambda b=body: effective_body_name(b, owning_component, occ_chain), 'Body')
            initial_visibility_by_key[_entity_key(body)] = is_effectively_visible(body, owning_component, occ_chain)
            if not has_real_geometry(body):
                reason = 'no real geometry (degenerate/unresolved)'
            else:
                reason = None
            if reason:
                rejected_bodies.append((name, reason))
            else:
                exportable_body_entries.append((body, owning_component, occ_chain))

        if not exportable_body_entries:
            msg = 'No solid bodies found in this design'
            if rejected_bodies:
                msg += f'\n\n{len(rejected_bodies)} body(ies) were found but excluded:\n'
                for name, reason in rejected_bodies[:15]:
                    msg += f'  {name}: {reason}\n'
                if len(rejected_bodies) > 15:
                    msg += f'  ...and {len(rejected_bodies) - 15} more\n'
            ui.messageBox(msg)
            return

        # Merge bodies belonging to the same Fusion Bodies-folder "group"
        # (see `group_into_export_units`'s own docstring for why this is
        # detected via generic body naming rather than reading the group
        # itself - there's no API for the browser group at all) into single
        # export units before names are computed, so a 26-body group gets
        # ONE export name/STEP file/project_data.json entry, not 26.
        export_units = group_into_export_units(exportable_body_entries)

        # Computed once, up front - see `unique_export_names`'s own
        # docstring for why every other place that needs "this unit's
        # name" (STEP filename, materials dict key, progress/error
        # messages, visibility restore) uses THIS list instead of each
        # independently re-deriving (and risking disagreeing on) a name.
        export_names = unique_export_names(export_units)

        file_dialog = ui.createFolderDialog()
        file_dialog.title = 'Select Output Folder for Drawing Export'

        if file_dialog.showDialog() != 0:
            ui.messageBox('Export cancelled by user')
            return

        # Everything goes in an `exports` subfolder of the selected folder,
        # keeping the selected folder itself uncluttered.
        output_dir = os.path.join(file_dialog.folder, 'exports')

        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        progress = ui.createProgressDialog()
        progress.isCancelButtonShown = True
        # +2 steps: the F3D archive and the JSON metadata dump
        progress.show('Exporting Project For Drawings', 'Starting export...', 0, len(export_units) + 2)

        export_manager = design.exportManager
        successful = 0
        failed = []
        empty_step_files = []
        created_files = []
        was_cancelled = False
        # Every body for which export_isolated_body_copy() (the raw-copy
        # export route meant to fix shared/referenced fasteners bundling
        # sibling occurrences together) failed and fell back to the older
        # root_comp export instead - surfaced in the summary dialog so a
        # silent fallback is visible instead of looking identical to a
        # successful isolated export.
        isolated_copy_failures = []

        # Step 1: export the whole design as a single Fusion Archive
        try:
            doc_name = sanitize_filename(app.activeDocument.name) or 'design'
            f3d_file = os.path.join(output_dir, f'{doc_name}.f3d')
            counter = 1
            base_f3d = f3d_file
            while os.path.exists(f3d_file):
                base_name = os.path.splitext(base_f3d)[0]
                f3d_file = f'{base_name}_{counter}.f3d'
                counter += 1

            archive_options = export_manager.createFusionArchiveExportOptions(f3d_file)
            export_manager.execute(archive_options)
            created_files.append(f3d_file)
        except Exception as e:
            failed.append(f'F3D export: {str(e)}')
        progress.progressValue = 1
        adsk.doEvents()

        # Step 2: dump design parameters/threads/holes/sketch dimensions to JSON
        try:
            project_data = build_project_data(design, export_units, export_names, initial_visibility_by_key)
            json_file = os.path.join(output_dir, 'project_data.json')
            counter = 1
            base_json = json_file
            while os.path.exists(json_file):
                base_name = os.path.splitext(base_json)[0]
                json_file = f'{base_name}_{counter}.json'
                counter += 1

            with open(json_file, 'w', encoding='utf-8') as f:
                json.dump(project_data, f, indent=2)
            created_files.append(json_file)
        except Exception as e:
            failed.append(f'Drawing data JSON export: {str(e)}')
        progress.progressValue = 2
        adsk.doEvents()

        # Store original visibility of every body AND every occurrence
        # `set_instance_visible` might touch - see its own and
        # `collect_original_visibility`'s docstrings for why both levels,
        # not just one, need to be captured and later restored.
        original_visibility = collect_original_visibility(all_body_entries)

        try:
            # Iterate over every exportable body found (solid + real
            # geometry), regardless of its visibility at script start - see
            # the comment above `export_units`'s construction.
            for idx, (target_bodies, _owning_component, target_occ_chain) in enumerate(export_units):
                # User clicked Cancel or closed the dialog: stop immediately
                if progress.wasCancelled:
                    was_cancelled = True
                    break

                safe_name = export_names[idx]
                try:
                    progress.message = f'Exporting: {safe_name}'
                    progress.progressValue = 2 + idx
                    adsk.doEvents()

                    if progress.wasCancelled:
                        was_cancelled = True
                        break

                    # Hide every INSTANCE (occurrence-level where one
                    # exists, body-level only as the root-body fallback -
                    # see `set_instance_visible`'s own docstring for why
                    # body-level alone would show/hide every occurrence of
                    # a shared fastener component at once instead of just
                    # one). Already swallows its own errors, so one bad
                    # instance can't take down every other body's turn.
                    for body, _owning_component, occ_chain in all_body_entries:
                        set_instance_visible(body, occ_chain, False)

                    # Show ONLY this target instance (every body in the
                    # unit, for a merged Bodies-folder group - they all
                    # share the same occurrence chain, see
                    # `group_into_export_units`)
                    for target_body in target_bodies:
                        set_instance_visible(target_body, target_occ_chain, True)

                    # Force Fusion to actually process the visibility
                    # changes just made before the STEP translator reads
                    # the scene - `isVisible` assignments only queue a
                    # graphics/document update, they don't apply it
                    # synchronously. Without pumping the event loop here,
                    # the translator can read a STALE visibility snapshot
                    # (e.g. still the state from before this iteration's
                    # hide/show calls landed), which is exactly what was
                    # observed for shared fastener components used via
                    # multiple occurrences of the same component: every
                    # exported instance's STEP file came out containing
                    # ALL sibling occurrences' geometry instead of just the
                    # one meant to be visible, even though the same
                    # isolation logic worked correctly for every ordinary
                    # (non-shared) body. `refresh()` on top of `doEvents()`
                    # additionally forces the viewport's own redraw so the
                    # change is committed, not just queued.
                    adsk.doEvents()
                    try:
                        app.activeViewport.refresh()
                    except Exception:
                        pass
                    adsk.doEvents()

                    output_file = os.path.join(output_dir, f'{safe_name}.step')

                    # `unique_export_names` already disambiguates same-name
                    # bodies within THIS run, but a leftover file from an
                    # earlier run in the same output folder is still
                    # possible - keep a last-resort collision check.
                    counter = 1
                    base_file = output_file
                    while os.path.exists(output_file):
                        base_name = os.path.splitext(base_file)[0]
                        output_file = f'{base_name}_{counter}.step'
                        counter += 1

                    # Export a raw, non-referenced COPY of just this body,
                    # at its correct world position, instead of exporting
                    # root_comp (or even the target occurrence itself) and
                    # relying on Fusion to isolate one instance out of a
                    # scene that also contains its siblings. See
                    # `export_isolated_body_copy`'s own docstring: both of
                    # those routes were confirmed (by counting solids in
                    # the resulting STEP files with an outside tool) to
                    # still bundle every sibling occurrence of a shared/
                    # referenced fastener component into one file, even
                    # with the doEvents()/refresh() pump above - this is
                    # the fix for that, not another variant of the same
                    # visibility-based approach.
                    isolated_ok, isolated_error = export_isolated_body_copy(
                        export_manager, root_comp, target_bodies, target_occ_chain, output_file
                    )
                    if not isolated_ok:
                        isolated_copy_failures.append(f'{safe_name}: {isolated_error}')
                        # Fall back to the original hide-everything-but-
                        # one-instance + root_comp export for anything the
                        # isolated-copy route couldn't handle (e.g. a body
                        # type TemporaryBRepManager can't copy) - better a
                        # possibly-imperfect export than none at all.
                        step_options = export_manager.createSTEPExportOptions(output_file, root_comp)
                        result = export_manager.execute(step_options)

                    # A body that looks entirely normal in Fusion (isValid,
                    # real faces, real volume - has_real_geometry already
                    # checked all that) can still translate to a STEP file
                    # with no actual solid in it (see step_file_has_geometry
                    # and has_real_geometry's own docstrings) - typically a
                    # referenced/linked component that didn't resolve at
                    # STEP-translation time. Per explicit request, don't
                    # leave a useless empty .step file behind for the
                    # drawing pipeline to trip over later (it would just
                    # produce a blank PDF for it) - delete it immediately
                    # and don't count it toward `successful`, only toward
                    # the empty-file warning list.
                    if step_file_has_geometry(output_file):
                        created_files.append(output_file)
                        successful += 1
                    else:
                        empty_step_files.append(safe_name)
                        try:
                            os.remove(output_file)
                        except Exception:
                            created_files.append(output_file)  # couldn't delete - at least track it for cleanup on cancel
                    progress.progressValue = 2 + idx + 1

                except Exception as e:
                    failed.append(f'{safe_name}: {str(e)}')
                    progress.progressValue = 2 + idx + 1
                    adsk.doEvents()

        finally:
            # Restore every body and occurrence to its original visibility.
            restore_all_visibility(all_body_entries, original_visibility)

            progress.hide()

        if was_cancelled:
            # Delete every file this run created, then stop without the summary dialog
            delete_failures = []
            for f in created_files:
                try:
                    if os.path.exists(f):
                        os.remove(f)
                except Exception as e:
                    delete_failures.append(f'{f}: {str(e)}')

            msg = f'Export cancelled.\n\nRemoved {len(created_files) - len(delete_failures)} of {len(created_files)} partial file(s).'
            if delete_failures:
                msg += '\n\nCould not delete:\n' + '\n'.join(delete_failures[:5])
            ui.messageBox(msg)
            return

        result_msg = f'Export Complete!\n\nExported: {successful} bodies + F3D + project_data.json\n'
        if isolated_copy_failures:
            result_msg += (
                f'\nISOLATED-COPY EXPORT FELL BACK for {len(isolated_copy_failures)} body(ies) '
                f'(used the older, less reliable root_comp export instead - a body counted here may '
                f'still have sibling-occurrence geometry bundled into its file):\n'
            )
            for entry in isolated_copy_failures[:10]:
                result_msg += f'  {entry}\n'
            if len(isolated_copy_failures) > 10:
                result_msg += f'  ...and {len(isolated_copy_failures) - 10} more\n'
        if failed:
            result_msg += f'Failed: {len(failed)}\n\n'
            for fail in failed[:3]:
                result_msg += f'  {fail}\n'
            if len(failed) > 3:
                result_msg += f'  ...and {len(failed)-3} more\n'

        if rejected_bodies:
            result_msg += (
                f'\nEXCLUDED: {len(rejected_bodies)} body(ies) were found in the design but '
                f'not exported:\n'
            )
            for name, reason in rejected_bodies[:15]:
                result_msg += f'  {name}: {reason}\n'
            if len(rejected_bodies) > 15:
                result_msg += f'  ...and {len(rejected_bodies) - 15} more\n'

        result_msg += (
            f'\n({len(all_body_entries)} total bodies found by the traversal before any '
            f'filtering - compare against what you see in the Fusion browser; if a body/'
            f'fastener you expect isn\'t in this count at all, the traversal never reached '
            f'it, which is a different problem than being excluded above.)\n'
        )
        result_msg += f'\nOutput folder:\n{output_dir}'
        ui.messageBox(result_msg)

        try:
            if os.name == 'nt':
                os.startfile(output_dir)
        except:
            pass

    except Exception as e:
        if ui:
            ui.messageBox(f'Error: {str(e)}\n\n{traceback.format_exc()}')
