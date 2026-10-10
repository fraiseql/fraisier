"""The ``confiture`` executable fraisier runs.

Hosts install fraisier with ``uv tool install``, which exposes fraisier's own
entry points and nothing else: the ``confiture`` fraisier pins sits next to the
tool's interpreter, not on PATH. Resolving it there runs the version fraisier
depends on and audited, whatever PATH holds.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def confiture_executable() -> str:
    """The ``confiture`` beside the running interpreter, else PATH's.

    ``sys.executable`` is deliberately not resolved: a venv's ``python`` is a
    symlink to the base interpreter, and only its unresolved parent is the
    venv's ``bin/``.
    """
    beside = Path(sys.executable).parent / "confiture"
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    log.warning(
        "no confiture beside %s; running the confiture on PATH, whose version "
        "fraisier does not pin",
        sys.executable,
    )
    return "confiture"
