"""One reading of a PostgreSQL URL, libpq's, for every child fraisier starts.

A URL is parsed once into :class:`PgConn` and handed to a child as libpq's
``PG*`` environment variables, so nothing but non-secret arguments ever sits
on a child's argv. The parse follows libpq's rules as captured from libpq
itself: the netloc first, then the path, then the query in order, each later
value overwriting an earlier one; userinfo splits on the first ``@``; ``%XX``
is decoded strictly and ``+`` stays ``+``.

A URL that libpq would read differently from fraisier is refused rather than
guessed at: a keyword with no environment variable, ``service``, an unknown
keyword, a malformed escape, a second unencoded ``@``, a URL naming no
database.

Imports nothing from fraisier, so config validation can use it without an
import cycle.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

#: libpq keyword → environment variable, from ``PQconndefaults()`` on libpq
#: 18. ``service`` is listed because libpq lists it; ``parse`` refuses it.
_KEYWORD_ENV: dict[str, str] = {
    "service": "PGSERVICE",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "passfile": "PGPASSFILE",
    "channel_binding": "PGCHANNELBINDING",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "dbname": "PGDATABASE",
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "port": "PGPORT",
    "client_encoding": "PGCLIENTENCODING",
    "options": "PGOPTIONS",
    "application_name": "PGAPPNAME",
    "sslmode": "PGSSLMODE",
    "sslnegotiation": "PGSSLNEGOTIATION",
    "sslcompression": "PGSSLCOMPRESSION",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "sslcertmode": "PGSSLCERTMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcrl": "PGSSLCRL",
    "sslcrldir": "PGSSLCRLDIR",
    "sslsni": "PGSSLSNI",
    "requirepeer": "PGREQUIREPEER",
    "require_auth": "PGREQUIREAUTH",
    "min_protocol_version": "PGMINPROTOCOLVERSION",
    "max_protocol_version": "PGMAXPROTOCOLVERSION",
    "ssl_min_protocol_version": "PGSSLMINPROTOCOLVERSION",
    "ssl_max_protocol_version": "PGSSLMAXPROTOCOLVERSION",
    "gssencmode": "PGGSSENCMODE",
    "krbsrvname": "PGKRBSRVNAME",
    "gsslib": "PGGSSLIB",
    "gssdelegation": "PGGSSDELEGATION",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "load_balance_hosts": "PGLOADBALANCEHOSTS",
}
ENV_KEYWORD = {var: key for key, var in _KEYWORD_ENV.items()}

#: Keywords libpq 18 accepts that have no environment variable. A child could
#: only be given one on argv, so ``parse`` refuses them.
_NO_ENV = frozenset(
    {
        "fallback_application_name",
        "keepalives",
        "keepalives_idle",
        "keepalives_interval",
        "keepalives_count",
        "tcp_user_timeout",
        "sslpassword",
        "replication",
        "scram_client_key",
        "scram_server_key",
        "oauth_issuer",
        "oauth_client_id",
        "oauth_client_secret",
        "oauth_scope",
        "sslkeylogfile",
    }
)

#: The only ambient ``PG*`` variables a child keeps: operator-scoped defaults
#: (certificates, the password file, auth policy, timeouts, a label) that
#: cannot change which server, user, database or session the URL names.
#: Every other ``PG*`` is dropped, including ones libpq adds later.
OPERATOR_DEFAULTS = frozenset(
    {
        "PGSSLROOTCERT",
        "PGSSLCERT",
        "PGSSLKEY",
        "PGSSLCRL",
        "PGSSLCRLDIR",
        "PGPASSFILE",
        "PGREQUIREAUTH",
        "PGCONNECT_TIMEOUT",
        "PGAPPNAME",
    }
)

#: Variables that set transport security and nothing else. The allowlist
#: drops them, because an ambient value could weaken a URL that does not pin
#: them; but an operator who exported one meant a policy, so a TCP child they
#: would have reached is refused instead of connecting without it (D15).
TRANSPORT_POLICY = frozenset(
    {
        "PGSSLMODE",
        "PGSSLCERTMODE",
        "PGSSLNEGOTIATION",
        "PGSSLMINPROTOCOLVERSION",
        "PGSSLMAXPROTOCOLVERSION",
        "PGGSSENCMODE",
        "PGCHANNELBINDING",
        "PGKRBSRVNAME",
        "PGGSSLIB",
    }
)

#: Exit status of a runner command refused by :func:`transport_refusal`.
TRANSPORT_REFUSED = 123

_SCHEMES = ("postgresql://", "postgres://")

_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})?")


def decode(component: str) -> str:
    """Percent-decode *component* the way libpq does.

    ``%XX`` (two hex digits) is that byte, and the bytes are UTF-8. A ``%``
    not followed by two hex digits and ``%00`` are refused, as libpq refuses
    them. ``+`` is not a space.
    """
    out = bytearray()
    pos = 0
    for match in _ESCAPE.finditer(component):
        out += component[pos : match.start()].encode()
        if match.group(1) is None:
            # Not echoed: the component may be the password.
            msg = "invalid percent-encoded token: '%' must start a %XX escape"
            raise ValueError(msg)
        byte = int(match.group(1), 16)
        if byte == 0:
            msg = "forbidden value %00 in a percent-encoded value"
            raise ValueError(msg)
        out.append(byte)
        pos = match.end()
    out += component[pos:].encode()
    return out.decode()


def split_netloc(netloc: str) -> tuple[str, str]:
    """``(userinfo, hostport)``, split on the first ``@`` as libpq splits it.

    A second unencoded ``@`` is refused: libpq would read everything after
    the first as the host.
    """
    userinfo, at, hostport = netloc.partition("@")
    if not at:
        return "", netloc
    if "@" in hostport:
        msg = "a second '@' in the URL's netloc: percent-encode '@' as %40"
        raise ValueError(msg)
    return userinfo, hostport


def split_url(url: str) -> tuple[str, str, str, str] | None:
    """``(scheme, netloc, path, query)`` of *url*, cut where libpq cuts it.

    Only ``?`` and the first ``/`` delimit; a ``#`` is an ordinary character,
    as it is to libpq. ``None`` for anything but a PostgreSQL URL.
    """
    scheme = next((s for s in _SCHEMES if url.startswith(s)), None)
    if scheme is None:
        return None
    rest, _, query = url[len(scheme) :].partition("?")
    netloc, _, path = rest.partition("/")
    return scheme, netloc, path, query


def query_params(query: str) -> list[str]:
    """The ``&``-separated parameters of *query*; libpq ignores one trailing ``&``."""
    params = query.split("&") if query else []
    if len(params) > 1 and not params[-1]:
        params.pop()
    return params


def split_param(param: str) -> tuple[str, str]:
    """``(raw_key, raw_value)`` of one query parameter, which must have ``=``."""
    raw_key, eq, raw_value = param.partition("=")
    if not eq:
        # A key-less "password:secret" typo would otherwise be echoed.
        key = "<redacted>" if "password" in raw_key else raw_key
        msg = f'missing key/value separator "=" in URI query parameter: {key!r}'
        raise ValueError(msg)
    return raw_key, raw_value


def _host_port(entry: str) -> tuple[str, str]:
    """One ``host[:port]`` entry of a netloc, brackets stripped from IPv6."""
    if entry.startswith("["):
        host, bracket, rest = entry[1:].partition("]")
        if not bracket or (rest and not rest.startswith(":")):
            msg = "unterminated IPv6 address in the URL's host"
            raise ValueError(msg)
        return host, rest[1:]
    host, _, port = entry.partition(":")
    return decode(host), decode(port)


def _check_keyword(key: str) -> None:
    if key == "service":
        msg = (
            "URL query parameter 'service' is not supported: a service file "
            "outranks environment variables, so the URL's other values would "
            "lose to it"
        )
        raise ValueError(msg)
    if key in _NO_ENV:
        msg = (
            f"URL query parameter {key!r} has no libpq environment variable, so "
            "fraisier cannot pass it without putting it on argv"
        )
        raise ValueError(msg)
    if key not in _KEYWORD_ENV:
        msg = f'invalid URI query parameter: "{key}"'
        raise ValueError(msg)


def scrubbed(environ: Mapping[str, str]) -> dict[str, str]:
    """*environ* minus every ``PG*`` variable but :data:`OPERATOR_DEFAULTS`."""
    return {
        key: value
        for key, value in environ.items()
        if not key.startswith("PG") or key in OPERATOR_DEFAULTS
    }


def is_socket(pg_env: Mapping[str, str]) -> bool:
    """Whether a child with this connection env reaches only unix sockets.

    libpq applies no TLS or GSS encryption there. A host is a socket when it
    is empty (libpq's default socket) or a path (``/…``, or ``@…`` for an
    abstract one); a ``hostaddr`` always means TCP.
    """
    if pg_env.get("PGHOSTADDR"):
        return False
    hosts = pg_env.get("PGHOST", "").split(",")
    return all(not host or host.startswith(("/", "@")) for host in hosts)


def transport_refusal(
    environ: Mapping[str, str], pg_env: Mapping[str, str]
) -> str | None:
    """Why a child with connection env *pg_env* may not run in *environ*.

    ``None`` unless the child uses TCP and *environ* sets a
    :data:`TRANSPORT_POLICY` variable *pg_env* does not, whatever its value:
    the child would connect without it. The message names the URL fix.
    """
    if is_socket(pg_env):
        return None
    dropped = sorted(v for v in TRANSPORT_POLICY if v in environ and v not in pg_env)
    if not dropped:
        return None
    fix = "&".join(f"{ENV_KEYWORD[v]}={environ[v]}" for v in dropped)
    return (
        f"{', '.join(dropped)} is set in the environment, and fraisier no longer "
        f"passes it to database clients: put it in the URL (?{fix})"
    )


@dataclass(frozen=True)
class PgConn:
    """A PostgreSQL URL as libpq reads it: keyword → decoded value."""

    params: Mapping[str, str] = field(repr=False)

    @classmethod
    def parse(cls, url: str) -> PgConn:
        """Parse *url* (``postgresql://`` or ``postgres://``) with libpq's rules.

        Raises:
            ValueError: naming what libpq would refuse or read differently.
                The message never contains the password.
        """
        parts = split_url(url)
        if parts is None:
            msg = (
                "not a PostgreSQL URL: expected postgresql://[user[:password]@]"
                "[host][:port]/dbname[?param=value&...]"
            )
            raise ValueError(msg)
        _, netloc, path, query = parts

        params: dict[str, str] = {}
        userinfo, hostport = split_netloc(netloc)
        if userinfo:
            user, colon, password = userinfo.partition(":")
            if user:
                params["user"] = decode(user)
            if colon:
                params["password"] = decode(password)
        if hostport:
            pairs = [_host_port(entry) for entry in hostport.split(",")]
            if any(host for host, _ in pairs):
                params["host"] = ",".join(host for host, _ in pairs)
            if any(port for _, port in pairs):
                params["port"] = ",".join(port for _, port in pairs)
        if path:
            params["dbname"] = decode(path)

        for param in query_params(query):
            raw_key, raw_value = split_param(param)
            key, value = decode(raw_key), decode(raw_value)
            if key == "ssl" and value == "true":
                key, value = "sslmode", "require"  # libpq's JDBC compatibility
            _check_keyword(key)
            params[key] = value

        if not params.get("dbname"):
            msg = "the URL names no database: add it as the path (…/dbname)"
            raise ValueError(msg)
        return cls(params)

    def pg_env(self) -> dict[str, str]:
        """Each parameter as the ``PG*`` variable libpq reads it from."""
        return {_KEYWORD_ENV[key]: value for key, value in self.params.items()}

    def check_environment(self, environ: Mapping[str, str] = os.environ) -> None:
        """Refuse a TCP URL that *environ* sets a transport policy for.

        Raises:
            ValueError: see :func:`transport_refusal`.
        """
        refusal = transport_refusal(environ, self.pg_env())
        if refusal:
            raise ValueError(refusal)

    def child_env(self) -> dict[str, str]:
        """The environment for a child connecting with this URL.

        :func:`scrubbed` ``os.environ``, plus :meth:`pg_env`, which outranks
        the operator defaults it keeps.

        Raises:
            ValueError: :meth:`check_environment` refused it.
        """
        self.check_environment()
        return {**scrubbed(os.environ), **self.pg_env()}

    def effective_sslmode(self) -> str:
        """The sslmode a child connects with, for an operator to read."""
        if is_socket(self.pg_env()):
            return "none (socket)"
        return self.params.get("sslmode", "prefer (no certificate check)")

    @property
    def host(self) -> str | None:
        return self.params.get("host")

    @property
    def port(self) -> int | None:
        """The port, or ``None`` when there is none or more than one host."""
        port = self.params.get("port")
        return int(port) if port and "," not in port else None

    @property
    def user(self) -> str | None:
        return self.params.get("user")

    @property
    def password(self) -> str | None:
        return self.params.get("password")

    @property
    def dbname(self) -> str:
        return self.params["dbname"]
