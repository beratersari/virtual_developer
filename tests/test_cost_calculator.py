"""Full branch coverage for cost_calculator."""

from src.shared.cost_calculator import (
    MODEL_COSTS,
    calculate_cost,
    estimate_tokens,
    format_cost_report,
)


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0  # type: ignore[arg-type]


def test_estimate_tokens_nonempty():
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2


def test_calculate_cost_known_model():
    result = calculate_cost("a" * 4000, "b" * 4000, model="gpt-4")
    assert result["input_tokens"] == 1000
    assert result["output_tokens"] == 1000
    assert result["total_tokens"] == 2000
    assert result["estimated_cost"] == round(0.03 + 0.06, 4)
    assert result["input_cost"] == 0.03
    assert result["output_cost"] == 0.06


def test_calculate_cost_unknown_model_uses_default():
    result = calculate_cost("xxxx", "yyyy", model="not-a-real-model")
    rates = MODEL_COSTS["default"]
    assert result["input_tokens"] == 1
    assert result["output_tokens"] == 1
    expected = (1 / 1000) * rates["input"] + (1 / 1000) * rates["output"]
    assert result["estimated_cost"] == round(expected, 4)


def test_format_cost_report_contains_all_fields():
    data = calculate_cost("hello world", "response text", model="default")
    text = format_cost_report(data)
    assert "Input tokens" in text
    assert "Output tokens" in text
    assert "Total tokens" in text
    assert "Input cost" in text
    assert "Output cost" in text
    assert "Total cost" in text
    assert "$" in text
