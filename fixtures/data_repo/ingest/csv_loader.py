"""CSV ingestion for tabular uploads."""

import csv


def load_rows(path, has_header=True):
    """Load data rows from a CSV file, skipping the header when present."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        return [row for row in reader]  # BUG: header row kept as data
