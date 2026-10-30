"""Shared constants for database strategy classification.

Imports nothing from fraisier: config validation reads these, and
``fraisier.config`` must not import a module that imports it back.
"""

from typing import Literal

# Strategies that perform superuser PostgreSQL operations (DROP/CREATE DATABASE,
# role management) and therefore require an admin_url connecting as a role with
# sufficient privileges. Strategies not in this set only need database_url.
ADMIN_STRATEGIES: frozenset[str] = frozenset({"rebuild", "restore_migrate"})

TemplateMode = Literal["auto", "always", "never"]

#: The values ``restore.template`` accepts.
TEMPLATE_MODES: tuple[TemplateMode, ...] = ("auto", "always", "never")
