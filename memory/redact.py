"""Shared best-effort secret redaction for TRACE content.

Used on the capture path (before persisting) and on the retrieval path
(before injecting prior memory into model-visible context).

Best-effort only: pattern matching catches common key formats and obvious
assignments, not every secret. Do not rely on it to make a transcript safe
to share.
"""

from __future__ import annotations

import re

_R = "***REDACTED***"

# (pattern, replacement) — applied in order, case-insensitive unless noted.
_PATTERNS: tuple = (
    # Private keys and connection strings first (they span other patterns).
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"), "***REDACTED PRIVATE KEY***"),
    (re.compile(r"\b(mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://[^:/\s]+:[^@\s]+@", re.I), r"\1://***REDACTED:***REDACTED@"),
    # Provider token formats (no fixed lengths: never leave a suffix behind).
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"), _R),                       # Anthropic
    (re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{16,}"), _R),     # OpenAI project/service
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{32,}"), _R),                            # OpenAI legacy and similar
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"), _R),       # Stripe
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), _R),                       # GitHub fine-grained
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), _R),                         # GitHub classic
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}"), _R),                          # GitLab
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), _R),                      # Slack
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), _R),                        # AWS access key id
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{35,}"), _R),                            # Google API key
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "***REDACTED JWT***"),
    (re.compile(r"\b(bearer)\s+[A-Za-z0-9._\-~+/]{20,}=*", re.I), r"\1 " + _R),
    # Assignments: lowercase config names and UPPER_CASE env-style names.
    (re.compile(r"\b(api[_-]?key|secret|password|passwd|passphrase|credential|auth[_-]?token|access[_-]?token|refresh[_-]?token|client[_-]?secret|aws[_-]?secret[_-]?access[_-]?key)(\s*[:=]\s*)['\"]?[^\s'\"]+['\"]?", re.I), r"\1=" + _R),
    (re.compile(r"\b([A-Z][A-Z0-9]*_(?:[A-Z0-9]+_)*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|PWD|CREDENTIALS?))(\s*[:=]\s*)['\"]?[^\s'\"]+['\"]?"), r"\1=" + _R),
    # Prose: "the password is hunter2".
    (re.compile(r"\b(password|passwd|passphrase|api key|secret key)(\s+(?:is|was|=)\s+)['\"]?[^\s'\",;]+", re.I), r"\1\2" + _R),
)


def redact_text(content: str) -> str:
    """Redact secret-like values from content. Non-string input is returned as-is."""
    if not isinstance(content, str) or not content:
        return content
    result = content
    for regex, replacement in _PATTERNS:
        result = regex.sub(replacement, result)
    return result
