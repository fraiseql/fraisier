"""CLI interface for Fraisier deployment system.

Commands:
    fraisier list                           # List all fraises
    fraisier deploy <fraise> <environment>  # Deploy a fraise
    fraisier status <fraise> <environment>  # Check fraise status
    fraisier providers                      # List available providers
    fraisier provider-info <type>           # Show provider details
    fraisier provider-test <type>           # Test provider pre-flight
"""

from .main import main

__all__ = ["main"]
