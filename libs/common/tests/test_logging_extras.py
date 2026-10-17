"""Guard against reserved LogRecord keys inside logging ``extra=`` payloads.

Python's logging raises ``KeyError`` the moment a record is emitted if the
``extra`` dict collides with a LogRecord attribute (``created``, ``name``,
``module``, ...). Tests run services at WARNING level, so an offending
``logger.info`` call can stay hidden until INFO logging is enabled in
deployment — therefore scan the source instead of waiting for a log call.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # libs/common/tests -> repository root

# Exactly the attributes Python sets on every LogRecord instance.
RESERVED = frozenset(vars(logging.LogRecord(name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None)))


def _source_files() -> Iterator[Path]:
    roots = [ROOT / "libs" / "common" / "src", *sorted((ROOT / "services").glob("*/src"))]
    for root in roots:
        yield from root.rglob("*.py")


def _extras_collisions(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    collisions: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                continue
            for key in keyword.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value in RESERVED:
                    collisions.append(f"{path.relative_to(ROOT)}:{node.lineno}: extra key {key.value!r}")
    return collisions


def test_no_reserved_keys_in_logging_extras() -> None:
    offenders = [collision for path in _source_files() for collision in _extras_collisions(path)]
    assert not offenders, "logging `extra=` uses reserved LogRecord attributes (raises KeyError when emitted):\n" + "\n".join(offenders)
