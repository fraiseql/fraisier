"""``PgConn``'s keyword table is the installed libpq's, version-tolerantly.

The table is static (copied from libpq 18's ``PQconndefaults()``), so this
reads the installed libpq and fails when the two disagree: a keyword libpq
gained that fraisier would neither map nor refuse, or an envvar that moved.
"""

from __future__ import annotations

import ctypes
import ctypes.util

import pytest

from fraisier.dbops._pgconn import _KEYWORD_ENV, _NO_ENV

#: Keywords libpq 18 added (captured: Ubuntu 24.04's libpq 16.15 lacks
#: exactly these). An older libpq may lack them; it must lack nothing else.
_ADDED_IN_18 = frozenset(
    {
        "sslnegotiation",
        "min_protocol_version",
        "max_protocol_version",
        "scram_client_key",
        "scram_server_key",
        "oauth_issuer",
        "oauth_client_id",
        "oauth_client_secret",
        "oauth_scope",
        "sslkeylogfile",
    }
)


class _Option(ctypes.Structure):
    _fields_ = [
        ("keyword", ctypes.c_char_p),
        ("envvar", ctypes.c_char_p),
        ("compiled", ctypes.c_char_p),
        ("val", ctypes.c_char_p),
        ("label", ctypes.c_char_p),
        ("dispchar", ctypes.c_char_p),
        ("dispsize", ctypes.c_int),
    ]


def _installed() -> tuple[int, dict[str, str | None]]:
    name = ctypes.util.find_library("pq")
    if name is None:
        pytest.skip("no libpq shared library on this machine")  # ty: ignore[too-many-positional-arguments]
    lib = ctypes.CDLL(name)
    lib.PQlibVersion.restype = ctypes.c_int
    lib.PQconndefaults.restype = ctypes.POINTER(_Option)
    lib.PQconninfoFree.argtypes = [ctypes.POINTER(_Option)]
    options = lib.PQconndefaults()
    table: dict[str, str | None] = {}
    try:
        i = 0
        while options[i].keyword:
            envvar = options[i].envvar
            table[options[i].keyword.decode()] = envvar.decode() if envvar else None
            i += 1
    finally:
        lib.PQconninfoFree(options)
    return lib.PQlibVersion(), table


def test_every_installed_keyword_is_mapped_or_refused_identically():
    _, installed = _installed()
    ours = {**dict.fromkeys(_NO_ENV), **_KEYWORD_ENV}

    assert {k: v for k, v in installed.items() if ours.get(k, "?") != v} == {}


def test_the_installed_libpq_lacks_only_keywords_added_in_18():
    version, installed = _installed()
    allowed_missing = _ADDED_IN_18 if version < 180000 else frozenset()

    missing = (_KEYWORD_ENV.keys() | _NO_ENV) - installed.keys()

    assert missing <= allowed_missing
