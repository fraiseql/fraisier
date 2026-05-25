"""Phase 3 — config validators tolerate LazyEnv (#220).

Stage-2 validators (those that run lazily behind ``@cached_property``
or per-(fraise, env) caching) historically used ``isinstance(x, str)``
to type-check string-typed YAML fields, which rejected ``LazyEnv``
placeholders and defeated lazy resolution.

After Phase 3 they widen to ``is_string_like()`` and defer
content-shape checks (URL schemes, unit-name regexes, enum
membership) past the validator boundary — consumers re-run the check
after ``to_str()``.
"""

from __future__ import annotations

import pytest

from fraisier.config import FraisierConfig
from fraisier.config._lazy_env import LazyEnv
from fraisier.config._validation import (
    _validate_pg_url,
    validate_pg_url_string,
)
from fraisier.errors import ValidationError


def _write_config(tmp_path, content):
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(content)
    return config_file


class TestValidatePgUrl:
    def test_str_pg_url_passes(self):
        assert _validate_pg_url("api", "database_url", "postgresql://x/db") == []

    def test_str_non_pg_url_fails(self):
        errors = _validate_pg_url("api", "database_url", "mysql://x/db")
        assert errors and "PostgreSQL URL" in errors[0]

    def test_lazyenv_defers_scheme_check(self, monkeypatch):
        # No resolution, no validation: load-time tolerates the placeholder.
        monkeypatch.delenv("DB", raising=False)
        assert _validate_pg_url("api", "database_url", LazyEnv("DB", "p")) == []

    def test_validate_pg_url_string_helper_runs_scheme_check(self):
        assert validate_pg_url_string("api", "database_url", "postgresql://x") == []
        errors = validate_pg_url_string("api", "database_url", "mysql://x")
        assert errors and "PostgreSQL URL" in errors[0]


class TestPgUrlInYaml:
    _TEMPLATE = """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        database:
          database_url: {expr}
"""

    def test_unset_envvar_database_url_loads_and_validates(
        self, tmp_path, monkeypatch
    ):
        # !envvar DB unset → load OK, get_fraise_environment OK.
        # No URL scheme check fires at validation time.
        monkeypatch.delenv("DB", raising=False)
        config_file = _write_config(
            tmp_path,
            self._TEMPLATE.format(expr="!envvar DB"),
        )
        config = FraisierConfig(config_file)
        fraise = config.get_fraise_environment("my_api", "production")
        assert isinstance(fraise["database"]["database_url"], LazyEnv)

    def test_envvar_set_to_non_pg_url_fails_at_resolved_scheme_check(
        self, tmp_path, monkeypatch
    ):
        # Once the operator wires up a consumer-side validate_pg_url_string
        # over the resolved value, a bad URL surfaces there. That helper
        # is what _validate_pg_url defers to.
        monkeypatch.setenv("DB", "mysql://nope")
        config_file = _write_config(
            tmp_path,
            self._TEMPLATE.format(expr="!envvar DB"),
        )
        config = FraisierConfig(config_file)
        # Load + Stage-2 validation tolerates LazyEnv presence; the
        # scheme check must be runnable on the resolved value by callers.
        fraise = config.get_fraise_environment("my_api", "production")
        resolved = str(fraise["database"]["database_url"])
        errors = validate_pg_url_string("my_api", "database_url", resolved)
        assert errors and "PostgreSQL URL" in errors[0]

    def test_str_database_url_with_non_pg_scheme_still_fails_at_load(
        self, tmp_path
    ):
        # Plain (non-!envvar) string values still get the scheme check.
        config_file = _write_config(
            tmp_path,
            self._TEMPLATE.format(expr="mysql://nope"),
        )
        config = FraisierConfig(config_file)
        with pytest.raises(ValidationError, match=r"PostgreSQL URL"):
            config.get_fraise_environment("my_api", "production")
