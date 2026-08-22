from __future__ import annotations

import json
import logging

from unifi_netbox_sync.logging_utils import JsonLogFormatter, configure_logging


def _make_record(message: str = "hello", level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="test.logger",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_json_log_formatter_emits_valid_json_with_expected_keys():
    formatter = JsonLogFormatter()

    line = formatter.format(_make_record("something happened"))
    payload = json.loads(line)

    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["message"] == "something happened"
    assert "time" in payload


def test_json_log_formatter_includes_exc_info():
    formatter = JsonLogFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(formatter.format(record))
    assert "ValueError: boom" in payload["exc_info"]


def test_configure_logging_selects_json_formatter():
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging("INFO", "json")
        assert isinstance(root.handlers[0].formatter, JsonLogFormatter)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_configure_logging_selects_text_formatter_by_default():
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging("INFO", "text")
        assert not isinstance(root.handlers[0].formatter, JsonLogFormatter)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
