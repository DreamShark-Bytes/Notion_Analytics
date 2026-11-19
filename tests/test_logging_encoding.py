import ast
import io
import logging
from pathlib import Path

import pytest

from sync import _log_handlers

ROOT = Path(__file__).parent.parent
MESSAGE = "[tasks] Page p1: live → trashed \U0001F600"


def test_cp1252_cannot_encode_the_arrow():
    # the premise of the Windows bug: the default file encoding there has no arrow
    with pytest.raises(UnicodeEncodeError):
        "→".encode("cp1252")


def test_file_handler_writes_utf8(tmp_path):
    log_file = tmp_path / "run.log"
    handlers = _log_handlers(str(log_file))
    file_handler = handlers[1]
    assert file_handler.encoding == "utf-8"
    logger = logging.getLogger("test_logging_encoding.file")
    logger.propagate = False
    logger.addHandler(file_handler)
    try:
        logger.warning(MESSAGE)
    finally:
        logger.removeHandler(file_handler)
        for h in handlers:
            h.close()
    assert MESSAGE in log_file.read_text(encoding="utf-8")


def test_console_handler_survives_a_cp1252_stream(tmp_path, monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr("sys.stderr", stream)
    handlers = _log_handlers(str(tmp_path / "run.log"))
    console = handlers[0]
    failures = []
    console.handleError = lambda record: failures.append(record)
    logger = logging.getLogger("test_logging_encoding.console")
    logger.propagate = False
    logger.addHandler(console)
    try:
        logger.warning(MESSAGE)
        stream.flush()
    finally:
        logger.removeHandler(console)
        for h in handlers:
            h.close()
    assert failures == []
    assert b"trashed" in raw.getvalue()


def _log_call_strings(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"logger", "log"}
            and node.func.attr in {"debug", "info", "warning", "error", "exception", "critical"}
        ):
            for arg in node.args:
                for sub in ast.walk(arg):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        yield node.lineno, sub.value


@pytest.mark.parametrize("name", ["sync.py", "storage.py", "dbt/scripts/export_parquet.py"])
def test_log_messages_are_plain_ascii_safe(name):
    """Log text stays encodable in cp1252, so a handler with the Windows default cannot fail on it."""
    bad = []
    for lineno, text in _log_call_strings(ROOT / name):
        try:
            text.encode("cp1252")
        except UnicodeEncodeError:
            bad.append((lineno, text))
    assert not bad, f"non-cp1252 characters in log messages: {bad}"
