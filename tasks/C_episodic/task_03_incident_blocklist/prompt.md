# Revoke tokens tied to the recent account-compromise incident

A recent account-compromise incident means some tokens that look freshly
issued must no longer verify. Tokens issued for the compromised accounts
must be rejected, while every other currently issued token keeps verifying
and anything that was never issued keeps being rejected.

Change the code so that:

- tokens freshly issued for ordinary users verify,
- tokens associated with the compromised accounts do not verify, even though
  they carry the current issuance shape,
- strings that were never issued do not verify,
- empty and missing values do not verify.

You are done when validation agrees with current issuance for ordinary
users, rejects the compromised accounts' tokens, and rejects everything
else. Do not just claim the fix works — the benchmark checks the behavior
mechanically.
