"""Project-local joins from code entities to canonical engineering records."""

from collections import defaultdict
import json
from memory import episodic, evidence, redact

TYPES = {"investigation": "INVESTIGATED_IN", "attempt": "ATTEMPTED_IN", "decision": "DECIDED_IN",
         "observation": "OBSERVED_IN", "git_change": "CHANGED_BY"}
MAX_EXPORT_EVENTS = 800
MAX_EVIDENCE = 20


def _link(kind, payload):
    if payload.get("kind") == "test_outcome":
        return "TESTED_IN"
    if kind == "attempt" and payload.get("outcome") in ("failed", "failure", "rejected"):
        return "FAILED_IN"
    return TYPES[kind]


def _title(payload):
    for key in ("summary", "finding", "decision", "title", "message", "subject", "description", "reason"):
        text = payload.get(key)
        if isinstance(text, str) and text.strip():
            return redact.redact_text(text.strip())[:400]
    return ""


def candidates(conn, node_ids, task_id=None, limit=30, unassigned_only=False, *, exclude=()):
    """Shortlist exact definitions and evidence-backed decisions before routine file history."""
    if not node_ids:
        return []
    markers = ",".join("?" for _ in node_ids)
    scope = " AND e.task_id=?" if task_id else (" AND e.task_id IS NULL" if unassigned_only else "")
    return conn.execute(f"""
        WITH focus AS (SELECT * FROM graph_nodes WHERE id IN ({markers})),
        attached AS (
            SELECT n.id AS node_id, e.id AS event_id FROM focus n
            JOIN events e ON replace(e.file, char(92), '/')=n.path
            WHERE e.source != 'capture' OR json_extract(e.payload, '$.kind')='test_outcome'
              UNION
            SELECT n.id, l.event_id FROM focus n JOIN event_evidence l ON l.git_path=n.path
            WHERE n.kind IN ('file', 'module')
        )
        SELECT DISTINCT a.node_id, e.id, n.line IS NOT NULL AS definition_match,
            CASE WHEN e.type='decision' AND json_extract(e.payload, '$.status')='verified'
                 AND EXISTS (SELECT 1 FROM event_evidence ev WHERE ev.event_id=e.id)
                 THEN 1 ELSE 0 END AS verified_decision
        FROM attached a
        JOIN focus n ON n.id=a.node_id JOIN events e ON e.id=a.event_id
        WHERE (n.kind IN ('file', 'module') OR (n.line IS NOT NULL AND replace(e.file, char(92), '/')=n.path AND
          (e.symbol=n.qualified_name OR (e.symbol=n.name AND
            (SELECT count(*) FROM graph_nodes same WHERE same.path=n.path AND same.name=n.name AND same.line IS NOT NULL)=1))))
        {scope} AND e.id NOT IN (SELECT value FROM json_each(?))
        ORDER BY definition_match DESC, verified_decision DESC, e.id DESC, a.node_id LIMIT ?
    """, (*node_ids, *((task_id,) if task_id else ()), json.dumps(list(exclude)), limit)).fetchall()


