"""Parse opencoderman-findings fences the way aMIR-mini does."""

from src.review.findings import split_findings
from src.review.position import format_discussion
from src.review.threads import is_finding_body


def test_split_strips_opencoderman_findings_fence():
    text = """### Özet
C++ change. 1 Critical.

```opencoderman-findings
{
  "findings": [
    {
      "path": "src/buf.cpp",
      "start_line": 6,
      "end_line": 6,
      "side": "new",
      "severity": "critical",
      "title": "stack buffer overflow",
      "body": "Unbounded strcpy."
    }
  ]
}
```
"""
    markdown, findings = split_findings(text)
    assert "opencoderman-findings" not in markdown
    assert "Unbounded strcpy" not in markdown
    assert "### Özet" in markdown
    assert len(findings) == 1
    assert findings[0].path == "src/buf.cpp"
    assert findings[0].start_line == 6
    assert findings[0].severity == "critical"
    assert findings[0].title == "stack buffer overflow"


def test_split_accepts_trailing_json_findings_object():
    text = """### Özet
ok

```json
{"findings": [{"path": "a.py", "start_line": 3, "title": "x", "body": "y"}]}
```
"""
    markdown, findings = split_findings(text)
    assert "findings" not in markdown
    assert len(findings) == 1
    assert findings[0].path == "a.py"


def test_split_does_not_eat_unrelated_json_fence():
    text = """### Özet
example:

```json
{"name": "login", "ok": true}
```
"""
    markdown, findings = split_findings(text)
    assert findings == []
    assert '{"name": "login"' in markdown


def test_split_drops_broken_findings_block():
    text = """### Özet
ok

```opencoderman-findings
{not json
```
"""
    markdown, findings = split_findings(text)
    assert "opencoderman-findings" not in markdown
    assert findings == []


def test_markdown_headings_recover_findings():
    text = """### Critical

#### 1. `src/a.py:10-12` — bad lock

**Why it is an issue and where**
Race on the cache.

**Suggested fix**
Use a mutex.
"""
    markdown, findings = split_findings(text)
    assert markdown
    assert len(findings) == 1
    assert findings[0].path == "src/a.py"
    assert findings[0].start_line == 10
    assert findings[0].end_line == 12
    assert findings[0].severity == "critical"
    assert "Race" in findings[0].body


def test_format_discussion_has_finding_marks():
    from src.review.findings import Finding

    body = format_discussion(
        Finding(
            path="a.py",
            start_line=1,
            end_line=1,
            side="new",
            severity="major",
            title="oops",
            body="why",
        )
    )
    assert is_finding_body(body)
    assert "<!-- yaver-finding -->" in body
    assert "<!-- creasy-finding -->" in body
    assert "**Önemli**" in body
