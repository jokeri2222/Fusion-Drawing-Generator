"""
Tolerance = machining margin (fit-limited) combined with thermal movement.

Machining margin: a percentage of the dimension, set by the preset
(loose / standard / tight), so a part still gets a tolerance when there is
no temperature swing at all. It is limited by the fit with the neighbours
(realistic clearance model): the margin can't be smaller than the user's
smallest tolerance, and can't exceed the fit band, which is
- the smallest tolerance when the part touches another one (nearest gap
  <= the smallest tolerance - a tight fit),
- half the gap when there is a gap (the two parts share it),
- the largest tolerance when no neighbour is close.

Thermal movement for a dimension of length L (mm):
    delta_L = L * CTE * (T_operating - T_manufacturing)
taken separately per direction at the operating extreme that pushes that
way (0 when the part can't move that way). It is 0 when mfg == operating.

Total per direction = sqrt(margin^2 + thermal^2). Between two parts the
real gap (nearest-point distance, fit_check.py) also changes by the
difference of the parts' own thermal movements.

Material data comes from Fusion (project_data.json `materials`); a
name-based table is only a fallback when it isn't available.
"""

import math

# Machining margin as a fraction of the dimension, per preset.
# Per preset: (machining margin in % of the dimension, smallest tolerance in
# mm - touching parts, largest tolerance in mm - no close neighbour).
# 'custom' takes all three from the user's own values.
PRESET_VALUES = {
    'tight': (0.05, 0.01, 0.25),
    'standard': (0.2, 0.02, 0.5),
    'loose': (0.5, 0.05, 1.0),
}
TOLERANCE_PRESETS = {k: v[0] / 100.0 for k, v in PRESET_VALUES.items()}
DEFAULT_PRESET = 'standard'
PRESET_CHOICES = list(TOLERANCE_PRESETS) + ['custom']  # 'custom' uses ToleranceConfig.custom_percent

# Fallback coefficients of linear thermal expansion (1/degC), for when a
# body's material property can't be read from Fusion at all (older API,
# or no material assigned). Matched against the material name by substring,
# longest/most-specific keys checked first. Values are typical handbook
# figures for the material FAMILY, not a specific alloy - a real per-alloy
# value from Fusion (when available) is always preferred over this table.
_CTE_FALLBACK_TABLE = [
    ('stainless steel', 17.3e-6),
    ('carbon steel', 11.7e-6),
    ('mild steel', 11.7e-6),
    ('tool steel', 11.0e-6),
    ('steel', 12.0e-6),
    ('cast iron', 10.5e-6),
    ('aluminum', 23.1e-6),
    ('aluminium', 23.1e-6),
    ('titanium', 8.6e-6),
    ('brass', 19.0e-6),
    ('bronze', 18.0e-6),
    ('copper', 16.5e-6),
    ('nylon', 90e-6),
    ('abs', 90e-6),
    ('polycarbonate', 65e-6),
    ('pla', 68e-6),
    ('petg', 60e-6),
    ('acetal', 110e-6),
    ('delrin', 110e-6),
    ('pom', 110e-6),
    ('peek', 47e-6),
    ('rubber', 200e-6),
]
# Used when nothing else is known at all - a mid-range engineering-metal
# value, so an unrecognized/unassigned material still gets a plausible
# (if unlabeled-as-uncertain) thermal contribution rather than a silent 0.
_CTE_GENERIC_DEFAULT = 15e-6


def resolve_cte(material_entry):
    """(coefficient_per_c, is_estimated) for a project_data.json `materials`
    entry (`{'name', 'density_kg_m3', 'thermal_expansion_coefficient_per_c',
    '_cte_lookup_failed'}`, or None if the body has no material info at
    all). Prefers Fusion's own reported value; falls back to the name-based
    table, then to a generic default - `is_estimated` tells the caller
    (for a note on the drawing) whether this is a real per-material figure
    or a coarse guess.
    """
    if material_entry:
        cte = material_entry.get('thermal_expansion_coefficient_per_c')
        if cte:
            return cte, False
        name = (material_entry.get('name') or '').lower()
        for keyword, value in _CTE_FALLBACK_TABLE:
            if keyword in name:
                return value, True
    return _CTE_GENERIC_DEFAULT, True


