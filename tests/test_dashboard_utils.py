"""Tests for dashboard form number parsing, independent from Flask routes."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard_utils import read_bounded_form_numbers

FIELDS = [
    ("count", int, 1, 100),
    ("ratio", float, 0.1, 1.0),
]


def test_parses_int_and_decimal_comma():
    assert read_bounded_form_numbers({"count": "8", "ratio": "0,75"}, FIELDS) == {
        "count": 8, "ratio": 0.75
    }


def test_blank_fields_are_omitted():
    assert read_bounded_form_numbers({"count": "", "ratio": "  "}, FIELDS) == {}


def test_values_are_clamped_to_bounds():
    assert read_bounded_form_numbers({"count": "0", "ratio": "2"}, FIELDS) == {
        "count": 1, "ratio": 1.0
    }


def test_invalid_field_identifies_key():
    try:
        read_bounded_form_numbers({"count": "nope", "ratio": "0.5"}, FIELDS)
    except ValueError as exc:
        assert str(exc) == "count"
    else:
        raise AssertionError("Expected invalid numeric input to raise ValueError")
