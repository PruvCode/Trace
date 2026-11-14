"""Evidence-aware ranking signals for project-local context."""

import hashlib
import re

from memory import entity_history, redact, source_files

IDENTIFIERS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}(?:[./\\-][A-Za-z0-9_-]+)*")
CATEGORIES = (
    ("debugging", r"\b(debug|bug|fix|failure|failed|error|investigat\w*)\b"),
    ("implementation", r"\b(feature|implement\w*|add\w*)\b"),
    ("architecture", r"\b(architecture|design|adr|decision)\b"),
    ("refactoring", r"\b(refactor\w*|restructur\w*)\b"),
    ("verification", r"\b(test\w*|pytest|verif\w*|regression)\b"),
    ("configuration", r"\b(config\w*|dependenc\w*|version|upgrade)\b"),
    ("performance", r"\b(latency|performance|slow|throughput|profil\w*)\b"),
)


def classify(event, text):
    value = f"{event.type} {text}".lower()
    categories = [name for name, pattern in CATEGORIES if re.search(pattern, value)]
    if event.payload.get("kind") == "test_outcome" and "verification" not in categories:
        categories.append("verification")
    return categories


def graph_matches(conn, query, task_id, *, unassigned_only=False, exclude=()):
    if not query or not entity_history.available(conn):
        return {}
    terms = list(dict.fromkeys(term.replace("\\", "/").rstrip(".") for term in IDENTIFIERS.findall(query[:2000])))[:24]
    if not terms:
        return {}
    markers = ",".join("?" for _ in terms)
    rows = conn.execute(f"""
        SELECT id, path, name, qualified_name FROM graph_nodes
        WHERE (kind='file' AND path IN ({markers})) OR
            (line IS NOT NULL AND qualified_name IN ({markers})) OR
            (line IS NOT NULL AND name IN ({markers}) AND
               (SELECT count(*) FROM graph_nodes g WHERE g.name=graph_nodes.name AND g.line IS NOT NULL)=1)
        ORDER BY path, line LIMIT 24
    """, (*terms, *terms, *terms)).fetchall()
    by_id = {row["id"]: row for row in rows}
    links = entity_history.candidates(conn, list(by_id), task_id, unassigned_only=unassigned_only, exclude=exclude)
    result = {}
    focused_paths = {by_id[row["node_id"]]["path"] for row in links if row["definition_match"]}
    for row in links:
        node = by_id[row["node_id"]]
        if not row["definition_match"] and node["path"] in focused_paths:
            continue
        result.setdefault(row["id"], {"path": node["path"], "name": node["name"]})
    return result


def decision_targets(conn, project, text, file, symbol, verified_files):
    """Locate definitions named by a decision in the current source snapshot."""
    if project is None or not entity_history.available(conn):
        return []
    terms = list(dict.fromkeys(IDENTIFIERS.findall(text[:500])))[:32]
    if not terms:
        return []
    markers = ",".join("?" for _ in terms)
    rows = conn.execute(f"""
        SELECT n.name, n.qualified_name, n.path, n.line, f.source_hash
        FROM graph_nodes n JOIN structural_fragments f ON n.path=f.file
        WHERE n.line IS NOT NULL AND ((n.qualified_name IN ({markers}) AND
            (SELECT count(*) FROM graph_nodes p WHERE p.qualified_name=n.qualified_name AND p.line IS NOT NULL)=1) OR
            (n.name IN ({markers}) AND ((n.path=? AND n.name=? AND
                (SELECT count(*) FROM graph_nodes p WHERE p.path=n.path AND p.name=n.name AND p.line IS NOT NULL)=1)
             OR (SELECT count(*) FROM graph_nodes p WHERE p.name=n.name AND p.line IS NOT NULL)=1)))
        ORDER BY n.path, n.line LIMIT 16
    """, (*terms, *terms, (file or "").replace("\\", "/"), symbol)).fetchall()
    targets = []
    positions = {name: index for index, name in enumerate(terms)}
    for row in sorted(rows, key=lambda row: min(positions.get(row[key], len(terms)) for key in ("name", "qualified_name"))):
        path, name = row["path"], row["qualified_name"]
        if len(path) > 180 or len(name) > 180 or redact.redact_text(path) != path or redact.redact_text(name) != name:
            continue
        if path not in verified_files:
            try:
                source = source_files.read_source(project, path)
            except OSError:
                source = None
            verified_files[path] = hashlib.sha256(source).hexdigest() if source is not None else None
        if verified_files[path] == row["source_hash"]:
            targets.append({"path": path, "line": row["line"], "symbol": row["name"], "qualified": name})
        if len(targets) == 4:
            break
    return targets


def target_requirements(text, targets):
    """Group explicitly named clauses while keeping the full decision authoritative."""
    names = [target["symbol"] for target in targets]
    groups = {name: [] for name in names if names.count(name) == 1}
    owner = None
    # shortcut: group sentence-leading symbol names only, until decisions have structured rule links.
    for sentence in re.split(r"(?<=\.)\s+", text.strip()):
        if not sentence:
            continue
        opening = sentence.split(maxsplit=1)[0].split("(", 1)[0].strip("`:")
        if opening in groups:
            owner = opening
        if owner:
            groups[owner].append(sentence)
    return {name: " ".join(rules) for name, rules in groups.items() if rules}


def priority(event, *, exact, matched, freshness, has_evidence):
    """Sort decisions, observed failures and supported outcomes before routine history."""
    status = event.payload.get("status")
    failure = event.payload.get("outcome") in ("failed", "failure", "rejected")
    return (
        freshness == "stale", status == "hypothesis",
        -int(exact), -int(matched), -int(status == "verified" and has_evidence),
        -int(event.type == "decision"), -int(failure), -int(has_evidence), -event.id,
    )
