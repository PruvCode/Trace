# Renewal through the request handler disagrees with the service

Renewing a session through the request handler is failing for inputs the
underlying session service accepts. Calling the service directly with a
user and a valid token works, but the same inputs through `handle_renew`
raise or return the wrong result.

Change the code so that:

- `handle_renew` accepts exactly the inputs `services.renew_session`
  accepts,
- `handle_renew` returns exactly what `services.renew_session` returns,
- currently valid renewals keep working through both entry points.

You are done when the handler agrees with the service on all inputs. Do
not just claim the fix works — the benchmark checks the behavior
mechanically.
