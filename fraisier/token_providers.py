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

Resolution (``TokenProvider.resolve()``) is what actually fetches the
token. It is called exactly once per provider per deploy by the deploy
pipeline and may raise ``DeploymentError`` on failure — a 401 from the
IdP or a script crash means "fix the config / IdP," not "rebuild from
scratch," which is what ``DeploymentError`` cleanly signals.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field

from fraisier.errors import ConfigurationError, DeploymentError

logger = logging.getLogger(__name__)

_VALID_PROVIDER_TYPES: frozenset[str] = frozenset({"exec"})

_DEFAULT_EXEC_TIMEOUT = 10


@dataclass(frozen=True)
class TokenProvider:
    """One ``token_provider:`` configuration block.

    A single frozen dataclass carries the common fields (``type``,
    ``header``, ``format``, ``timeout``) and the type-specific fields.
    The right subset is validated at parse time; ``.resolve()``
    dispatches on ``type``. Sequence fields use tuples to keep
    instances genuinely immutable.
    """

    type: str
    header: str = "Authorization"
    format: str = "Bearer {token}"
    timeout: int = _DEFAULT_EXEC_TIMEOUT
    # exec-specific
    command: tuple[str, ...] = field(default_factory=tuple)

    def resolve(self) -> str:
        """Acquire the token from the underlying provider.

        Called exactly once per provider per deploy. Raises
        ``DeploymentError`` on failure (non-zero exit, timeout,
        unexpected exception).
        """
        if self.type == "exec":
            return _resolve_exec(self)
        # Unreachable — _VALID_PROVIDER_TYPES gates parse_token_provider,
        # which is the only constructor. New types must add a branch
        # here.
        raise DeploymentError(
            f"token_provider.type={self.type!r} has no resolver implemented"
        )


def parse_token_provider(raw: dict) -> TokenProvider:
    """Parse a ``token_provider:`` mapping into a ``TokenProvider``.

    Pure parse — no subprocesses, no network. Validates the fields
    required by the declared ``type``. Unknown or missing ``type``
    raises ``ConfigurationError`` naming the currently-valid provider
    types.
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

    header = raw.get("header", "Authorization")
    fmt = raw.get("format", "Bearer {token}")
    timeout = int(raw.get("timeout", _DEFAULT_EXEC_TIMEOUT))

    if provider_type == "exec":
        command_raw = raw.get("command")
        if not command_raw or not isinstance(command_raw, list):
            raise ConfigurationError(
                "token_provider.command must be a non-empty list of "
                f"strings for type=exec, got {command_raw!r}"
            )
        return TokenProvider(
            type=provider_type,
            header=header,
            format=fmt,
            timeout=timeout,
            command=tuple(str(arg) for arg in command_raw),
        )

    # Defensive — should not reach here given the type-gate above.
    raise ConfigurationError(  # pragma: no cover
        f"token_provider.type={provider_type!r} parser not implemented"
    )


def _resolve_exec(provider: TokenProvider) -> str:
    """Run the configured subprocess and return its stdout.

    Trims *only* the trailing newline — opaque tokens may contain ``=``
    padding or internal spaces. Non-zero exit, timeout, or unexpected
    failure raises ``DeploymentError`` with a message naming the
    provider type and exit code. The resolved token value never appears
    in any log line.
    """
    argv = list(provider.command)
    logger.info("Running token provider exec: %s", argv[0])
    logger.debug("token provider argv: %s", argv)
    try:
        completed = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            timeout=provider.timeout,
        )
    except subprocess.CalledProcessError as exc:
        stderr_tail = (exc.stderr or "")[-500:]
        raise DeploymentError(
            f"token_provider type=exec failed: {argv[0]} exited "
            f"{exc.returncode}: {stderr_tail.strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DeploymentError(
            f"token_provider type=exec timed out: {argv[0]} did not "
            f"return within {provider.timeout}s"
        ) from exc
    return completed.stdout.rstrip("\n")
