"""Fraisier - Deployment orchestrator for the FraiseQL ecosystem.

PostgreSQL applications using confiture for migrations.

A fraisier (French for strawberry plant) manages fraises (services).
Just as a strawberry plant produces strawberries, Fraisier orchestrates
the deployment of your services (fraises).

Key Concepts:
    - fraise: A deployable service (the strawberry fruit)
    - fraisier: The deployment orchestrator (the plant)
    - fraises.yaml: Configuration file listing all fraises

Usage:
    fraisier list                           # List all fraises
    fraisier deploy <fraise> <environment>  # Deploy a fraise
    fraisier status <fraise> <environment>  # Check fraise status
"""

# Prevent Python from writing __pycache__ bytecode directories. When fraisier
# runs as root (e.g. via systemd or manual CLI), root-owned __pycache__ dirs
# inside app venvs block subsequent `uv sync` calls by the install user.
# Disabling bytecode caching trades a negligible startup cost for correctness —
# fraisier is a deployment tool imported at startup, not in a hot path. (#196)
import os
import sys

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

from importlib.metadata import version  # noqa: E402

__version__ = version("fraisier")
__all__ = ["__version__"]
