"""Shared constants for database strategy classification."""

# Strategies that perform superuser PostgreSQL operations (DROP/CREATE DATABASE,
# role management) and therefore require an admin_url connecting as a role with
# sufficient privileges. Strategies not in this set only need database_url.
ADMIN_STRATEGIES: frozenset[str] = frozenset({"rebuild", "restore_migrate"})
