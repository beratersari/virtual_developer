# Sample Calculator Project

This is a sample Python project with intentional bugs for testing the JIRA Virtual Developer agent.

## Bugs Present

1. **`divide()`** - No division by zero check
   - Will raise `ZeroDivisionError` when `b=0`
   - Should raise a `ValueError` with a proper message

2. **`power()`** - Wrong operator
   - Uses multiplication (`*`) instead of power (`**`)
   - Returns `base * exponent` instead of `base ** exponent`

3. **`average()`** - No empty list check
   - Will raise `ZeroDivisionError` on empty list
   - Should check for empty list and raise `ValueError`

4. **`factorial()`** - Wrong base case
   - Returns `0` for `n < 1` instead of `1`
   - Base case should be `n <= 1` return `1`

## Running Tests

```bash
cd sample_project
pip install -e ".[test]"
pytest
```

Expected: Some tests will fail due to the bugs.

## Fixing with AI Agent

The JIRA Virtual Developer can fix these bugs. Run from the parent directory:

```bash
python cli.py test-issue --project sample_project \
  --title "Fix calculator bugs" \
  --description "Fix all bugs in calculator/calc.py"
```
