"""Shared best-effort secret redaction for TRACE content.

Used on the capture path (before persisting) and on the retrieval path
(before injecting prior memory into model-visible context).

Best-effort: not guaranteed to detect every secret. Covers common patterns
for API keys, tokens, passwords, and credentials.
"""

from __future__ import annotations

import re

# (pattern, replacement[, flags]) — applied in order.
_PATTERNS: tuple = (
    (r'(api[_-]?key|secret|password|passwd|credential|auth[_-]?token|access[_-]?token|refresh[_-]?token)\s*[:=]\s*\S+', r'\1=***REDACTED***'),
    (r'sk-[a-zA-Z0-9]{32,}', '***REDACTED***'),
    (r'(sk|pk)_(live|test)_[a-zA-Z0-9]{24,}', '***REDACTED***'),
    (r'gh[psuo]_[a-zA-Z0-9]{36}', '***REDACTED***'),
    (r'xox[baprs]-[\w-]{10,}', '***REDACTED***'),
    (r'AKIA[0-9A-Z]{16}', '***REDACTED***'),
    (r'(aws[_-]?secret[_-]?access[_-]?key)\s*[:=]\s*\S+', r'\1=***REDACTED***'),
    (r'AIza[0-9A-Za-z\-_]{35}', '***REDACTED***'),
    (r'bearer\s+[a-zA-Z0-9\._\-]{20,}', 'bearer ***REDACTED***', re.IGNORECASE),
    (r'-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----', '***REDACTED PRIVATE KEY***'),
    (r'(mongodb|postgres|mysql|redis)://[^:]+:[^@]+@', r'\1://***REDACTED:***REDACTED@', re.IGNORECASE),
    (r'eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}', '***REDACTED JWT***'),
)


def redact_text(content: str) -> str:
    """Redact secret-like values from content. Non-string input is returned as-is."""
    if not isinstance(content, str) or not content:
        return content
    result = content
    for pattern in _PATTERNS:
        if len(pattern) == 3:
            regex, replacement, flags = pattern
            result = re.sub(regex, replacement, result, flags=flags)
        else:
            regex, replacement = pattern
            result = re.sub(regex, replacement, result, flags=re.IGNORECASE)
    return result
