"""LazyEnv safety in CLI JSON output (#220 Phase 5 Cycle 5.4).

CLI commands serialize diagnostic data with ``json.dumps``. A bare
``LazyEnv`` reaching ``json.dumps`` would either: (a) ``TypeError`` (no
encoder), (b) call ``default=str`` and resolve to the secret in plain
text, or (c) call ``default=repr`` and emit the bare repr including
``yaml_path``. None of those is acceptable for diagnostic output.

``fraisier.cli._json.dumps`` wraps ``json.dumps`` with an encoder that
substitutes a ``"<envvar:NAME>"`` placeholder for any reachable
``LazyEnv`` instance — never resolving the variable, never leaking
the secret.
"""

from __future__ import annotations

import json

import pytest

from fraisier.cli._json import dumps
from fraisier.config._lazy_env import LazyEnv


class TestDumpsLazyEnv:
    def test_plain_data_unchanged(self):
        out = dumps({"a": 1, "b": "x", "c": [1, 2, 3]})
        assert json.loads(out) == {"a": 1, "b": "x", "c": [1, 2, 3]}

    def test_lazyenv_becomes_envvar_placeholder(self):
        # The placeholder must NOT call resolve(), even when the env
        # var happens to be set — diagnostic JSON shouldn't ever leak
        # secrets, regardless of process environment.
        import os

        os.environ["SHOULD_NOT_LEAK"] = "topsecret"
        try:
            out = dumps({"secret": LazyEnv("SHOULD_NOT_LEAK", "fraises.api.s")})
            payload = json.loads(out)
            assert payload == {"secret": "<envvar:SHOULD_NOT_LEAK>"}
            # Defense-in-depth: even the raw JSON text must not contain
            # the resolved value.
            assert "topsecret" not in out
            # And must not emit a `LazyEnv(...)` repr either.
            assert "LazyEnv(" not in out
        finally:
            os.environ.pop("SHOULD_NOT_LEAK", None)

    def test_lazyenv_nested_in_dict_in_list(self):
        out = dumps(
            {
                "checks": [
                    {"name": "auth", "value": LazyEnv("TOK", "p")},
                    {"name": "url", "value": "https://x"},
                ]
            }
        )
        payload = json.loads(out)
        assert payload["checks"][0]["value"] == "<envvar:TOK>"
        assert payload["checks"][1]["value"] == "https://x"

    def test_unset_lazyenv_placeholder_still_safe(self, monkeypatch):
        # Even unset (no resolve path attempted), placeholder is fine.
        monkeypatch.delenv("UNSET", raising=False)
        out = dumps({"x": LazyEnv("UNSET", "p")})
        assert json.loads(out) == {"x": "<envvar:UNSET>"}

    def test_indent_arg_threaded(self):
        out = dumps({"a": 1}, indent=2)
        assert "\n  " in out

    def test_non_serializable_non_lazyenv_still_raises(self):
        # The encoder ONLY handles LazyEnv; non-serializable types
        # should keep raising TypeError so we don't silently drop data.
        class Weird:
            pass

        with pytest.raises(TypeError):
            dumps({"x": Weird()})
