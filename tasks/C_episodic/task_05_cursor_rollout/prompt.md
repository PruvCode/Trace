# Restore cursor compatibility for older clients

Since the opaque-cursor rollout, clients that stored cursors from
before the rollout can no longer paginate: their saved cursors are
rejected as invalid instead of decoding to the position they point
at. New opaque cursors keep working.

Change the code so that:

- cursors issued by the current encoder decode to their position,
- cursors stored by older clients decode to their position as well,
- anything that was never a cursor in either generation is rejected.

You are done when decoding accepts both cursor generations and
rejects everything else. Do not just claim the fix works — the
benchmark checks the behavior mechanically.
