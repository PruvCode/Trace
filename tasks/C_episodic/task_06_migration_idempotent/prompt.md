# Finish the backfill without reprocessing migrated rows

A staged follow-up migration archives unprocessed user rows, but it
reprocesses rows a previous backfill already migrated: running it
against a table with already-migrated rows duplicates ledger entries
and restamps them under a new identity. Rows the earlier backfill
completed belong to the original migration, not to a new one.

Change the code so that:

- already-migrated rows are left untouched,
- each unprocessed row is archived exactly once with one ledger entry,
- running the operation repeatedly keeps state and counts stable,
- remaining rows are recorded under the original migration identity.

You are done when repeated runs converge instead of duplicating work.
Do not just claim the fix works — the benchmark checks the behavior
mechanically.
