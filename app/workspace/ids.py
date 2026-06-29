from __future__ import annotations

import re

WORKSPACE_ID_PATTERN = r"^ws_[A-Za-z0-9_-]+$"
WORKSPACE_ID_RE = re.compile(WORKSPACE_ID_PATTERN)
