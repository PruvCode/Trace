# Align the client with the registered routes

The session client builds a path the router no longer serves: URLs it
produces disagree with the routes the service actually registers, so
client-issued session calls miss. The router's registry is the source
of truth for live paths.

Change the code so that:

- the client builds exactly the session path the router registers,
- trailing slashes on the base URL keep working,
- the router registry itself is unchanged.

You are done when client URLs agree with the registered routes. Do not
just claim the fix works — the benchmark checks the behavior
mechanically.
