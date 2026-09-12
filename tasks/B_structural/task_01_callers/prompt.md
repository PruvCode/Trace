# Renewal accepts tokens the shared validation rejects

Session renewal is letting malformed tokens through. For example, the short
token `"tok_x"` passes `renew_session`, even though the shared token
validation used everywhere else in the service rejects it.

Change the code so that:

- `renew_session` accepts exactly the tokens the shared validation accepts,
- `renew_session` rejects everything the shared validation rejects,
- currently valid renewals keep working.

You are done when renewal behavior agrees with the shared validation on all
inputs. Do not just claim the fix works — the benchmark checks the behavior
mechanically.
