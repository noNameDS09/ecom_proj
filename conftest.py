"""Root conftest: makes the monorepo importable without installing anything.

The services are plain Python packages that live under
``services/<name>/src``; ``libs/common/src`` holds the shared runtime library.
Adding them to ``sys.path`` keeps local development, Docker images and the test
suite on exactly the same import layout.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC_ROOTS = [ROOT, ROOT / "libs" / "common" / "src", ROOT / "tests"]
SRC_ROOTS += sorted(path / "src" for path in (ROOT / "services").glob("*/") if (path / "src").is_dir())

for path in SRC_ROOTS:
    entry = str(path)
    if entry not in sys.path:
        sys.path.insert(0, entry)

# Keep integration tests from picking up a developer's .env by accident.
os.environ.setdefault("ENVIRONMENT", "test")
