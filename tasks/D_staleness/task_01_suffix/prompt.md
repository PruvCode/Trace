# Tighten token validation to the current issued format

Token validation is accepting tokens it should reject. Tighten
`validate_token` so it accepts exactly the tokens `refresh_token`
currently issues and rejects everything else.

You are done when validation agrees with current issuance on all inputs.
Do not just claim the fix works — the benchmark checks the behavior
mechanically.
