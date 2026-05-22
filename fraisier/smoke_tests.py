"""Authenticated smoke-test runner (#204 PR B).

After ``/health`` passes, the deploy pipeline runs configured HTTP
requests with bearer credentials and JSONPath assertions. Failure
default is ``rollback`` — the whole point of this hook is to catch
regressions that unauthenticated ``/health`` missed; if the
authenticated probe fails too, the new code is broken.

JSONPath is intentionally a minimal ``$.dotted.path`` subset — no
recursion (``$..foo``), no array filters (``$.a[0]``), no wildcards
(``$.*``). Unsupported shapes are rejected at schema parse time with
the message ``unsupported JSONPath syntax: use $.dotted.path only``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import httpx

from fraisier.errors import ConfigurationError
from fraisier.token_providers import TokenProvider, parse_token_provider

logger = logging.getLogger(__name__)


_VALID_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_VALID_ON_FAILURE = frozenset({"rollback", "halt", "warn"})
_VALID_ASSERTION_KEYS = frozenset({"json_path", "not_null", "null", "equals"})


class _Missing:
    """Sentinel returned by ``_walk_json_path`` when a key is absent.

    Distinct from JSON ``null`` (Python ``None``). The two need different
    handling for the ``null`` assertion verb — both should satisfy a
    ``null: true`` assertion ("the key isn't there or is explicitly
    null"); only ``not_null: true`` distinguishes them.
    """

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "<missing>"


_MISSING = _Missing()


# Reject any path that includes recursion, array indexing, or wildcards
# at parse time. The hand-rolled walker only understands `$.a.b.c`.
_UNSUPPORTED_JSONPATH_CHARS = re.compile(r"\.\.|[\[*@]")


class SmokeTestError(Exception):
    """Raised by ``run_smoke_tests`` when a probe fails.

    ``rollback`` is ``True`` when the failing test's policy was
    ``rollback`` (default) so the deploy pipeline can dispatch to
    ``_restore_previous_state``. When ``False`` (``halt`` policy), the
    deploy still aborts but no rollback runs — the operator must
    investigate manually because the failure is policy-marked as
    irrecoverable by the smoke-test itself.
    """

    def __init__(self, message: str, *, rollback: bool):
        super().__init__(message)
        self.rollback = rollback


@dataclass(frozen=True)
class Assertion:
    """One assertion against the JSON response body."""

    json_path: str
    not_null: bool = False
    null: bool = False
    equals: Any = _MISSING  # _MISSING means "this verb not in use"

    def matches(self, doc: Any) -> bool:
        actual = _walk_json_path(doc, self.json_path)
        if self.not_null:
            return actual is not _MISSING and actual is not None
        if self.null:
            return actual is _MISSING or actual is None
        if self.equals is not _MISSING:
            return actual == self.equals
        # No verb set — vacuously true (loader rejects this shape).
        return True


@dataclass(frozen=True)
class SmokeTest:
    """One smoke-test entry."""

    name: str
    method: str
    url: str
    headers: dict[str, str]
    body: str | None
    timeout: int
    on_failure: Literal["rollback", "halt", "warn"]
    assertions: list[Assertion]
    token_provider: TokenProvider | None = None


def _walk_json_path(doc: Any, path: str) -> Any:
    """Walk a ``$.a.b.c`` path through *doc*; return ``_MISSING`` if absent.

    The leading ``$`` is required; ``$`` alone returns the root document.
    Any intermediate value that is not a ``dict`` short-circuits to
    ``_MISSING``.
    """
    if not path.startswith("$"):
        raise ConfigurationError(f"JSONPath must start with $: {path!r}")
    if path == "$":
        return doc
    if not path.startswith("$."):
        raise ConfigurationError("unsupported JSONPath syntax: use $.dotted.path only")
    parts = path[2:].split(".")
    current: Any = doc
    for key in parts:
        if not isinstance(current, dict):
            return _MISSING
        if key not in current:
            return _MISSING
        current = current[key]
    return current


def _parse_assertion(raw: dict) -> Assertion:
    unknown = set(raw) - _VALID_ASSERTION_KEYS
    if unknown:
        raise ConfigurationError(
            f"unknown assertion key(s): {sorted(unknown)!r}; valid: "
            f"{sorted(_VALID_ASSERTION_KEYS)!r}"
        )
    if "json_path" not in raw:
        raise ConfigurationError("assertion is missing required 'json_path'")
    json_path = raw["json_path"]
    if _UNSUPPORTED_JSONPATH_CHARS.search(json_path):
        raise ConfigurationError(
            f"unsupported JSONPath syntax: use $.dotted.path only (got {json_path!r})"
        )
    # Validate the walker can parse it (raises on missing $).
    _walk_json_path({}, json_path)
    return Assertion(
        json_path=json_path,
        not_null=bool(raw.get("not_null", False)),
        null=bool(raw.get("null", False)),
        equals=raw.get("equals", _MISSING),
    )


def _resolve_url(url: str, *, base_url: str | None) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        # Absolute URL.
        return url
    if url.startswith("/"):
        if base_url is None:
            raise ConfigurationError(
                "smoke_tests.url is relative but no health_check.url is "
                "configured to resolve against — provide an absolute URL "
                "or configure health_check"
            )
        return urljoin(base_url.rstrip("/") + "/", url.lstrip("/"))
    raise ConfigurationError(
        f"smoke_tests.url must be absolute (scheme://host/...) or relative "
        f"with a leading '/', got {url!r}"
    )


def load_smoke_tests(
    env_config: dict,
    *,
    base_url: str | None,
) -> list[SmokeTest]:
    """Parse ``smoke_tests:`` into a list of ``SmokeTest`` objects.

    *base_url* is the scheme+host of ``health_check.url`` (without path).
    Relative entries (``url: /graphql``) are joined onto it. Any schema
    error — unknown method, unknown ``on_failure``, malformed JSONPath,
    relative URL without a base, unknown ``token_provider.type`` — raises
    ``ConfigurationError``.
    """
    raw = env_config.get("smoke_tests") or []
    tests: list[SmokeTest] = []
    for entry in raw:
        method = (entry.get("method") or "GET").upper()
        if method not in _VALID_METHODS:
            raise ConfigurationError(
                f"smoke_tests.method must be one of {sorted(_VALID_METHODS)!r}, "
                f"got {method!r}"
            )

        on_failure = entry.get("on_failure", "rollback")
        if on_failure not in _VALID_ON_FAILURE:
            raise ConfigurationError(
                f"smoke_tests.on_failure must be one of "
                f"{sorted(_VALID_ON_FAILURE)!r}, got {on_failure!r}"
            )

        url = _resolve_url(entry["url"], base_url=base_url)

        assertions = [_parse_assertion(a) for a in entry.get("assert", [])]

        provider_raw = entry.get("token_provider")
        token_provider = (
            parse_token_provider(provider_raw) if provider_raw is not None else None
        )

        headers = dict(entry.get("headers") or {})
        if token_provider is not None:
            provider_header_lower = token_provider.header.lower()
            for existing_header in headers:
                if existing_header.lower() == provider_header_lower:
                    raise ConfigurationError(
                        f"smoke_tests[{entry.get('name', url)!r}]: header "
                        f"collision between static headers.{existing_header!r} "
                        f"and token_provider.header={token_provider.header!r}; "
                        "remove the static header or change the provider's target"
                    )

        tests.append(
            SmokeTest(
                name=entry.get("name", url),
                method=method,
                url=url,
                headers=headers,
                body=entry.get("body"),
                timeout=int(entry.get("timeout", 5)),
                on_failure=on_failure,
                assertions=assertions,
                token_provider=token_provider,
            )
        )
    return tests


def _should_rollback(test: SmokeTest) -> bool:
    return test.on_failure == "rollback"


def _redacted_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted = {}
    for k, v in headers.items():
        if k.lower() in {"authorization", "cookie", "x-api-key"}:
            redacted[k] = "***redacted***"
        else:
            redacted[k] = v
    return redacted


def _run_one(test: SmokeTest) -> None:
    logger.info(
        "Running smoke test %s: %s %s (headers=%s)",
        test.name,
        test.method,
        test.url,
        _redacted_headers(test.headers),
    )
    try:
        with httpx.Client(timeout=test.timeout) as client:
            response = client.request(
                test.method,
                test.url,
                headers=test.headers,
                content=test.body,
            )
    except httpx.TimeoutException as exc:
        raise SmokeTestError(
            f"smoke test {test.name!r} timed out after {test.timeout}s: {exc}",
            rollback=_should_rollback(test),
        ) from exc
    except httpx.RequestError as exc:
        raise SmokeTestError(
            f"smoke test {test.name!r} request failed: {exc}",
            rollback=_should_rollback(test),
        ) from exc

    if not (200 <= response.status_code < 300):
        raise SmokeTestError(
            f"smoke test {test.name!r} returned HTTP {response.status_code}",
            rollback=_should_rollback(test),
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise SmokeTestError(
            f"smoke test {test.name!r} returned non-JSON body: {exc}",
            rollback=_should_rollback(test),
        ) from exc

    for assertion in test.assertions:
        if not assertion.matches(body):
            actual = _walk_json_path(body, assertion.json_path)
            raise SmokeTestError(
                f"smoke test {test.name!r} assertion failed: "
                f"{assertion.json_path}={actual!r} did not satisfy {assertion!r}",
                rollback=_should_rollback(test),
            )


def run_smoke_tests(tests: list[SmokeTest]) -> None:
    """Run each test in order.

    A ``rollback`` or ``halt`` failure raises ``SmokeTestError``
    immediately; the deploy pipeline reads ``exc.rollback`` to decide
    whether to invoke ``_restore_previous_state``. A ``warn`` failure
    is logged at WARNING and iteration continues.
    """
    for test in tests:
        try:
            _run_one(test)
        except SmokeTestError as exc:
            if test.on_failure == "warn":
                logger.warning("smoke test %s failed (warn): %s", test.name, exc)
                continue
            raise
