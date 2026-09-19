# Accept only freshly issued tokens

`validate_token()` in the token module is accepting strings it should
reject. Any string ending in the issued suffix currently verifies, even
when it was never issued for a real user.

Change the code so that:

- tokens freshly issued for a user verify,
- strings that were never issued do not verify,
- empty and missing values do not verify.

You are done when validation agrees with current issuance on all inputs.
Do not just claim the fix works — the benchmark checks the behavior
mechanically.
