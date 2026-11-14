"""Shared capture and recall for hosts that deliver text in lifecycle events."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path

from memory import redact, session_context, store, synthetic, transcript

MAX_MESSAGE_CHARS = 8_000
MAX_CONTEXT_CHARS = 3_000


def capture(db: Path, host: str, session: str, role: str, content: str, turn: str | None) -> None:
    if not content.strip() or synthetic.is_synthetic_memory_text(content):
        return
    clean = redact.redact_text(content)
    # Shortcut: replay is content-deduplicated when the host supplies no stable turn ID.
    identity = json.dumps([session, role, turn or clean], ensure_ascii=True)
    source_id = f"{host}:hook:{hashlib.sha256(identity.encode()).hexdigest()}"
    with closing(store.connect(db)) as conn, conn:
        conn.execute("PRAGMA busy_timeout = 750")
        transcript.record_message(
            conn, autocommit=False, session_id=session, role="system", content="Session started",
            metadata={"agent": host}, source_id=f"session:{session}",
        )
        transcript.record_message(
            conn, autocommit=False, session_id=session, role=role,
            content=clean[:MAX_MESSAGE_CHARS], metadata={"agent": host, "source": "native_hook"},
            source_id=source_id,
        )


def recall(db: Path, session: str, query: str) -> str:
    if not query.strip():
        return ""
    result = session_context.get_session_start_context(
        db, exclude_session_id=session, query=redact.redact_text(query)[:2_000],
        max_items=4, max_chars=MAX_CONTEXT_CHARS, project_root=db.parent.parent,
    )
    return synthetic.render_user_block(result["items"])
