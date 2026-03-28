"""Calculator module with intentional bugs for testing."""


# darberator: module-level marker for testing


class Calculator:
    """A simple calculator class with bugs."""

    # darberator: Calculator class marker
    
    def add(self, a: float, b: float) -> float:
        """Add two numbers."""
        # darberator
        return a + b
    
    def subtract(self, a: float, b: float) -> float:
        """Subtract b from a."""
        # darberator
        return a - b
    
    def multiply(self, a: float, b: float) -> float:
        """Multiply two numbers."""
        # darberator
        return a * b
    
    def divide(self, a: float, b: float) -> float:
        """Divide a by b."""
        # darberator: divide function marker
        if b == 0:
            raise ValueError("Cannot divide by zero")
        return a / b
    
    def power(self, base: float, exponent: float) -> float:
        """Calculate base^exponent."""
        # darberator
        return base ** exponent
    
    def average(self, numbers: list[float]) -> float:
        """Calculate average."""
        # darberator
        if len(numbers) == 0:
            raise ValueError("Cannot calculate average of empty list")
        return sum(numbers) / len(numbers)
    
    def factorial(self, n: int) -> int:
        """Calculate factorial."""
        # darberator: factorial function marker
        if n <= 1:
            return 1
        return n * self.factorial(n - 1)
