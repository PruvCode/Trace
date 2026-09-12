# Reject empty user IDs without breaking renewal

Logins for empty user IDs must be rejected, while renewal keeps working for
every current caller, including automated flows that renew on behalf of
other accounts.

Note: a previous fix attempt in this area was reverted.

Change the code so that:

- calling `login_and_refresh("", "s3cret")` raises `ValueError`,
- calling `login_and_refresh(None, "s3cret")` raises `ValueError`,
- valid logins keep working exactly as before,
- `renew_session` keeps renewing for every caller it serves today.

You are done when the behavior above holds. Do not just claim the fix
works — the benchmark checks the behavior mechanically.
