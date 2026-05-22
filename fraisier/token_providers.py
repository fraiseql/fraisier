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

import httpx

from fraisier.errors import ConfigurationError, DeploymentError

logger = logging.getLogger(__name__)

_VALID_PROVIDER_TYPES: frozenset[str] = frozenset(
    {"exec", "oauth2_client_credentials", "oauth2_refresh_token"}
)

_DEFAULT_EXEC_TIMEOUT = 10
_DEFAULT_OAUTH2_TIMEOUT = 10


def _oauth2_http_transport() -> httpx.BaseTransport | None:
    """Hook for tests to substitute an ``httpx.MockTransport``.

    Returns ``None`` in production — ``httpx.Client(transport=None)``
    uses the real network transport, which is the desired default.
    """
    return None


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
    # oauth2_*-specific
    token_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    audience: str | None = None
    scope: str | None = None
    refresh_token: str | None = None

    def resolve(self) -> str:
        """Acquire the token from the underlying provider.

        Called exactly once per provider per deploy. Raises
        ``DeploymentError`` on failure (non-zero exit, timeout,
        unexpected exception).
        """
        if self.type == "exec":
            return _resolve_exec(self)
        if self.type == "oauth2_client_credentials":
            return _resolve_oauth2_client_credentials(self)
        if self.type == "oauth2_refresh_token":
            return _resolve_oauth2_refresh_token(self)
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

    if provider_type == "oauth2_client_credentials":
        oauth2_timeout = int(raw.get("timeout", _DEFAULT_OAUTH2_TIMEOUT))
        return TokenProvider(
            type=provider_type,
            header=header,
            format=fmt,
            timeout=oauth2_timeout,
            token_url=_require_str(raw, "token_url", provider_type),
            client_id=_require_str(raw, "client_id", provider_type),
            client_secret=_require_str(raw, "client_secret", provider_type),
            audience=raw.get("audience"),
            scope=raw.get("scope"),
        )

    if provider_type == "oauth2_refresh_token":
        oauth2_timeout = int(raw.get("timeout", _DEFAULT_OAUTH2_TIMEOUT))
        return TokenProvider(
            type=provider_type,
            header=header,
            format=fmt,
            timeout=oauth2_timeout,
            token_url=_require_str(raw, "token_url", provider_type),
            client_id=_require_str(raw, "client_id", provider_type),
            refresh_token=_require_str(raw, "refresh_token", provider_type),
            scope=raw.get("scope"),
        )

    # Defensive — should not reach here given the type-gate above.
    raise ConfigurationError(  # pragma: no cover
        f"token_provider.type={provider_type!r} parser not implemented"
    )


def _require_str(raw: dict, field_name: str, provider_type: str) -> str:
    value = raw.get(field_name)
    if not value or not isinstance(value, str):
        raise ConfigurationError(
            f"token_provider.{field_name} must be a non-empty string for "
            f"type={provider_type}, got {value!r}"
        )
    return value


def _post_oauth2_token(
    *,
    token_url: str,
    form_body: dict[str, str | None],
    timeout: int,
    provider_type: str,
) -> str:
    """Shared helper for OAuth2 token-endpoint POSTs.

    Sends a form-encoded body to *token_url*, expects a JSON response
    with ``access_token``. Drops ``None``-valued form keys. Failure
    surfaces as ``DeploymentError`` with provider type + status code or
    high-level reason — never the response body verbatim, because some
    IdPs echo the client_secret in error responses.

    The request body is logged with secret fields redacted: never the
    raw form body.
    """
    body = {k: v for k, v in form_body.items() if v is not None}
    redacted_body = {
        k: ("***redacted***" if k in {"client_secret", "refresh_token"} else v)
        for k, v in body.items()
    }
    logger.info(
        "Requesting OAuth2 token from %s (grant=%s)",
        token_url,
        body.get("grant_type"),
    )
    logger.debug("OAuth2 form body (redacted): %s", redacted_body)
    transport = _oauth2_http_transport()
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.post(token_url, data=body)
    except httpx.HTTPError as exc:
        raise DeploymentError(
            f"token_provider type={provider_type} network error talking "
            f"to {token_url}: {type(exc).__name__}"
        ) from exc

    if not (200 <= response.status_code < 300):
        raise DeploymentError(
            f"token_provider type={provider_type} token endpoint "
            f"{token_url} returned HTTP {response.status_code}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise DeploymentError(
            f"token_provider type={provider_type} token endpoint returned "
            "non-JSON response body"
        ) from exc

    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise DeploymentError(
            f"token_provider type={provider_type} token endpoint response "
            "missing 'access_token' field"
        )
    return access_token


def _resolve_oauth2_client_credentials(provider: TokenProvider) -> str:
    """OIDC client-credentials grant.

    POSTs ``grant_type=client_credentials`` with client_id, client_secret,
    and the optional ``audience`` and ``scope`` to ``token_url``.
    Returns the ``access_token`` from the JSON response.
    """
    assert provider.token_url is not None  # parser guarantee
    assert provider.client_id is not None
    assert provider.client_secret is not None
    return _post_oauth2_token(
        token_url=provider.token_url,
        form_body={
            "grant_type": "client_credentials",
            "client_id": provider.client_id,
            "client_secret": provider.client_secret,
            "audience": provider.audience,
            "scope": provider.scope,
        },
        timeout=provider.timeout,
        provider_type=provider.type,
    )


def _resolve_oauth2_refresh_token(provider: TokenProvider) -> str:
    """OIDC ``refresh_token`` grant.

    POSTs ``grant_type=refresh_token`` with ``client_id`` and
    ``refresh_token``. Returns the ``access_token`` from the JSON
    response. Any rotated ``refresh_token`` in the response is
    **discarded** — persisting it is out of scope for fraisier and
    remains the operator's responsibility (e.g. a separate scheduled
    rotator that updates the deploy user's secrets file).
    """
    assert provider.token_url is not None  # parser guarantee
    assert provider.client_id is not None
    assert provider.refresh_token is not None
    return _post_oauth2_token(
        token_url=provider.token_url,
        form_body={
            "grant_type": "refresh_token",
            "client_id": provider.client_id,
            "refresh_token": provider.refresh_token,
            "scope": provider.scope,
        },
        timeout=provider.timeout,
        provider_type=provider.type,
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
