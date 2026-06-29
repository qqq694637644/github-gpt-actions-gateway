from __future__ import annotations

from app.ci.logs import parse_failed_log


def test_parse_failed_log_finds_error() -> None:
    raw = "line 1\nline 2\nCMake Error at foo.cmake:1\nnext\n"
    parsed = parse_failed_log(raw, max_lines=50, max_bytes=5000)
    assert parsed.error_summary
    assert "CMake Error" in parsed.log_excerpt


def test_parse_failed_log_falls_back_to_tail() -> None:
    raw = "\n".join(f"line {i}" for i in range(300))
    parsed = parse_failed_log(raw, max_lines=50, max_bytes=5000)
    assert parsed.error_summary[0].startswith("No explicit")
    assert "line 299" in parsed.last_lines
