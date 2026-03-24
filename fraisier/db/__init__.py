"""Multi-database adapter layer for Fraisier.

Provides a unified interface for working with different database backends:
- SQLite (development, testing)
- PostgreSQL (production)
- MySQL (alternative production)

Follows trait-based abstraction pattern from FraiseQL.
"""

from .adapter import (
    DatabaseType,
    FraiserDatabaseAdapter,
    PoolMetrics,
)
from .factory import create_adapter_from_url, get_database_adapter
from .migrations import (
    MigrationError,
    MigrationRunner,
    get_migration_runner,
    run_migrations,
)
from .observability import (
    DatabaseObservability,
    DeploymentDatabaseAudit,
    get_audit_logger,
    get_database_observability,
)

__all__ = [
    "DatabaseObservability",
    "DatabaseType",
    "DeploymentDatabaseAudit",
    "FraiserDatabaseAdapter",
    "MigrationError",
    "MigrationRunner",
    "PoolMetrics",
    "create_adapter_from_url",
    "get_audit_logger",
    "get_database_adapter",
    "get_database_observability",
    "get_migration_runner",
    "run_migrations",
]