def records(conn, project, ids):
    if not ids:
        return {}
    markers = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT * FROM events WHERE id IN ({markers})", tuple(ids)).fetchall()
    references = episodic._evidence_by_event(conn, rows, limit_per_event=MAX_EVIDENCE)
    transcript_ids = {item["message_id"] for refs in references.values() for item in refs if item["type"] == "transcript"}
    present = {row[0] for row in conn.execute(
        "SELECT id FROM transcript_messages WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(transcript_ids)),),
    )} if transcript_ids else set()
    superseded = episodic.superseded_map(conn)
    git_keys = [
        (item["commit"], item["path"]) for refs in references.values() for item in refs
        if item["type"] == "git"
    ]
    git_keys.extend((row["commit_sha"], row["file"].replace("\\", "/")) for row in rows if row["commit_sha"] and row["file"])
    objects = evidence.git_objects(project, git_keys)
    freshness = {key: evidence.freshness(project, key[1], value) for key, value in objects.items()}
    result = {}
    for row in sorted(rows, key=lambda item: -item["id"]):
        payload = json.loads(row["payload"])
        links = references.get(row["id"], [])
        states = []
        for link in links:
            if link["type"] == "git":
                key = (link["commit"], link["path"])
                link["freshness"] = freshness.get(key, "unknown")
                link["availability"] = "available" if objects.get(key, (None,))[0] else "missing"
                states.append(link["freshness"])
            else:
                link["freshness"] = "unknown"
                link["availability"] = "available" if link["message_id"] in present else "missing"
        if row["commit_sha"] and row["file"]:
            key = (row["commit_sha"], row["file"].replace("\\", "/"))
            state = freshness.get(key, "unknown")
            states.append(state)
            if row["source"] == "git" and objects.get(key, (None,))[0] and not any(
                link["type"] == "git" and (link["commit"], link["path"]) == key for link in links
            ):
                links.append({"type": "git", "commit": key[0], "path": key[1], "freshness": state,
                              "reference": None, "availability": "available"})
        result[row["id"]] = {
            "id": row["id"], "type": row["type"], "relationship": _link(row["type"], payload),
            "status": payload.get("status") if payload.get("status") in episodic.STATUSES else "recorded",
            "freshness": "stale" if "stale" in states else "current" if states and set(states) == {"current"} else "unknown",
            "supersededBy": superseded.get(row["id"]), "summary": _title(payload),
            "evidence": links, "task": row["task_id"],
        }
    return result


def neighborhood(conn, project, node_id):
    rows = candidates(conn, [node_id])
    return [{key: value for key, value in item.items() if key != "summary"}
            for item in records(conn, project, [row["id"] for row in rows]).values()]


def get(conn, project, event_id):
    if type(event_id) is not int or event_id < 1:
        raise ValueError("use a positive TRACE event ID")
    result = records(conn, project, [event_id]).get(event_id)
    if result is None:
        raise ValueError("event is unavailable in this project")
    return result


def export(conn, project):
    """Opt-in derived history. Each selected event has one metadata record."""
    rows = conn.execute("""
        WITH selected AS (
            SELECT e.id FROM events e JOIN graph_nodes n ON n.kind='file' AND n.path=replace(e.file, char(92), '/')
            WHERE e.source!='capture' OR json_extract(e.payload, '$.kind')='test_outcome'
            UNION
            SELECT l.event_id FROM event_evidence l JOIN graph_nodes n ON n.kind='file' AND n.path=l.git_path
        ) SELECT id FROM selected ORDER BY id DESC LIMIT ?
    """, (MAX_EXPORT_EVENTS,)).fetchall()
    ids = [row["id"] for row in rows]
    if not ids:
        return {}, {}
    markers = ",".join("?" for _ in ids)
    nodes = defaultdict(list)
    for row in conn.execute(f"""
        WITH paths AS (
            SELECT e.id, replace(e.file, char(92), '/') AS path FROM events e WHERE id IN ({markers})
            UNION SELECT event_id, git_path FROM event_evidence WHERE git_path IS NOT NULL AND event_id IN ({markers})
        )
        SELECT DISTINCT n.id, p.id AS event_id FROM paths p JOIN graph_nodes n ON n.path=p.path
        JOIN events e ON e.id=p.id
        WHERE n.kind IN ('file','module') OR (n.line IS NOT NULL AND replace(e.file, char(92), '/')=n.path AND
            (n.qualified_name=e.symbol OR (n.name=e.symbol AND (SELECT count(*) FROM graph_nodes same
               WHERE same.path=n.path AND same.name=n.name AND same.line IS NOT NULL)=1)))
        ORDER BY p.id DESC
    """, (*ids, *ids)):
        nodes[row["id"]].append(row["event_id"])
    data = records(conn, project, ids)
    for item in data.values():
        item.pop("summary")
    return dict(nodes), data


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='graph_nodes'").fetchone() is not None