class ToleranceConfig:
    """Tolerance policy: temperatures plus a fit band derived from how close
    each body sits to its neighbours.

    Fit band (`fit_band_mm`): if a body touches another (nearest distance
    <= `min_tolerance_mm`) it is a tight fit and gets `min_tolerance_mm`. With a
    gap, the two parts share it, so the band is gap / 2, clamped to
    [min_tolerance_mm, max_tolerance_mm]. A body with no neighbour in range
    (`body_gaps[name] is None` or unknown) gets `max_tolerance_mm`.
    `body_gaps` ({name: nearest gap mm or None}) is filled at run time from
    the real geometry (fit_check.nearest_gap_per_body), never saved.

    `body_overrides` (`{body_name: {manufacturing_temp_c?, operating_min_c?,
    operating_max_c?}}`) gives different bodies different thermal
    environments; `for_body()` resolves the effective config for one body.
    """

    def __init__(self, preset=DEFAULT_PRESET, manufacturing_temp_c=20.0, operating_min_c=20.0, operating_max_c=20.0,
                 body_overrides=None, custom_min_mm=0.02, custom_max_mm=0.5,
                 body_gaps=None, custom_percent=0.2, **_ignored):
        self.preset = preset if preset in PRESET_CHOICES else DEFAULT_PRESET
        self.manufacturing_temp_c = manufacturing_temp_c
        self.operating_min_c = operating_min_c
        self.operating_max_c = operating_max_c
        self.body_overrides = body_overrides or {}
        self.custom_min_mm = custom_min_mm
        self.custom_max_mm = max(custom_max_mm, custom_min_mm)
        self.body_gaps = body_gaps if body_gaps is not None else {}
        self.custom_percent = custom_percent  # % of the dimension, used when preset == 'custom'
        self.fit_mm = None  # set by for_body()

    def _values(self):
        if self.preset == 'custom':
            return self.custom_percent, self.custom_min_mm, self.custom_max_mm
        return PRESET_VALUES[self.preset]

    @property
    def margin_fraction(self):
        return max(self._values()[0], 0.0) / 100.0

    @property
    def min_tolerance_mm(self):
        return self._values()[1]

    @property
    def max_tolerance_mm(self):
        return max(self._values()[2], self._values()[1])

    def fit_band_mm(self, gap_mm):
        if gap_mm is None:
            return self.max_tolerance_mm
        if gap_mm <= self.min_tolerance_mm:
            return self.min_tolerance_mm
        return min(self.max_tolerance_mm, max(self.min_tolerance_mm, gap_mm / 2.0))

    def for_body(self, body_name):
        """Effective config for `body_name`: its temperature overrides (if
        any) on top of the global values, with its own fit band resolved."""
        override = self.body_overrides.get(body_name) or {}
        cfg = ToleranceConfig(
            preset=override.get('preset', self.preset),
            manufacturing_temp_c=override.get('manufacturing_temp_c', self.manufacturing_temp_c),
            operating_min_c=override.get('operating_min_c', self.operating_min_c),
            operating_max_c=override.get('operating_max_c', self.operating_max_c),
            body_overrides=self.body_overrides,
            custom_min_mm=self.custom_min_mm, custom_max_mm=self.custom_max_mm,
            body_gaps=self.body_gaps, custom_percent=self.custom_percent,
        )
        cfg.fit_mm = cfg.fit_band_mm(self.body_gaps.get(body_name))
        return cfg

    @property
    def max_delta_t(self):
        return max(
            abs(self.manufacturing_temp_c - self.operating_min_c),
            abs(self.manufacturing_temp_c - self.operating_max_c),
        )

    def to_dict(self):
        d = {
            'preset': self.preset,
            'custom_percent': self.custom_percent,
            'manufacturing_temp_c': self.manufacturing_temp_c,
            'operating_min_c': self.operating_min_c,
            'operating_max_c': self.operating_max_c,
            'custom_min_mm': self.custom_min_mm,
            'custom_max_mm': self.custom_max_mm,
        }
        if self.body_overrides:
            d['body_overrides'] = self.body_overrides
        return d

    @classmethod
    def from_dict(cls, d):
        if not d:
            return cls()
        return cls(
            preset=d.get('preset', DEFAULT_PRESET),
            custom_percent=d.get('custom_percent', 0.2),
            manufacturing_temp_c=d.get('manufacturing_temp_c', 20.0),
            operating_min_c=d.get('operating_min_c', 20.0),
            operating_max_c=d.get('operating_max_c', 20.0),
            body_overrides=d.get('body_overrides', {}),
            custom_min_mm=d.get('custom_min_mm', d.get('min_tolerance_mm', 0.02)),
            custom_max_mm=d.get('custom_max_mm', d.get('max_tolerance_mm', 0.5)),
        )


