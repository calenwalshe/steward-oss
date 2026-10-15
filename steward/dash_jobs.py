"""The one-shot jobs renderer the packaged status pane runs: prints the jobs frame and writes the click map to
$STEWARD_DASH_MAP. The code lives in steward/dashrender.py; the resident pane (steward/dashapp.py) uses it directly."""
import sys

from steward.dashrender import main

if __name__ == "__main__":
    sys.exit(main())
