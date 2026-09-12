# Support the new token format in validation

Token validation must handle the upcoming format rollout. Update
`validate_token` so both current and new-format tokens verify, without
loosening rejection of anything else.

You are done when `validate_token` accepts current tokens and new-format
tokens, and still rejects everything else. Do not just claim the fix
works — the benchmark checks the behavior mechanically.
