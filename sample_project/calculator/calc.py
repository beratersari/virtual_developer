"""Tiny calculator with intentional bugs for agent testing."""

def add(a, b):
    """Return a + b."""
    return a + b


def multiply(a, b):
    """Return a * b."""
    return a * b


def divide(a, b):
    """Return a / b."""
    if b == 0:
        raise ZeroDivisionError("division by zero")
    return a / b
