"""Secret redaction for logs and stored content."""

from __future__ import annotations

import re
from typing import Iterable


DEFAULT_PATTERNS = [
    r"(?i)api[_-]?key\s*[:=]\s*['\"]?([\w-]{16,})",
    r"(?i)authorization\s*:\s*bearer\s+([\w.-]+)",
    r"(?i)password\s*[:=]\s*['\"]?([^\s'\"]+)",
    r"sk-[a-zA-Z0-9]{20,}",
    r"xai-[a-zA-Z0-9]{20,}",
    r"ghp_[a-zA-Z0-9]{20,}",
]


class SecretRedactor:
    def __init__(self, patterns: Iterable[str] | None = None) -> None:
        raw = list(patterns) if patterns else list(DEFAULT_PATTERNS)
        self._compiled = [re.compile(p) for p in raw]

    def redact(self, text: str) -> str:
        if not text:
            return text
        out = text
        for cre in self._compiled:
            out = cre.sub(lambda m: m.group(0).replace(m.group(m.lastindex or 0), "***REDACTED***")
                          if m.lastindex else "***REDACTED***", out)
        return out
