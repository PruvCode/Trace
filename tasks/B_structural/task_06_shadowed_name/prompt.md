# Strip markup from display names

Profile display names are rendered from raw user input, and markup is
reaching the screen: a name like `"<b>ann</b>"` displays with its tags
instead of as plain text. The project has a sanitization policy for
user-supplied text that the rendering path is not honoring.

Change the code so that:

- markup is stripped from display names,
- surrounding whitespace is trimmed,
- plain names render unchanged.

You are done when display names honor the project's sanitization
policy. Do not just claim the fix works — the benchmark checks the
behavior mechanically.
