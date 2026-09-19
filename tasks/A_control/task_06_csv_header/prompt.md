# Keep the CSV header out of the data

`load_rows()` in `ingest/csv_loader.py` treats the header row as data:
loading a file with a header returns one row too many, and the first
"record" is the column names.

Change the code so that:

- with the default `has_header=True`, the header row is skipped,
- with `has_header=False`, every row is returned as data,
- data rows are otherwise returned unchanged.

You are done when the header is handled according to the flag. Do not
just claim the fix works — the benchmark checks the behavior
mechanically.
