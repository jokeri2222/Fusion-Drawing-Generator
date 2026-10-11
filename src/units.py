"""Unit conversion and display formatting.

Everything inside the program (geometry, tolerances, temperatures) is stored in
millimetres, degrees Celsius and degrees. This module converts to and from the
units a person wants to see, and formats drawing dimensions in the chosen
length unit.
"""
import math

# name -> (symbol, millimetres per unit, display decimals)
LENGTH = {
    'millimetre': ('mm', 1.0, 3),
    'centimetre': ('cm', 10.0, 4),
    'metre': ('m', 1000.0, 6),
    'kilometre': ('km', 1e6, 9),
    'micrometre': ('µm', 1e-3, 0),
    'nanometre': ('nm', 1e-6, 0),
    'inch': ('in', 25.4, 4),
    'foot': ('ft', 304.8, 5),
    'yard': ('yd', 914.4, 5),
    'mile': ('mi', 1609344.0, 9),
    'thou / mil': ('mil', 0.0254, 1),
    'decimetre': ('dm', 100.0, 5),
    'nautical mile': ('nmi', 1852000.0, 9),
    'angstrom': ('Å', 1e-7, 0),
}

# name -> (symbol, degrees per unit, display decimals)
ANGLE = {
    'degree': ('°', 1.0, 2),
    'radian': ('rad', 180.0 / math.pi, 4),
    'gradian': ('gon', 0.9, 2),
    'arcminute': ("'", 1.0 / 60.0, 1),
    'arcsecond': ('"', 1.0 / 3600.0, 0),
    'milliradian': ('mrad', 180.0 / math.pi / 1000.0, 2),
    'turn': ('turn', 360.0, 5),
}

TEMPERATURE = {
    'Celsius': ('°C', 1),
    'Fahrenheit': ('°F', 1),
    'Kelvin': ('K', 2),
    'Rankine': ('°R', 1),
}

_state = {'length': 'millimetre', 'angle': 'degree', 'temperature': 'Celsius'}


def set_units(length=None, angle=None, temperature=None):
    if length in LENGTH:
        _state['length'] = length
    if angle in ANGLE:
        _state['angle'] = angle
    if temperature in TEMPERATURE:
        _state['temperature'] = temperature


def current(kind):
    return _state[kind]


# ---- length -----------------------------------------------------------------
def length_symbol(unit=None):
    return LENGTH[unit or _state['length']][0]


def length_decimals(unit=None):
    return LENGTH[unit or _state['length']][2]


def length_from_mm(value, unit=None):
    return value / LENGTH[unit or _state['length']][1]


def length_to_mm(value, unit=None):
    return value * LENGTH[unit or _state['length']][1]


def format_length(mm_value, unit=None, with_unit=False):
    unit = unit or _state['length']
    text = f'{length_from_mm(mm_value, unit):.{length_decimals(unit)}f}'
    return f'{text} {length_symbol(unit)}' if with_unit else text


def format_dimension(mm_value, tol_mm=None, unit=None):
    """Drawing label such as '12.500 ±0.020 mm' in the chosen unit."""
    unit = unit or _state['length']
    text = format_length(mm_value, unit)
    if tol_mm is not None:
        text += f' ±{format_length(tol_mm, unit)}'
    return f'{text} {length_symbol(unit)}'


# ---- angle ------------------------------------------------------------------
def angle_symbol(unit=None):
    return ANGLE[unit or _state['angle']][0]


def angle_decimals(unit=None):
    return ANGLE[unit or _state['angle']][2]


def angle_from_deg(value, unit=None):
    return value / ANGLE[unit or _state['angle']][1]


def angle_to_deg(value, unit=None):
    return value * ANGLE[unit or _state['angle']][1]


# ---- temperature ------------------------------------------------------------
def temperature_symbol(unit=None):
    return TEMPERATURE[unit or _state['temperature']][0]


def temperature_decimals(unit=None):
    return TEMPERATURE[unit or _state['temperature']][1]


def temp_from_c(value, unit=None):
    unit = unit or _state['temperature']
    if unit == 'Fahrenheit':
        return value * 9.0 / 5.0 + 32.0
    if unit == 'Kelvin':
        return value + 273.15
    if unit == 'Rankine':
        return (value + 273.15) * 9.0 / 5.0
    return value


def temp_to_c(value, unit=None):
    unit = unit or _state['temperature']
    if unit == 'Fahrenheit':
        return (value - 32.0) * 5.0 / 9.0
    if unit == 'Kelvin':
        return value - 273.15
    if unit == 'Rankine':
        return value * 5.0 / 9.0 - 273.15
    return value


def delta_from_c(value, unit=None):
    """A temperature DIFFERENCE (no offset) in the given unit."""
    unit = unit or _state['temperature']
    return value * 9.0 / 5.0 if unit in ('Fahrenheit', 'Rankine') else value
