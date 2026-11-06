# Console typefaces

The console serves these bundled fonts locally through `/assets/fonts`.
It does not load Google Fonts from the customer's browser.

- Roboto Flex: https://github.com/google/fonts/tree/main/ofl/robotoflex
- Google Sans Code: https://github.com/google/fonts/tree/main/ofl/googlesanscode

The adjacent SIL Open Font License files apply to their respective typefaces.
The WOFF files retain the 400–500 weight axis. Other variation axes use their
original defaults. Glyphs include Latin/Latin Extended, general punctuation,
and the interface's arrows/check marks; system fallbacks cover other scripts.
They were subset with fontTools as a build-time authoring step. fontTools is
not required by the application at backend.
