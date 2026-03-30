"""Schema hash tracking for smart-cache template rebuilds.

Computes a SHA-256 hash over sorted SQL migration files so that
template rebuilds can be skipped when the schema hasn't changed.

.. deprecated::
    Use ``confiture.core.builder.SchemaBuilder.compute_hash()`` instead.
    It handles ``superuser_dirs`` exclusion and uses Rust acceleration.
"""

import hashlib
import warnings
from dataclasses import dataclass
from pathlib import Path


def hash_schema(schema_dir: Path) -> str:
    """Compute SHA-256 hash of all ``*.sql`` files in *schema_dir*.

    Files are sorted by name to ensure deterministic ordering.

    .. deprecated::
        Use ``confiture.core.builder.SchemaBuilder.compute_hash()``
        instead, which excludes deployment-only config and is
        significantly faster via the Rust extension.
    """
    warnings.warn(
        "hash_schema() is deprecated. "
        "Use confiture.core.builder.SchemaBuilder.compute_hash() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    h = hashlib.sha256()
    for sql_file in sorted(schema_dir.glob("*.sql")):
        h.update(sql_file.name.encode())
        h.update(sql_file.read_bytes())
    return h.hexdigest()


@dataclass
class SchemaComparison:
    """Result of comparing current schema hash with stored template hash."""

    needs_rebuild: bool
    current_hash: str
    stored_hash: str
    hash_file: Path

    def save(self) -> None:
        """Persist the current hash to the hash file."""
        self.hash_file.write_text(self.current_hash)


def compare_with_template(
    schema_dir: Path,
    hash_file: Path,
) -> SchemaComparison:
    """Compare current schema hash with stored template hash.

    Returns a ``SchemaComparison`` with ``needs_rebuild=True`` if the
    schema has changed since the last template was created (or if no
    stored hash exists).
    """
    current = hash_schema(schema_dir)

    stored = ""
    if hash_file.exists():
        stored = hash_file.read_text().strip()

    return SchemaComparison(
        needs_rebuild=current != stored,
        current_hash=current,
        stored_hash=stored,
        hash_file=hash_file,
    )
