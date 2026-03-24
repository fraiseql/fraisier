"""External database guard.

Fraises with ``external_db: true`` should be skipped for all database
management operations (reset, migrate, build, backup).
"""

from typing import Any, overload


def is_external_db(fraise_config: dict[str, Any]) -> bool:
    """Return True if the fraise uses an externally managed database."""
    return bool(fraise_config.get("external_db", False))


@overload
def filter_db_fraises(
    fraises: dict[str, dict[str, Any]],
    *,
    return_skipped: bool = False,
) -> dict[str, dict[str, Any]]: ...


@overload
def filter_db_fraises(
    fraises: dict[str, dict[str, Any]],
    *,
    return_skipped: bool = True,
) -> tuple[dict[str, dict[str, Any]], list[str]]: ...


def filter_db_fraises(
    fraises: dict[str, dict[str, Any]],
    *,
    return_skipped: bool = False,
) -> dict[str, dict[str, Any]] | tuple[dict[str, dict[str, Any]], list[str]]:
    """Filter out fraises with ``external_db: true``.

    If *return_skipped* is True, also returns the list of skipped names.
    """
    included: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []

    for name, config in fraises.items():
        if is_external_db(config):
            skipped.append(name)
        else:
            included[name] = config

    if return_skipped:
        return included, skipped
    return included
