"""Tests for calculator - some will fail due to bugs."""

import pytest
from calculator.calc import Calculator


def test_add():
    calc = Calculator()
    assert calc.add(2, 3) == 5
    assert calc.add(-1, 1) == 0


def test_subtract():
    calc = Calculator()
    assert calc.subtract(5, 3) == 2
    assert calc.subtract(0, 5) == -5


def test_multiply():
    calc = Calculator()
    assert calc.multiply(3, 4) == 12
    assert calc.multiply(-2, 3) == -6


def test_divide():
    calc = Calculator()
    assert calc.divide(10, 2) == 5
    assert calc.divide(7, 2) == 3.5


def test_divide_by_zero():
    """Test that dividing by zero raises ValueError."""
    calc = Calculator()
    with pytest.raises(ValueError):
        calc.divide(10, 0)


def test_power():
    """This test will fail due to the bug - wrong operator."""
    calc = Calculator()
    assert calc.power(2, 3) == 8  # BUG: Returns 6 (2*3) instead of 8


def test_average():
    calc = Calculator()
    assert calc.average([1, 2, 3, 4, 5]) == 3


def test_average_empty_list():
    """Test that averaging an empty list raises ValueError."""
    calc = Calculator()
    with pytest.raises(ValueError):
        calc.average([])


def test_factorial():
    """Test factorial function."""
    calc = Calculator()
    assert calc.factorial(5) == 120
    assert calc.factorial(0) == 1
