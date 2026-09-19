# Stop returning other accounts' cached data

User lookups are returning the wrong account: looking up two different
accounts that share a display name returns the first account's record
for both.

Note: a previous caching attempt in this area was reverted.

Change the code so that:

- each account ID returns its own record,
- repeated lookups stay consistent,
- the cache keeps speeding up repeat lookups.

You are done when lookups never confuse accounts. Do not just claim
the fix works — the benchmark checks the behavior mechanically.
