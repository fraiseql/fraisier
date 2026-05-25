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
token. It is called once per ``TokenProvider`` instance per deploy by
the deploy pipeline and may raise ``DeploymentError`` on failure — a
401 from the IdP or a script crash means "fix the config / IdP," not
"rebuild from scratch." The deployer catches that ``DeploymentError``
in ``_run_smoke_tests_or_halt`` and halts with ``status=failed``
without invoking ``_restore_previous_state`` — migrations have run and
the service has restarted by this point, but a transient IdP issue is
not a code regression and a rollback on every IdP hiccup would be the
wrong default.

The class hierarchy is one base + one subclass per provider type. Each
subclass declares its own type-specific fields as required (non-``None``)
attributes; the parser dispatches on the YAML ``type`` to the matching
subclass. Adding a new provider type means writing one subclass and
listing it in ``_PROVIDER_CLASSES`` — no other code changes.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from string import Formatter
from typing import ClassVar, TypedDict

import httpx

from fraisier.config._lazy_env import LazyEnv, is_string_like, to_str
from fraisier.errors import ConfigurationError, DeploymentError

logger = logging.getLogger(__name__)

_DEFAULT_EXEC_TIMEOUT = 10
_DEFAULT_OAUTH2_TIMEOUT = 10

_COMMON_KEYS: frozenset[str] = frozenset({"type", "header", "format", "timeout"})


def _oauth2_http_transport() -> httpx.BaseTransport | None:
    """Hook for tests to substitute an ``httpx.MockTransport``.

    Returns ``None`` in production — ``httpx.Client(transport=None)``
    uses the real network transport, which is the desired default.
    """
    return None


@dataclass(frozen=True, kw_only=True)
class TokenProvider:
    """Abstract base for typed token providers.

    Common fields (``header``, ``format``, ``timeout``) live here; each
    subclass extends with its own required fields. The base class is
    not instantiated directly — ``parse_token_provider`` returns one of
    the concrete subclasses. Annotations that accept any provider type
    use ``TokenProvider`` (the base) for breadth.
    """

    header: str = "Authorization"
    format: str = "Bearer {token}"
    timeout: int = _DEFAULT_EXEC_TIMEOUT

    #: The YAML ``type:`` string this class represents. Subclasses override.
    TYPE: ClassVar[str] = ""

    #: Keys accepted in the ``token_provider:`` mapping for this type.
    #: Subclasses extend ``_COMMON_KEYS`` with their type-specific fields.
    VALID_KEYS: ClassVar[frozenset[str]] = _COMMON_KEYS

    @property
    def type(self) -> str:
        """The YAML ``type:`` string of this provider instance."""
        return self.TYPE

    def resolve(self) -> str:
        """Acquire the token, raising ``DeploymentError`` on failure."""
        raise NotImplementedError("subclasses must implement resolve()")

    @classmethod
    def _parse(cls, raw: dict) -> TokenProvider:
        """Parse this provider type's specific fields. Subclasses override."""
        raise NotImplementedError("subclasses must implement _parse()")


