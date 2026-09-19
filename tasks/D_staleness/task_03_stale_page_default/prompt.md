# Restore the configured feed default

The feed helper returns fewer rows by default than the service is
configured for: the default limit disagrees with the configured page
size, while explicit limits work. The configured default lives in the
service configuration.

Change the code so that:

- an explicit limit is honored exactly,
- the default follows the configured page size,
- nothing else about the feed changes.

You are done when the default limit agrees with the configuration. Do
not just claim the fix works — the benchmark checks the behavior
mechanically.
