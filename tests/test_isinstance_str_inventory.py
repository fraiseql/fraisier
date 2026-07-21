"""Inventory guard for ``isinstance(x, str)`` sites (#220).

The audit catalogues every ``isinstance(_, str)`` site in ``fraisier/``
and documents each one's status (LazyEnv-aware / not-eligible /
non-config / after-LazyEnv) in the inventory at the bottom of
``fraisier/config/_lazy_env.py``.

This test pins the COUNT of those sites. If a contributor adds a new
``isinstance(x, str)`` call, the count goes up and the test fails —
forcing them to either widen via ``is_string_like`` (the right call
for a config-derived value) or update the inventory with the new
row's justification.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Locked inventory size — bump this only after auditing the new site
# AND adding a row to the inventory comment in
# fraisier/config/_lazy_env.py. Excludes _lazy_env.py itself because
# that file contains the inventory comment (whose row text would also
# match the regex), plus LazyEnv's own ``__eq__`` peer-check which is
# internal-by-design.
EXPECTED_INSTANCE_STR_COUNT = 18


def test_isinstance_str_count_locked():
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [
            "git",
            "grep",
            "-nE",
            r"isinstance\([^,]+,\s*str\)",
            "fraisier/",
            ":(exclude)fraisier/config/_lazy_env.py",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    # Skip blank lines / non-matching trailing content.
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == EXPECTED_INSTANCE_STR_COUNT, (
        f"Found {len(lines)} `isinstance(_, str)` sites in fraisier/, "
        f"expected {EXPECTED_INSTANCE_STR_COUNT}. "
        "If you added a NEW site, audit it: a config-derived value "
        "should use `is_string_like` (from fraisier.config._lazy_env). "
        "Then update the inventory comment in "
        "fraisier/config/_lazy_env.py and bump EXPECTED_INSTANCE_STR_COUNT."
        "\n\nCurrent sites:\n" + "\n".join(lines)
    )


def test_inventory_comment_present():
    # Quick smoke check that the inventory comment block didn't get
    # silently deleted by a "tidy unused imports" sweep.
    src = (
        Path(__file__).resolve().parent.parent / "fraisier" / "config" / "_lazy_env.py"
    ).read_text()
    assert "Inventory of remaining ``isinstance(x, str)`` sites" in src
    # The header row of the table must be present.
    assert re.search(r"File:Line\s+Status\s+Reason", src), (
        "Inventory table header missing in _lazy_env.py"
    )
