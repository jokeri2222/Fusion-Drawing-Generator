"""Per-body inclusion classification for the drawing/assembly pipeline: a
user-editable override of what each exported body is actually FOR, on top
of (not instead of) `assembly_guide.is_fastener`'s automatic guess.

Three states:
- 'drawing': full per-body technical drawing AND placed in the assembly
  instructions pictorial. Default for any real (non-fastener) body.
- 'assembly_only': no dedicated per-body drawing, but still placed in the
  assembly instructions pictorial - the default for a detected fastener
  (a screw doesn't need its own dimensioned drawing, but obviously still
  needs to appear in the build sequence).
- 'ignore': excluded from EVERYTHING - never loaded, never drawn, never
  placed in the assembly, never shown in Per-Body Temperatures. Blocked
  before any processing touches the body at all, same guarantee a
  detected fastener already gets, just now user-selectable for ANY body
  (e.g. reference/construction geometry, a body that isn't part of the
  real design at all).

User choices are saved in a sidecar `body_inclusion.json` next to
project_data.json - deliberately NOT written into project_data.json
itself, so re-running the Fusion export (which regenerates project_data.json
from scratch) never clobbers a user's saved choices.
"""

import glob
import json
import os

import assembly_guide

VALID_STATES = ('drawing', 'assembly_only', 'ignore')


def _sidecar_path(project_dir, for_write=False):
    import tolerance
    return tolerance.config_file(project_dir, 'body_inclusion.json', for_write)


def default_inclusion(name, body_types):
    """The zero-configuration default for a body that has no saved choice
    yet. A detected fastener defaults to 'assembly_only', same as before
    this feature existed. A body that was hidden in Fusion at export time
    (`initially_visible: False` in body_types - see the export script)
    also defaults to 'assembly_only' rather than 'drawing': it used to be
    excluded from the export entirely, so the closest zero-configuration
    equivalent is "still show it in the assembly, but don't generate a
    dedicated drawing nobody asked for". Everything else defaults to
    'drawing', unchanged from before.
    """
    if assembly_guide.is_fastener(name, body_types):
        return 'assembly_only'
    info = body_types.get(name) if isinstance(body_types, dict) else None
    if isinstance(info, dict) and info.get('initially_visible') is False:
        return 'assembly_only'
    return 'drawing'


def load_inclusion(project_dir):
    """{body_name: state} for every *.step body found in `project_dir` -
    a saved choice from body_inclusion.json where present and still valid,
    `default_inclusion` (the fastener heuristic) for everything else,
    including any body added/renamed since the sidecar was last saved.
    """
    body_types = assembly_guide.load_body_types(project_dir)
    names = sorted(
        os.path.splitext(os.path.basename(p))[0]
        for p in glob.glob(os.path.join(project_dir, '*.step'))
    )

    saved = {}
    path = _sidecar_path(project_dir)
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                saved = json.load(f)
        except Exception:
            saved = {}

    result = {}
    for name in names:
        state = saved.get(name)
        result[name] = state if state in VALID_STATES else default_inclusion(name, body_types)
    return result


def save_inclusion(project_dir, inclusion):
    """Persist `inclusion` ({body_name: state}) to the sidecar file. Only
    recognized states are written - anything else silently falls back to
    `default_inclusion` on next load rather than corrupting the file.
    """
    clean = {name: state for name, state in inclusion.items() if state in VALID_STATES}
    with open(_sidecar_path(project_dir, for_write=True), 'w', encoding='utf-8') as f:
        json.dump(clean, f, indent=2, sort_keys=True)


def load_assembly_tree(project_dir):
    """The `assembly_tree` node (see `build_assembly_tree` in the Fusion
    export script) saved in this project's project_data.json, or None if
    the file/key isn't there yet (an older export, or a project that
    hasn't been re-exported since this feature was added) - callers should
    fall back to a flat list of bodies in that case.
    """
    path = os.path.join(project_dir, 'project_data.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return None
    return data.get('assembly_tree')


def included_step_paths(project_dir, states=('drawing', 'assembly_only')):
    """STEP file paths for every body whose inclusion state is in `states`,
    in the same sorted order `glob` + `_load_bodies`-style callers already
    use elsewhere in this project. Bodies marked 'ignore' (or any state not
    listed) are excluded here, BEFORE any caller loads/analyzes their real
    geometry - the same "blocked before anything happens" guarantee this
    project's fastener handling already established.
    """
    inclusion = load_inclusion(project_dir)
    paths = []
    for step_path in sorted(glob.glob(os.path.join(project_dir, '*.step'))):
        name = os.path.splitext(os.path.basename(step_path))[0]
        if inclusion.get(name) in states:
            paths.append(step_path)
    return paths
