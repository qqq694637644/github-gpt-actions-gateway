from __future__ import annotations

import re
from dataclasses import dataclass

ERROR_RE = re.compile(
    r"(error|failed|failure|exception|traceback|fatal|undefined reference|unresolved external symbol|cmake error|msb\d+|pytest failed|assertionerror|npm err!|error c\d+|fatal error c\d+|lnk\d+)",
    re.IGNORECASE,
)
ANNOTATION_RE = re.compile(r"::error(?:\s+[^:]*)?::(?P<message>.*)")


@dataclass
class ParsedLog:
    error_summary: list[str]
    annotations: list[dict[str, int | str | None]]
    log_excerpt: str
    last_lines: str
    truncated: bool


def _clip_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= max_bytes:
        return text, False
    clipped = data[:max_bytes].decode("utf-8", errors="replace")
    return clipped + "\n...[truncated]", True


def parse_failed_log(raw_log: str, *, max_lines: int = 500, max_bytes: int = 80_000) -> ParsedLog:
    lines = raw_log.splitlines()
    matches: list[int] = []
    annotations: list[dict[str, int | str | None]] = []

    for idx, line in enumerate(lines):
        if ERROR_RE.search(line):
            matches.append(idx)
        annotation_match = ANNOTATION_RE.search(line)
        if annotation_match:
            annotations.append({"line": idx + 1, "message": annotation_match.group("message").strip()})

    if matches:
        first = matches[0]
        start = max(0, first - 20)
        end = min(len(lines), first + 81)
        excerpt_lines = lines[start:end]
        summary_lines = []
        for idx in matches[:20]:
            clean = lines[idx].strip()
            if clean and clean not in summary_lines:
                summary_lines.append(clean[:1000])
    else:
        excerpt_lines = lines[-min(max_lines, len(lines)) :]
        summary_lines = ["No explicit error keyword found; returning tail of job log."] if raw_log else ["Job log is empty."]

    tail_count = min(200, max_lines, len(lines))
    last_lines = "\n".join(lines[-tail_count:])
    excerpt = "\n".join(excerpt_lines[:max_lines])

    excerpt, truncated_excerpt = _clip_bytes(excerpt, max_bytes)
    remaining = max(1000, max_bytes // 3)
    last_lines, truncated_tail = _clip_bytes(last_lines, remaining)

    return ParsedLog(
        error_summary=summary_lines[:20],
        annotations=annotations[:50],
        log_excerpt=excerpt,
        last_lines=last_lines,
        truncated=truncated_excerpt or truncated_tail,
    )
