# Fix paginated listing

`paginate()` in `store/paging.py` returns the wrong rows: pages are
1-based, but requesting page 1 skips the first page worth of rows, and
every later page is shifted the same way.

Change the code so that:

- page 1 returns the first `page_size` rows,
- page 2 returns the next `page_size` rows,
- an explicit `page_size` is honored and the default still follows the
  configured page size.

You are done when each page holds exactly its own rows. Do not just
claim the fix works — the benchmark checks the behavior mechanically.
