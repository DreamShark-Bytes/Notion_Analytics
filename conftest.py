import sys
from pathlib import Path
from unittest.mock import MagicMock

# Make project root and tools/ importable from any test file
_root = Path(__file__).parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "tools"))

# Stub notion_api so tests that target pure functions (sanitize_col,
# detect_changes, _expand_change_fields, etc.) don't require the package.
# Tests that exercise actual Notion API calls must use the venv.
_mock = MagicMock()
_mock.normalize_property = MagicMock(return_value=None)
_mock.extract_content = MagicMock(return_value="")
_mock.extract_comments = MagicMock(return_value=[])
_mock.NotionClient = MagicMock()
sys.modules.setdefault("notion_api", _mock)