def compute_tolerance_asymmetric(value_mm, material_entry, config):
    """(plus_mm, minus_mm) - the dimension's true, possibly ASYMMETRIC +/-
    tolerance, honest about which direction the thermal contribution
    actually pushes.

    `config.max_delta_t` (used by `compute_tolerance` below) always takes
    the LARGER of |mfg - op_min| and |mfg - op_max| and applies it as a
    single band in BOTH directions - fine for a body whose operating range
    straddles its manufacturing temperature, but wrong for one that only
    ever runs hotter (or only ever colder): a body made at 20C that only
    ever operates at 60-100C never shrinks below its as-made size at all,
    so a "-thermal" contribution on the low side isn't physically
    meaningful and shouldn't inflate that side's tolerance.

    Each direction is computed from the SIGNED thermal delta at that
    direction's own worst operating extreme, clamped to zero if the whole
    operating range sits on the other side of manufacturing temp (i.e. the
    dimension genuinely can't move that way), and returned as is (thermal only,
    kept honestly one-sided instead of forcing symmetry).
    """
    if value_mm is None:
        return None, None
    cte, _is_estimated = resolve_cte(material_entry)
    signed_at_min = value_mm * cte * (config.operating_min_c - config.manufacturing_temp_c)
    signed_at_max = value_mm * cte * (config.operating_max_c - config.manufacturing_temp_c)
    thermal_plus = max(signed_at_max, signed_at_min, 0.0)
    thermal_minus = max(-signed_at_min, -signed_at_max, 0.0)
    band = config.fit_mm if config.fit_mm is not None else config.min_tolerance_mm
    margin = min(max(abs(value_mm) * config.margin_fraction, config.min_tolerance_mm), band)
    return math.hypot(margin, thermal_plus), math.hypot(margin, thermal_minus)


def compute_tolerance(value_mm, material_entry, config):
    """Total +/- tolerance (mm) for a dimension of `value_mm`: the larger
    of the two (possibly asymmetric - see `compute_tolerance_asymmetric`)
    directional tolerances, i.e. the single worst-case magnitude a
    machinist should work to when only one +/- figure is wanted. Numerically
    identical to the older "always symmetric" formula for every dimension
    already printed on a generated drawing (both reduce to the same
    max(|mfg-op_min|,|mfg-op_max|)-based worst case) - this is a stricter,
    honestly-directional foundation the older formula was already a
    (correct, but opaque) shortcut for, not a change to what any existing
    "±X.XXX" callout shows.
    """
    plus, minus = compute_tolerance_asymmetric(value_mm, material_entry, config)
    if plus is None:
        return None
    return max(plus, minus)


def make_tolerance_fn(material_entry, config):
    """A ready-to-call `f(value_mm) -> tolerance_mm` closure for a specific
    body's material, bound to one tolerance policy - what the drawing
    generator actually threads through its dimension/callout text builders.
    """
    def fn(value_mm):
        return compute_tolerance(value_mm, material_entry, config)
    return fn


def config_file(project_dir, name, for_write=False):
    """Path of a saved config file: `<selected folder>/configs/<name>`.
    `project_dir` may be the selected folder or its `exports` subfolder;
    configs live beside `exports`, not inside it, so re-exporting never
    touches them. For reading, falls back to a legacy copy directly in
    `project_dir` if there's no file in `configs` yet.
    """
    import os

    base = os.path.dirname(project_dir) if os.path.basename(os.path.normpath(project_dir)) == 'exports' else project_dir
    path = os.path.join(base, 'configs', name)
    if for_write:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path
    legacy = os.path.join(project_dir, name)
    return path if os.path.exists(path) or not os.path.exists(legacy) else legacy


def load_tolerance_config(project_dir):
    """Read `tolerance_config.json` from a project folder, if present.
    Missing file or unreadable content quietly falls back to `ToleranceConfig()`
    defaults (standard preset, no temperature swing) - tolerance display is
    opt-in, so a project that has never set this up still generates
    drawings exactly as before, just without a thermal contribution.
    """
    import json
    import os

    path = config_file(project_dir, 'tolerance_config.json')
    if not os.path.exists(path):
        return ToleranceConfig()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return ToleranceConfig.from_dict(data)
    except Exception:
        return ToleranceConfig()


def save_tolerance_config(project_dir, config):
    import json
    import os

    path = config_file(project_dir, 'tolerance_config.json', for_write=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(config.to_dict(), f, indent=2)
    return path
