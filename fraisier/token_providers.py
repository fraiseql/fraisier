"""Pluggable token providers for authenticated smoke tests (#215).

The deploy pipeline runs smoke tests against the freshly-deployed service
with bearer credentials sourced from the operator's environment. Static
``!envvar`` references (v0.21+) cover the common case where a long-lived
JWT lives in a secrets manager and is exported to the deploy user. They
do not cover providers that hand out short-lived tokens — OIDC machine
clients, vault-issued JWTs, federated assume-role workflows — where the
token must be acquired *at deploy time* and used within seconds.

A ``token_provider:`` block on a smoke test declares how to acquire the
token. At deploy time, each distinct provider is resolved exactly once
(cached by object identity inside ``_run_smoke_tests_or_halt``) and the
resulting value is interpolated into the configured ``header`` using
``format``. Absence of the block keeps today's behavior — the smoke
test's static ``headers`` flow through unchanged.

Parsing is purely structural: ``parse_token_provider`` never shells out
or makes network calls, so ``fraisier validate`` does not trigger any
side effects from a provider configuration.
"""

from __future__ import annotations

from dataclasses import dataclass

from fraisier.errors import ConfigurationError

_VALID_PROVIDER_TYPES: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TokenProvider:
    """One ``token_provider:`` configuration block.

    Provider-specific fields are added in later phases. Sequence fields
    are typed as tuples to keep instances genuinely immutable.
    """

    type: str
    header: str = "Authorization"
    format: str = "Bearer {token}"


def parse_token_provider(raw: dict) -> TokenProvider:
    """Parse a ``token_provider:`` mapping into a ``TokenProvider``.

    Pure parse — no subprocesses, no network. Unknown or missing
    ``type`` raises ``ConfigurationError`` naming the currently-valid
    provider types.
    """
    if "type" not in raw:
        raise ConfigurationError(
            "token_provider is missing required 'type' field; "
            f"valid types: {sorted(_VALID_PROVIDER_TYPES)!r}"
        )
    provider_type = raw["type"]
    if provider_type not in _VALID_PROVIDER_TYPES:
        raise ConfigurationError(
            f"token_provider.type must be one of "
            f"{sorted(_VALID_PROVIDER_TYPES)!r}, got {provider_type!r}"
        )
    return TokenProvider(
        type=provider_type,
        header=raw.get("header", "Authorization"),
        format=raw.get("format", "Bearer {token}"),
    )
