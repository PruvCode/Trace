"""Staged user-table backfill (follow-up migration)."""

MIGRATION_ID = "bf-2026-04"


def run_backfill(rows, ledger):
    """Archive unprocessed rows, recording one ledger entry each."""
    for row in rows:
        if row.get("_migrated_by") == MIGRATION_ID:
            continue
        row["archived"] = True
        row["_migrated_by"] = MIGRATION_ID
        ledger.append({"row": row["id"], "by": MIGRATION_ID})
