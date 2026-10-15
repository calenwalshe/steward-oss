"""The one place the npm package name is defined (Phase 19, G2).

PACKAGE_SCOPE is a PLACEHOLDER on purpose. The real scope is the npm user or organisation of whoever publishes, and
that is a later, explicit publish decision (G19): until then nothing is published and `package.json` is `private`.
Replace the scope here, in this one file, when that decision is made; the build stamps it into package.json and
steward/_version.py, and the tests read it from here.
"""

PACKAGE_SCOPE = "@steward-local"
PACKAGE_NAME = "steward"
FULL_NAME = f"{PACKAGE_SCOPE}/{PACKAGE_NAME}"
