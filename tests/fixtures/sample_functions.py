# Source code strings used as agent inputs in tests.
# Each is a complete, self-contained Python snippet that the agent can generate tests for.

# Trivially pure — no exceptions, no dependencies
SIMPLE_ADD = """\
def add(a, b):
    return a + b
"""

# Multiple exception branches — tests whether the LLM covers all error paths
VALIDATE_AGE = """\
def validate_age(age):
    if not isinstance(age, int):
        raise TypeError("Age must be an integer")
    if age < 0:
        raise ValueError("Age cannot be negative")
    if age > 150:
        raise ValueError("Age exceeds realistic maximum")
    return age
"""

# Floating-point edge case — a naive test using == will fail due to rounding
ROUND_PERCENTAGE = """\
def round_percentage(value, total):
    if total == 0:
        raise ZeroDivisionError("Total cannot be zero")
    return round((value / total) * 100, 2)
"""

# Helper + main function in same module — tests whether the LLM imports correctly
# and does not try to import the helper separately
CALCULATE_TAX = """\
def get_tax_rate(country):
    rates = {"US": 0.07, "UK": 0.20, "DE": 0.19}
    return rates.get(country, 0.0)

def calculate_tax(price, country):
    rate = get_tax_rate(country)
    return round(price * rate, 2)
"""
