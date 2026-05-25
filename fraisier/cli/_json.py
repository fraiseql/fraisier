"""LazyEnv-aware JSON serialization for CLI diagnostic output (#220 Phase 5).

CLI commands serialize state with ``json.dumps`` for ``--json`` flags
and structured logging. Diagnostic output should NEVER resolve a
``LazyEnv`` placeholder — secrets must not leak into stdout, log files,
or pipelines. This helper substitutes ``"<envvar:NAME>"`` placeholders
for reachable ``LazyEnv`` instances at serialization time, without
calling ``resolve()``.

Use :func:`dumps` as a drop-in for ``json.dumps`` at every CLI JSON
output site. Non-LazyEnv non-serializable values still ``TypeError``
so silent data loss can't happen.
"""

from __future__ import annotations

import json
from typing import Any

from fraisier.config._lazy_env import LazyEnv


def _default(obj: Any) -> Any:
    if isinstance(obj, LazyEnv):
        return f"<envvar:{obj.name}>"
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON-serializable")


def dumps(obj: Any, *, indent: int | None = None) -> str:
    """Serialize *obj* to JSON, rendering ``LazyEnv`` as a placeholder.

    The placeholder is ``"<envvar:NAME>"`` and is generated without
    calling ``LazyEnv.resolve()`` — diagnostic JSON cannot leak the
    secret even when the env var is set.
    """
    return json.dumps(obj, indent=indent, default=_default)
