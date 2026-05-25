"""Eager-load helper for tests asserting the pre-lazy fail-fast contract.

``FraisierConfig.__init__`` only runs the cheap Stage-1 checks. Tests that
relied on the old "all errors surface at load time" behaviour can use
``eager_load(path)`` to build the config AND force-traverse every Stage-2
section so any error is raised — just like ``fraisier validate`` does
through ``_collect_all_validation_errors``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fraisier.config import FraisierConfig

if TYPE_CHECKING:
    from os import PathLike


def eager_load(path: str | PathLike[str]) -> FraisierConfig:
    """Build FraisierConfig and trigger every lazy section.

    Raises the first Stage-2 ``ValidationError`` /
    ``ConfigurationError`` encountered, matching the eager-fail
    behaviour the loader had before the Stage 1/2 split.
    """
    config = FraisierConfig(path)
    _ = config.notifications
    _ = config.hooks
    for name in config.list_fraises():
        for env_name in config.list_environments(name):
            config.get_fraise_environment(name, env_name)
    return config
