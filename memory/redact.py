"""Shared best-effort secret redaction for TRACE content.

The single source of redaction rules. Applied when anything is stored
(``transcript.record_message``, ``episodic.record_event``) and again wherever
stored text leaves TRACE (context injection, MCP results, logs), so rows
written before a rule existed are still covered on the way out.

Best-effort only: pattern matching catches common key formats and obvious
assignments, not every secret. A password mentioned in plain prose with no
nearby keyword ("log in with hunter2") is undetectable. Do not rely on this
to make a transcript safe to share.

Every repeat is bounded because stored text is untrusted: an unbounded run
before a required character makes a pattern quadratic on crafted input.
"""

from __future__ import annotations

import re

_R = "***REDACTED***"

# Words that always mark the next value as secret, anywhere in an identifier:
# password, db_password, dbPassword, jwtSecret.
_KEY_WORDS = (
    r"api[_-]?key|secret[_-]?key|access[_-]?key|encryption[_-]?key|signing[_-]?key|private[_-]?key|"
    r"account[_-]?key|shared[_-]?access[_-]?key|secret|password|passwd|passphrase|credentials?|"
    r"auth[_-]?token|access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token"
)
_IDENT = r"(?<![A-Za-z0-9_\-])"
_ASSIGN = (
    r"(['\"]?\s{0,8}[:=]\s{0,8})"  # separator, tolerating the closing quote of a JSON key
    r"(\"[^\"\n]{0,4096}\"|'[^'\n]{0,4096}'|`[^`\n]{0,4096}`|(?:[^\s'\"`&]|&(?!\w{1,64}=)){1,4096})"  # a following &name= is the next URL parameter
)
_SENSITIVE_KEY = re.compile(
    rf"[A-Za-z0-9_\-]{{0,40}}?(?:{_KEY_WORDS}|token|pass|pwd|pw|authorization|cookie)", re.I)
# Source-code shapes, not values: `password: str`, `password = get_password()`.
_CODE_VALUE = re.compile(
    r"^(?:none|null|nil|true|false|undefined|str|string|int|bool|bytes|=.*|"
    r"(?:self|cls|this|os|config|settings|args|env|process|req|request|body|params)\.[\w.\[\]'\"]+)$",
    re.I,
)
_CALL = re.compile(r"^[a-z_][\w.]*\(")
_PLACEHOLDER = re.compile(r"^(?:\$\{?[A-Z_][A-Z0-9_]*\}?|%[A-Z_][A-Z0-9_]*%|<[a-z][\w -]*>|\{\{.*\}\}|\*\*\*REDACTED.*)$")
_TYPE_HINT = re.compile(r"^(?:\w+\[.*|[A-Z][a-z]+[A-Z]\w*)$")
_PROSE_WORDS = frozenset("""required incorrect invalid missing wrong empty set expired not correct valid needed optional
stored hashed encrypted too the a an in on being protected reset unset unknown""".split())


def _unquoted(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'`" else value


def _looks_generated(value: str) -> bool:
    """Weak keyword ("token"), so require the value itself to look machine-made."""
    digits = sum(c.isdigit() for c in value)
    return (len(value) >= 8 and digits >= 3) or (
        len(value) >= 24 and any(c.isupper() for c in value) and any(c.islower() for c in value)
    )


def _is_code(key: str, sep: str, value: str) -> bool:
    bare = _unquoted(value).rstrip(",;):")
    return (
        not bare
        or bare.casefold() == key.casefold()
        or bool(_CODE_VALUE.match(bare) or _CALL.match(bare) or _PLACEHOLDER.match(bare))
        or (":" in sep and bool(_TYPE_HINT.match(bare)))
    )


def _strong_assign(match: re.Match) -> str:
    key, sep, value = match.groups()
    return match.group(0) if _is_code(key, sep, value) else f"{key}={_R}"


def _weak_assign(match: re.Match) -> str:
    key, sep, value = match.groups()
    if _is_code(key, sep, value) or not _looks_generated(_unquoted(value)):
        return match.group(0)
    return f"{key}={_R}"


def _strong_prose(match: re.Match) -> str:
    return match.group(0) if match.group(3).casefold() in _PROSE_WORDS else f"{match.group(1)}{match.group(2)}{_R}"


def _weak_prose(match: re.Match) -> str:
    return f"{match.group(1)}{match.group(2)}{_R}" if _looks_generated(match.group(3)) else match.group(0)


def _card(match: re.Match) -> str:
    digits = [int(c) for c in match.group(0) if c.isdigit()]
    if len(digits) not in (15, 16) or digits[0] not in (3, 4, 5, 6):
        return match.group(0)
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d > 4 else d * 2) for i, d in enumerate(reversed(digits)))
    return "***REDACTED CARD***" if total % 10 == 0 else match.group(0)


