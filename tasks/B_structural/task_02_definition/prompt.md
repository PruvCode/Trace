# Sessions expire after 30 minutes instead of the 1-hour policy

Users report being logged out after about 30 minutes, but the documented
service policy is a 1-hour session lifetime. The time-to-live value is
defined in more than one place in the codebase, and the definitions
disagree with each other.

Change the code so that:

- a session younger than one hour is considered valid,
- a session one hour old or older is considered expired,
- there is a single canonical source for the lifetime value.

You are done when session expiry follows the documented 1-hour policy. Do
not just claim the fix works — the benchmark checks the behavior
mechanically.