@dataclass(frozen=True, kw_only=True)
class ExecTokenProvider(TokenProvider):
    """Run a subprocess and use its stdout as the token.

    The script runs as the deploy user with the deploy user's
    environment inherited unchanged (``cwd`` and ``env_passthrough``
    are deferred — see #215). ``argv[0]`` is logged at INFO; full argv
    at DEBUG; the resolved token never appears in any log line at any
    level.
    """

    command: tuple[str, ...]

    TYPE: ClassVar[str] = "exec"
    VALID_KEYS: ClassVar[frozenset[str]] = _COMMON_KEYS | {"command"}

    def resolve(self) -> str:
        argv = list(self.command)
        logger.info("Running token provider exec: %s", argv[0])
        logger.debug("token provider argv: %s", argv)
        try:
            completed = subprocess.run(
                argv,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.CalledProcessError as exc:
            stderr_tail = (exc.stderr or "")[-500:].strip()
            if stderr_tail:
                # Deliberately at DEBUG only — a `set -x` wrapper could
                # echo the token to stderr, and the outer logger.exception
                # would otherwise drop it into the deploy journal.
                logger.debug(
                    "token_provider exec stderr tail (%s, exit=%d): %s",
                    argv[0],
                    exc.returncode,
                    stderr_tail,
                )
            raise DeploymentError(
                f"token_provider type=exec failed: {argv[0]} exited "
                f"{exc.returncode} (enable DEBUG logging for stderr)"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DeploymentError(
                f"token_provider type=exec timed out: {argv[0]} did not "
                f"return within {self.timeout}s"
            ) from exc
        # Trim only the trailing newline — opaque tokens may contain
        # `=` padding or internal spaces.
        return completed.stdout.rstrip("\n")

    @classmethod
    def _parse(cls, raw: dict) -> ExecTokenProvider:
        command_raw = raw.get("command")
        if not command_raw or not isinstance(command_raw, list):
            raise ConfigurationError(
                "token_provider.command must be a non-empty list of "
                f"strings for type=exec, got {command_raw!r}"
            )
        return cls(
            **_parse_common(raw, _DEFAULT_EXEC_TIMEOUT),
            command=tuple(str(arg) for arg in command_raw),
        )


@dataclass(frozen=True, kw_only=True)
class Oauth2ClientCredentialsTokenProvider(TokenProvider):
    """OIDC ``grant_type=client_credentials`` machine-to-machine grant.

    POSTs ``client_id`` + ``client_secret`` (plus optional ``audience``
    and ``scope``) to ``token_url`` and returns the response's
    ``access_token``. ``client_secret`` is redacted in all log lines;
    the token endpoint's response body is never echoed in the raised
    ``DeploymentError`` (some IdPs include the client_secret in error
    envelopes).
    """

    token_url: str | LazyEnv
    client_id: str | LazyEnv
    client_secret: str | LazyEnv
    audience: str | None = None
    scope: str | None = None

    TYPE: ClassVar[str] = "oauth2_client_credentials"
    VALID_KEYS: ClassVar[frozenset[str]] = _COMMON_KEYS | {
        "token_url",
        "client_id",
        "client_secret",
        "audience",
        "scope",
    }

    def resolve(self) -> str:
        return _post_oauth2_token(
            token_url=to_str(self.token_url),
            form_body={
                "grant_type": "client_credentials",
                "client_id": to_str(self.client_id),
                "client_secret": to_str(self.client_secret),
                "audience": self.audience,
                "scope": self.scope,
            },
            timeout=self.timeout,
            provider_type=self.TYPE,
        )

    @classmethod
    def _parse(cls, raw: dict) -> Oauth2ClientCredentialsTokenProvider:
        return cls(
            **_parse_common(raw, _DEFAULT_OAUTH2_TIMEOUT),
            token_url=_require_str(raw, "token_url", cls.TYPE),
            client_id=_require_str(raw, "client_id", cls.TYPE),
            client_secret=_require_str(raw, "client_secret", cls.TYPE),
            audience=raw.get("audience"),
            scope=raw.get("scope"),
        )


@dataclass(frozen=True, kw_only=True)
class Oauth2RefreshTokenProvider(TokenProvider):
    """OIDC ``grant_type=refresh_token`` grant.

    POSTs ``client_id`` + ``refresh_token`` (plus optional ``scope``)
    to ``token_url`` and returns the response's ``access_token``. Any
    rotated ``refresh_token`` in the response is **discarded** —
    persisting it is out of scope for fraisier; rotation remains the
    operator's responsibility (e.g. a scheduled rotator that updates
    the deploy user's secrets file).
    """

    token_url: str | LazyEnv
    client_id: str | LazyEnv
    refresh_token: str | LazyEnv
    scope: str | None = None

    TYPE: ClassVar[str] = "oauth2_refresh_token"
    VALID_KEYS: ClassVar[frozenset[str]] = _COMMON_KEYS | {
        "token_url",
        "client_id",
        "refresh_token",
        "scope",
    }

    def resolve(self) -> str:
        return _post_oauth2_token(
            token_url=to_str(self.token_url),
            form_body={
                "grant_type": "refresh_token",
                "client_id": to_str(self.client_id),
                "refresh_token": to_str(self.refresh_token),
                "scope": self.scope,
            },
            timeout=self.timeout,
            provider_type=self.TYPE,
        )

    @classmethod
    def _parse(cls, raw: dict) -> Oauth2RefreshTokenProvider:
        return cls(
            **_parse_common(raw, _DEFAULT_OAUTH2_TIMEOUT),
            token_url=_require_str(raw, "token_url", cls.TYPE),
            client_id=_require_str(raw, "client_id", cls.TYPE),
            refresh_token=_require_str(raw, "refresh_token", cls.TYPE),
            scope=raw.get("scope"),
        )


_PROVIDER_CLASSES: dict[str, type[TokenProvider]] = {
    cls.TYPE: cls
    for cls in (
        ExecTokenProvider,
        Oauth2ClientCredentialsTokenProvider,
        Oauth2RefreshTokenProvider,
    )
}

_VALID_PROVIDER_TYPES: frozenset[str] = frozenset(_PROVIDER_CLASSES)


def parse_token_provider(raw: dict) -> TokenProvider:
    """Parse a ``token_provider:`` mapping into a typed ``TokenProvider``.

    Pure parse — no subprocesses, no network. Dispatches on the
    ``type`` field to one of the concrete subclasses
    (``ExecTokenProvider`` / ``Oauth2ClientCredentialsTokenProvider`` /
    ``Oauth2RefreshTokenProvider``). Unknown or missing ``type`` raises
    ``ConfigurationError`` naming the currently-valid provider types.
    Unknown keys (including ``cwd`` and ``env_passthrough``, deferred
    from #215) are rejected with the valid-key list for the declared
    type so typos and unimplemented options surface at config-load
    time, not as silent no-ops at deploy time.
    """
    if "type" not in raw:
        raise ConfigurationError(
            "token_provider is missing required 'type' field; "
            f"valid types: {sorted(_VALID_PROVIDER_TYPES)!r}"
        )
    provider_type = raw["type"]
    cls = _PROVIDER_CLASSES.get(provider_type)
    if cls is None:
        raise ConfigurationError(
            f"token_provider.type must be one of "
            f"{sorted(_VALID_PROVIDER_TYPES)!r}, got {provider_type!r}"
        )

    unknown = set(raw) - cls.VALID_KEYS
    if unknown:
        raise ConfigurationError(
            f"token_provider type={provider_type} has unknown key(s): "
            f"{sorted(unknown)!r}; valid: {sorted(cls.VALID_KEYS)!r}"
        )

    return cls._parse(raw)


class _CommonFields(TypedDict):
    header: str
    format: str
    timeout: int


def _parse_common(raw: dict, default_timeout: int) -> _CommonFields:
    """Pull the common fields (header/format/timeout) from a raw mapping.

    Format is validated here so every provider type benefits from it
    without each subclass duplicating the check. Returns a TypedDict
    so the values keep their narrow types when splatted into the
    subclass constructor.
    """
    fmt = raw.get("format", "Bearer {token}")
    _validate_format(fmt)
    return {
        "header": raw.get("header", "Authorization"),
        "format": fmt,
        "timeout": int(raw.get("timeout", default_timeout)),
    }


def _require_str(raw: dict, field_name: str, provider_type: str) -> str | LazyEnv:
    """Return the value of ``raw[field_name]``, accepting ``str | LazyEnv``.

    Truthy check survives — ``LazyEnv`` is always truthy by design, so
    an unresolved env-var reference counts as "configured." The actual
    env lookup is deferred to provider ``.resolve()`` time via
    ``to_str()``.
    """
    value = raw.get(field_name)
    if not value or not is_string_like(value):
        raise ConfigurationError(
            f"token_provider.{field_name} must be a non-empty string for "
            f"type={provider_type}, got {value!r}"
        )
    return value


def _validate_format(fmt: str) -> None:
    """Reject a ``format`` string that wouldn't carry the resolved token.

    ``Formatter().parse()`` yields ``(literal, field_name, ...)`` tuples.
    The only legal field name is ``token`` — anything else (e.g.
    ``{access_token}``) would raise ``KeyError`` at resolve time, and
    a format with no field at all would silently drop the resolved
    token and ship a constant header value (often "Bearer XYZ") to
    every smoke test. Both shapes fail closed at parse time instead.
    """
    if not isinstance(fmt, str):
        raise ConfigurationError(
            f"token_provider.format must be a string, got {type(fmt).__name__}"
        )
    field_names: list[str] = []
    try:
        for _literal, field_name, _spec, _conv in Formatter().parse(fmt):
            if field_name is not None:
                field_names.append(field_name)
    except ValueError as exc:
        raise ConfigurationError(
            f"token_provider.format is not a valid format string: {exc}"
        ) from exc
    if "token" not in field_names:
        raise ConfigurationError(
            "token_provider.format must contain the literal '{token}' "
            f"placeholder, got {fmt!r}"
        )
    other = sorted(set(field_names) - {"token"})
    if other:
        raise ConfigurationError(
            f"token_provider.format contains unsupported placeholder(s) "
            f"{other!r}; only '{{token}}' is substituted (got {fmt!r})"
        )


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
