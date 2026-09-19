# Tighten login to the current service credential

Login is accepting credentials it should reject: any non-empty password
currently yields a fresh session token. Only the current service
credential may succeed; everything else must be rejected with an error.

The current service credential is defined in the login module. Check the
current source for the live value.

Change the code so that:

- logging in with the current service credential returns a fresh token,
- logging in with any other credential raises `ValueError`,
- valid logins keep working exactly as before.

You are done when login accepts exactly the current credential. Do not
just claim the fix works — the benchmark checks the behavior mechanically.
