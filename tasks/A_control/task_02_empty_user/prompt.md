# Reject empty user IDs at login

`login_and_refresh()` in `auth/login.py` currently accepts any `user_id` as
long as the password is correct — including empty strings and `None`. That
means sessions can be created for users that do not exist.

Change the code so that:

- calling `login_and_refresh("", "s3cret")` raises `ValueError`,
- calling `login_and_refresh(None, "s3cret")` raises `ValueError`,
- valid logins keep working exactly as before (correct password returns a
  fresh token; wrong password still raises `ValueError`).

You are done when the login behavior above holds. Do not just claim the fix
works — the benchmark checks the behavior mechanically.
