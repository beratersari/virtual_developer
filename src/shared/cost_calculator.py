"""Cost calculator for tracking token usage and API costs."""

from typing import Dict


# Approximate token costs per 1K tokens (input/output)
# These are rough estimates - actual costs vary by provider and model
MODEL_COSTS = {
    # OpenAI models
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    
    # Anthropic models
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "claude-3-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    "claude-opus-4-6": {"input": 0.015, "output": 0.075},
    "claude-sonnet-4": {"input": 0.003, "output": 0.015},
    
    # Default fallback
    "default": {"input": 0.005, "output": 0.015},
}


def estimate_tokens(text: str) -> int:
    """Roughly estimate token count from text.
    
    A very rough approximation: ~4 characters per token on average.
    """
    if not text:
        return 0
    return len(text) // 4


def calculate_cost(
    input_text: str,
    output_text: str,
    model: str = "default",
) -> Dict[str, float]:
    """Calculate estimated cost for a session.
    
    Returns:
        Dict with input_tokens, output_tokens, total_tokens, estimated_cost
    """
    input_tokens = estimate_tokens(input_text)
    output_tokens = estimate_tokens(output_text)
    
    # Get cost rates for model (fallback to default)
    rates = MODEL_COSTS.get(model, MODEL_COSTS["default"])
    
    # Calculate cost per 1K tokens
    input_cost = (input_tokens / 1000) * rates["input"]
    output_cost = (output_tokens / 1000) * rates["output"]
    total_cost = input_cost + output_cost
    
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated_cost": round(total_cost, 4),
        "input_cost": round(input_cost, 4),
        "output_cost": round(output_cost, 4),
    }


def format_cost_report(cost_data: Dict[str, float]) -> str:
    """Format cost data for display."""
    return f"""💰 Cost Summary:
• Input tokens:  {cost_data['input_tokens']:,}
• Output tokens: {cost_data['output_tokens']:,}
• Total tokens:  {cost_data['total_tokens']:,}
• Input cost:    ${cost_data['input_cost']:.4f}
• Output cost:   ${cost_data['output_cost']:.4f}
• Total cost:    ${cost_data['estimated_cost']:.4f}
"""