_SURROGATE = re.compile("[\ud800-\udfff]")

# (pattern, replacement) — applied in order.
_PATTERNS: tuple = (
    # Private keys and connection strings first (they span other patterns). A key
    # body cut off by truncation must not leak; a bare marker in code is left alone.
    (re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----(?:\s|\\n)[A-Za-z0-9+/=:\s\\\-]{0,20000}"
                r"(?:(?:BEGIN|END) [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----)?"), "***REDACTED PRIVATE KEY***"),
    (re.compile(r"\b(mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://[^:/\s]{1,256}:[^@\s]{1,256}@", re.I), r"\1://***REDACTED:***REDACTED@"),
    # Credentials embedded in any other URL.
    (re.compile(r"(?<![a-z0-9+.\-])([a-z][a-z0-9+.\-]{0,30})://[^:/\s@]{1,256}:[^@\s/]{0,256}@", re.I), r"\1://***REDACTED:***REDACTED@"),
    (re.compile(r"(?<![a-z0-9+.\-])([a-z][a-z0-9+.\-]{0,30}://)[A-Za-z0-9_\-]{16,256}@", re.I), r"\1" + _R + "@"),
    (re.compile(r"https://hooks\.slack\.com/services/\S{1,256}"), _R),
    (re.compile(r"https://discord(?:app)?\.com/api/webhooks/\S{1,256}"), _R),
    (re.compile(r"([?&]sig=)[^&\s]{1,512}"), r"\1" + _R),
    # Provider token formats (no suffix may be left behind).
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,512}"), _R),                     # Anthropic
    (re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{16,512}"), _R),   # OpenAI project/service
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{32,512}"), _R),                          # OpenAI legacy and similar
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,512}"), _R),     # Stripe
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,512}"), _R),                    # GitHub fine-grained
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,512}"), _R),                      # GitHub classic
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,512}"), _R),                       # GitLab
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,512}"), _R),                   # Slack
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), _R),                        # AWS access key id
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{35,512}"), _R),                         # Google API key
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,512}"), _R),                            # npm
    (re.compile(r"\bpypi-[A-Za-z0-9_\-]{30,512}"), _R),                        # PyPI
    (re.compile(r"\bhf_[A-Za-z0-9]{30,512}"), _R),                             # Hugging Face
    (re.compile(r"(?<![A-Za-z0-9_\-.])SG\.[A-Za-z0-9_\-]{16,128}\.[A-Za-z0-9_\-]{16,128}"), _R),  # SendGrid
    (re.compile(r"\b(?:gsk|xai)[_-][A-Za-z0-9]{20,512}"), _R),                 # Groq, xAI
    (re.compile(r"\b(?:shpat_|shpss_|whsec_|dop_v1_|dckr_pat_|glsa_|pplx-|ya29\.)[A-Za-z0-9_\-.]{16,512}"), _R),
    (re.compile(r"\bAGE-SECRET-KEY-1[0-9A-Z]{20,128}"), _R),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"), _R),                      # Telegram bot
    (re.compile(r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{10,4096}\.eyJ[A-Za-z0-9_\-]{10,4096}\.[A-Za-z0-9_\-]{10,4096}"), "***REDACTED JWT***"),
    # HTTP auth and command-line credentials.
    (re.compile(r"\b(bearer)\s{1,8}[A-Za-z0-9._\-~+/]{20,4096}=*", re.I), r"\1 " + _R),
    (re.compile(r"\b((?:proxy-)?authorization[\"']?\s{0,8}:\s{0,8}[\"']?(?:bearer|basic|token|digest|negotiate|apikey))\s{1,8}[^\s\"']{1,4096}", re.I), r"\1 " + _R),
    (re.compile(r"\b(x-api-key|x-auth-token|private-token)([\"']?\s{0,8}:\s{0,8}[\"']?)[^\s\"']{1,4096}", re.I), r"\1\2" + _R),
    (re.compile(r"\b((?:set-)?cookie[\"']?\s{0,8}:\s{0,8}[\"']?)[^\s=;\"']{1,256}=[^\r\n\"']{1,4096}", re.I), r"\1" + _R),
    (re.compile(r"\b(curl|wget|http)\b([^\n]{0,200}?\s(?:-u|--user)[\s=]{1,4})['\"]?[^\s'\":]{1,256}:[^\s'\"]{1,256}['\"]?"), r"\1\2" + _R),
    (re.compile(r"\b(sshpass\s{1,8}-p\s{1,8}|mysql\b[^\n]{0,100}?\s-p)[^\s'\"]{1,256}"), r"\1" + _R),
    (re.compile(r"(?<![\w-])(--(?:password|passwd|pass|token|api[-_]key|secret|client[-_]secret|auth[-_]token|access[-_]token))"
                r"(\s{1,8}|=)(?!\$)(?:\"[^\"\n]{0,4096}\"|'[^'\n]{0,4096}'|[^\s'\"-][^\s'\"]{0,4096})", re.I), r"\1\2" + _R),
    # Assignments.
    (re.compile(rf"{_IDENT}([A-Za-z0-9_\-]{{0,40}}?(?:{_KEY_WORDS})){_ASSIGN}", re.I), _strong_assign),
    (re.compile(rf"{_IDENT}(pass|pwd|pw|PASS|PWD|PGPASSWORD){_ASSIGN}"), _strong_assign),
    (re.compile(rf"\b([A-Z][A-Z0-9]{{0,40}}_(?:[A-Z0-9]{{1,40}}_){{0,8}}(?:PASSWORD|SECRET|PASS|PWD|CREDENTIALS?)){_ASSIGN}"), _strong_assign),
    (re.compile(rf"\b([A-Z][A-Z0-9]{{0,40}}_(?:[A-Z0-9]{{1,40}}_){{0,8}}(?:KEY|TOKEN)){_ASSIGN}"), _weak_assign),
    (re.compile(rf"{_IDENT}(token){_ASSIGN}", re.I), _weak_assign),
    # Prose: "the password is hunter2", "the service credential is cred_2026_b".
    (re.compile(r"\b(password|passwd|passphrase|api key|secret key|access key|private key)(\s{1,8}(?:is|was)\s{1,8})['\"`]?([^\s'\"`,;.]{1,4096})", re.I), _strong_prose),
    (re.compile(r"\b(credentials?|token|secret)(\s{1,8}(?:is|was)\s{1,8})['\"`]?([^\s'\"`,;]{1,4096})", re.I), _weak_prose),
    # Personal identifiers.
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "***REDACTED ID***"),
    (re.compile(r"\b\d(?:[ -]?\d){12,18}\b"), _card),
)


def redact_text(content: str) -> str:
    """Redact secret-like values from content. Non-string input is returned as-is.

    Lone surrogates, which JavaScript strings can carry, become U+FFFD: they
    cannot be encoded as UTF-8, so one such row would break every later write.
    """
    if not isinstance(content, str) or not content:
        return content
    result = _SURROGATE.sub("�", content)
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
        out = {}
        for key, item in value.items():
            sensitive = isinstance(key, str) and _SENSITIVE_KEY.fullmatch(key) is not None
            if sensitive and item not in (None, "", True, False) and not (isinstance(item, str) and _is_code(key, ":", item)):
                out[key] = _R
            else:
                out[key] = redact_value(item)
        return out
    return value
