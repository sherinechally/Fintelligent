"""Make `src/` importable for tests without requiring an editable install.

The package installs fine, but a reviewer who clones the repo and runs
pytest before `pip install -e .` should get passing tests rather than an
ImportError — especially for the redaction tests, which are the ones
somebody is most likely to want to run before trusting the safety claims.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
