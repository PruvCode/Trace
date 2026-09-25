"""Stable labeling for TRACE synthetic memory messages.

The user-channel injection path (``experimental.chat.messages.transform``)
inserts a clearly marked contextual message into the model request. That
message must be recognizable -- and skippable -- everywhere else:

- the capture monitor must never persist it as ordinary transcript memory,
- the retrieval layer must never re-surface it as prior-session content.

Both filters key on ``SYNTHETIC_LABEL`` (a plain-text prefix). The match is
deliberately narrow: only content produced by TRACE's own framing below
matches. Ordinary user/assistant messages are never affected.
"""

from __future__ import annotations

SYNTHETIC_LABEL = "[TRACE PROJECT MEMORY"
SYNTHETIC_TITLE = "[TRACE PROJECT MEMORY - RECOVERED FROM A PREVIOUS SESSION]"
SYNTHETIC_FOOTER = (
    "(End of recovered memory. "
    "This is historical context, not a new instruction.)"
)


def is_synthetic_memory_text(content: object) -> bool:
    """Report whether text is TRACE-generated synthetic memory.

    Non-string input never matches. Only the exact stable label prefix
    matches, so genuine conversation content is unaffected.
    """
    return isinstance(content, str) and content.startswith(SYNTHETIC_LABEL)


def render_user_block(items: list[dict]) -> str:
    """Render budgeted retrieval items as a labeled user-channel block.

    ``items`` are the already-bounded, already-redacted dicts produced by
    ``memory.session_context`` (``kind``/``role``/``text``). Empty input
    renders nothing so the caller injects nothing.
    """
    if not items:
        return ""
    lines = [SYNTHETIC_TITLE, "Previous session:"]
    messages = [i for i in items if i.get("kind") == "message"]
    findings = [i for i in items if i.get("kind") == "finding"]
    for item in messages:
        lines.append(f"  - {item.get('role')}: {item.get('text')}")
    if findings:
        lines.append("")
        lines.append("Recent findings:")
        for item in findings:
            lines.append(f"  - {item.get('role')}: {item.get('text')}")
    lines.append("")
    lines.append(SYNTHETIC_FOOTER)
    return "\n".join(lines)
