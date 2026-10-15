"""One-shot renderer kept for tests and tooling: prints the jobs frame and writes the click map to $STEWARD_DASH_MAP.
The code lives in steward/dashrender.py; the resident pane (steward/dashapp.py) uses it directly."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", ".."))
from steward.dashrender import main  # noqa: E402

sys.exit(main())
