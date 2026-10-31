"""Shared best-effort secret redaction for TRACE content.

The single source of redaction rules. Applied when anything is stored
(``transcript.record_message``, ``episodic.record_event``) and again wherever
stored text leaves TRACE (context injection, MCP results, service logs), so
rows written before a rule existed are still covered on the way out.

Best-effort only: pattern matching catches common key formats and obvious
assignments, not every secret. A password mentioned in plain prose with no
nearby keyword ("log in with hunter2") is undetectable. Do not rely on this
to make a transcript safe to share.
"""

from __future__ import annotations

import re

_R = "***REDACTED***"

# Words that always mark the next value as secret.
_KEY_WORDS = (
    r"api[_-]?key|secret|client[_-]?secret|password|passwd|pwd|passphrase|credentials?|"
    r"auth[_-]?token|access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|"
    r"private[_-]?key|account[_-]?key|shared[_-]?access[_-]?key|aws[_-]?secret[_-]?access[_-]?key"
)
_ASSIGN = (
    r"(['\"]?\s*[:=]\s*)"  # separator, tolerating the closing quote of a JSON key
    r"(\"[^\"\n]*\"|'[^'\n]*'|`[^`\n]*`|[^\s'\"`&]+)"
)
_SENSITIVE_KEY = re.compile(rf"(?:.*[^A-Za-z0-9])?(?:{_KEY_WORDS}|token)$", re.I)
# Source-code shapes, not values: `password: str`, `password = get_password()`.
_CODE_VALUE = re.compile(
    r"^(?:none|null|nil|true|false|undefined|str|string|int|bool|bytes|optional\[.*|"
    r"[\w.]+\(.*|(?:self|os|config|settings|args|env|process|cls|this)\.[\w.\[\]'\"]+,?|"
    r"\$\{?[A-Z_][A-Z0-9_]*\}?|%[A-Z_][A-Z0-9_]*%|<[^>]*>|\{\{.*\}\}|\*{3}.*)$",
    re.I,
)


def _unquoted(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'`" else value


def _looks_generated(value: str) -> bool:
    """Weak keyword ("token"), so require the value itself to look machine-made."""
    digits = sum(c.isdigit() for c in value)
    return (len(value) >= 8 and digits >= 3) or (
        len(value) >= 24 and any(c.isupper() for c in value) and any(c.islower() for c in value)
    )


def _strong_assign(match: re.Match) -> str:
    key, _, value = match.groups()
    return match.group(0) if _CODE_VALUE.match(_unquoted(value)) else f"{key}={_R}"


def _weak_assign(match: re.Match) -> str:
    key, _, value = match.groups()
    return f"{key}={_R}" if _looks_generated(_unquoted(value)) else match.group(0)


def _weak_prose(match: re.Match) -> str:
    return f"{match.group(1)}{match.group(2)}{_R}" if _looks_generated(match.group(3)) else match.group(0)


def _card(match: re.Match) -> str:
    digits = [int(c) for c in match.group(0) if c.isdigit()]
    if len(digits) not in (15, 16) or digits[0] not in (3, 4, 5, 6):
        return match.group(0)
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d > 4 else d * 2) for i, d in enumerate(reversed(digits)))
    return "***REDACTED CARD***" if total % 10 == 0 else match.group(0)


# (pattern, replacement) — applied in order.
_PATTERNS: tuple = (
    # Private keys and connection strings first (they span other patterns).
    # An END line cut off by truncation must not expose the key body.
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?"
                r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)"), "***REDACTED PRIVATE KEY***"),
    (re.compile(r"\b(mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://[^:/\s]+:[^@\s]+@", re.I), r"\1://***REDACTED:***REDACTED@"),
    # Credentials embedded in any other URL.
    (re.compile(r"\b([a-z][a-z0-9+.\-]*)://[^:/\s@]+:[^@\s/]*@", re.I), r"\1://***REDACTED:***REDACTED@"),
    (re.compile(r"\b([a-z][a-z0-9+.\-]*://)[A-Za-z0-9_\-]{16,}@", re.I), r"\1" + _R + "@"),
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
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}"), _R),                               # npm
    (re.compile(r"\bpypi-[A-Za-z0-9_\-]{30,}"), _R),                           # PyPI
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}"), _R),                                # Hugging Face
    (re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"), _R),       # SendGrid
    (re.compile(r"\b(?:gsk|xai)[_-][A-Za-z0-9]{20,}"), _R),                    # Groq, xAI
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "***REDACTED JWT***"),
    # HTTP auth and command-line credentials.
    (re.compile(r"\b(bearer)\s+[A-Za-z0-9._\-~+/]{20,}=*", re.I), r"\1 " + _R),
    (re.compile(r"\b((?:proxy-)?authorization[\"']?\s*:\s*[\"']?(?:bearer|basic|token|digest|negotiate|apikey))\s+[^\s\"']+", re.I), r"\1 " + _R),
    (re.compile(r"\b(x-api-key|x-auth-token)([\"']?\s*:\s*[\"']?)[^\s\"']+", re.I), r"\1\2" + _R),
    (re.compile(r"\b((?:set-)?cookie[\"']?\s*:\s*[\"']?)[^\s=;\"']+=[^\r\n\"']+", re.I), r"\1" + _R),
    (re.compile(r"(?<![\w-])(-u|--user)(\s+|=)['\"]?[^\s'\":]+:[^\s'\"]+['\"]?"), r"\1\2" + _R),
    (re.compile(r"(?<![\w-])(--(?:password|passwd|pass|token|api[-_]key|secret|client[-_]secret|auth[-_]token|access[-_]token))"
                r"(\s+|=)['\"]?[^\s'\"-][^\s'\"]*['\"]?", re.I), r"\1\2" + _R),
    # Assignments: lowercase config names and UPPER_CASE env-style names.
    (re.compile(rf"(?<![A-Za-z0-9])({_KEY_WORDS}){_ASSIGN}", re.I), _strong_assign),
    (re.compile(rf"\b([A-Z][A-Z0-9]*_(?:[A-Z0-9]+_)*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|PWD|CREDENTIALS?)){_ASSIGN}"), _strong_assign),
    (re.compile(rf"(?<![A-Za-z0-9])(token){_ASSIGN}", re.I), _weak_assign),
    # Prose: "the password is hunter2", "the service credential is cred_2026_b".
    (re.compile(r"\b(password|passwd|passphrase|api key|secret key|access key|private key)(\s+(?:is|was)\s+)['\"`]?[^\s'\"`,;]+", re.I), r"\1\2" + _R),
    (re.compile(r"\b(credentials?|token|secret)(\s+(?:is|was)\s+)['\"`]?([^\s'\"`,;]+)", re.I), _weak_prose),
    # Personal identifiers.
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "***REDACTED ID***"),
    (re.compile(r"\b\d(?:[ -]?\d){12,18}\b"), _card),
)


def redact_text(content: str) -> str:
    """Redact secret-like values from content. Non-string input is returned as-is."""
    if not isinstance(content, str) or not content:
        return content
    result = content
    for regex, replacement in _PATTERNS:
        result = regex.sub(replacement, result)
    return result


def redact_value(value: object) -> object:
    """Redact strings anywhere in JSON-shaped data; sensitive keys lose their value."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _R if isinstance(key, str) and isinstance(item, str) and item
            and _SENSITIVE_KEY.fullmatch(key) and not _CODE_VALUE.match(item)
            else redact_value(item)
            for key, item in value.items()
        }
    return value
