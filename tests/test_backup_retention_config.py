"""Retention policy for a corpus a host *receives* (#339).

``backup.environments.<env>.retain`` is the first validated structure under
the top-level ``backup:`` key, which every other consumer reads raw. It is
also the only config surface whose fields reach a systemd unit file
verbatim, so its validation is a security boundary rather than a
convenience — see :class:`TestRetainValidation`.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from pathlib import Path

from fraisier.config.loader import FraisierConfig
from fraisier.errors import ValidationError

FRAISES_HEADER = """
project:
  name: my-project

scaffold:
  deploy_user: fraisier

fraises:
  api:
    type: api
    environments:
      development:
        app_path: /var/app/api
        git_repo: /srv/git/api.git
"""

REQUIRED = {
    "dir": "/backup/production",
    "retention_days": 3,
    "schedule": "*-*-* 05:30:00 UTC",
}
"""The fields with no default. Everything else the plan gives one."""


def write_config(tmp_path: Path, backup_block: str = "") -> FraisierConfig:
    """Write a fraises.yaml carrying a literal *backup_block* and load it."""
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(FRAISES_HEADER + textwrap.dedent(backup_block))
    return FraisierConfig(str(cfg))


def write_entries(tmp_path: Path, *entries: dict, env: str = "development"):
    """Load a config whose *env* declares *entries*, serialized from dicts.

    Going through ``yaml.safe_dump`` rather than string interpolation is what
    lets a payload carrying a newline, a ``%`` or a ``$`` reach the validator
    verbatim — which is the input those tests exist to refuse.
    """
    block = yaml.safe_dump({"backup": {"environments": {env: {"retain": [*entries]}}}})
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(FRAISES_HEADER + "\n" + block)
    return FraisierConfig(str(cfg))


ONE_ENTRY = """
backup:
  environments:
    development:
      retain:
        - dir: /backup/production
          match: "*_full_*.dump"
          retention_days: 3
          keep_minimum: 3
          schedule: "*-*-* 05:30:00 UTC"
          name: production-full
          user: postgres
"""


class TestRetainParsing:
    """Cycle 6.1 — the policy is readable before anything renders a unit."""

    def test_retain_entries_parse_with_defaults(self, tmp_path):
        """The four optional fields default; the rest are declared."""
        config = write_entries(tmp_path, dict(REQUIRED))
        (entry,) = config.retain_entries("development")

        assert entry.dir == "/backup/production"
        assert entry.retention_days == 3
        assert entry.schedule == "*-*-* 05:30:00 UTC"
        # The four defaults, in the order the plan names them.
        assert entry.keep_minimum == 3
        assert entry.match == "*.dump"
        assert entry.user == "fraisier"
        assert entry.name == "production"

    def test_declared_fields_win_over_every_default(self, tmp_path):
        config = write_config(tmp_path, ONE_ENTRY)
        (entry,) = config.retain_entries("development")

        assert entry.name == "production-full"
        assert entry.match == "*_full_*.dump"
        assert entry.keep_minimum == 3
        assert entry.user == "postgres"

    def test_user_defaults_to_scaffold_deploy_user(self, tmp_path):
        """The corpus is usually owned by whoever fraisier deploys as."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            FRAISES_HEADER.replace("deploy_user: fraisier", "deploy_user: deployer")
            + "\n"
            + yaml.safe_dump(
                {"backup": {"environments": {"development": {"retain": [REQUIRED]}}}}
            )
        )
        (entry,) = FraisierConfig(str(cfg)).retain_entries("development")
        assert entry.user == "deployer"

    def test_retention_hours_is_derived_from_retention_days(self, tmp_path):
        """`cleanup_old_backups` takes hours; the config surface speaks days."""
        config = write_entries(tmp_path, {**REQUIRED, "retention_days": 3})
        (entry,) = config.retain_entries("development")
        assert entry.retention_hours == 72

    def test_absent_backup_key_yields_no_retention(self, tmp_path):
        config = write_config(tmp_path)
        assert config.retain_entries("development") == ()

    def test_backup_key_without_environments_yields_no_retention(self, tmp_path):
        """Every `backup:` block written before #339 is exactly this shape."""
        config = write_config(
            tmp_path,
            """
            backup:
              compression: "zstd:9"
              destinations:
                - name: local
                  path: /backup/my-project
            """,
        )
        assert config.retain_entries("development") == ()

    def test_an_environment_with_no_retain_block_yields_nothing(self, tmp_path):
        config = write_config(
            tmp_path,
            """
            backup:
              environments:
                development: {}
            """,
        )
        assert config.retain_entries("development") == ()

    def test_entries_carry_their_environment(self, tmp_path):
        """The unit's ExecStart selector is (env, name); both come from here."""
        config = write_config(tmp_path, ONE_ENTRY)
        (entry,) = config.retain_entries("development")
        assert entry.environment == "development"

    def test_an_environment_with_no_policy_yields_nothing(self, tmp_path):
        """Distinct from a *declared* block naming an environment no fraise has.

        That one is rejected — see
        :meth:`TestRetainValidation.test_unknown_environment_is_rejected`.
        Simply asking about an environment with no policy is not an error.
        """
        config = write_config(tmp_path, ONE_ENTRY)
        assert config.retain_entries("production") == ()

    def test_all_retain_entries_spans_every_environment(self, tmp_path):
        """The renderer needs every entry, not one environment's worth."""
        config = write_entries(
            tmp_path,
            dict(REQUIRED),
            {**REQUIRED, "dir": "/backup/staging", "retention_days": 7},
        )
        assert [e.name for e in config.all_retain_entries()] == [
            "production",
            "staging",
        ]


class TestRetainSectionWiring:
    """The section behaves like every other Stage-2 section, not beside them."""

    def test_reload_drops_the_cached_entries(self, tmp_path):
        """A cached_property survives `reload()` unless `_load` clears it."""
        config = write_entries(tmp_path, dict(REQUIRED))
        assert config.retain_entries("development")[0].retention_days == 3

        (tmp_path / "fraises.yaml").write_text(
            FRAISES_HEADER
            + "\n"
            + yaml.safe_dump(
                {
                    "backup": {
                        "environments": {
                            "development": {
                                "retain": [{**REQUIRED, "retention_days": 9}]
                            }
                        }
                    }
                }
            )
        )
        config.reload()
        assert config.retain_entries("development")[0].retention_days == 9

    def test_validate_surfaces_an_invalid_retain_block(self, tmp_path):
        """`fraisier validate` is the "every problem at once" entry point.

        A lazy section not listed there is a section an operator only
        discovers at scaffold time, on the host.
        """
        from click.testing import CliRunner

        from fraisier.cli.main import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            FRAISES_HEADER
            + "\n"
            + yaml.safe_dump(
                {
                    "backup": {
                        "environments": {
                            "development": {
                                "retain": [{**REQUIRED, "schedule": "every tuesday"}]
                            }
                        }
                    }
                }
            )
        )
        result = CliRunner().invoke(main, ["-c", str(cfg), "validate"])

        assert result.exit_code != 0, result.output
        assert "retain[0].schedule" in result.output


class TestRetainValidation:
    """Cycle 6.2 — every field reaching a unit file is checked, loudly.

    Each rejection names the YAML path of the offending entry, because an
    operator reading "invalid schedule" against a forty-fraise config has
    learned nothing.
    """

    def _reject(self, tmp_path, *entries: dict, env: str = "development") -> str:
        with pytest.raises(ValidationError) as exc:
            write_entries(tmp_path, *entries, env=env).retain_entries(env)
        return str(exc.value)

    def test_unknown_environment_is_rejected(self, tmp_path):
        """Units for an environment no fraise declares would never activate.

        `_env_active` means "a fraise on this host declares this env" (#336).
        A retain block for `staging` when no fraise has a `staging` renders a
        timer that is copied, gated, and never fires — the incident's own
        failure mode reproduced inside the system built to prevent it.
        """
        message = self._reject(tmp_path, dict(REQUIRED), env="staging")
        assert "backup.environments.staging" in message
        assert "development" in message  # the known names are listed

    def test_relative_dir_is_rejected(self, tmp_path):
        message = self._reject(tmp_path, {**REQUIRED, "dir": "backup/production"})
        assert "backup.environments.development.retain[0].dir" in message
        assert "absolute" in message

    @pytest.mark.parametrize(
        "bad", ["/backup/$(whoami)", "/backup/a;rm -rf /", "/backup/../etc"]
    )
    def test_dir_with_shell_metacharacters_is_rejected(self, tmp_path, bad):
        message = self._reject(tmp_path, {**REQUIRED, "dir": bad})
        assert "backup.environments.development.retain[0].dir" in message

    @pytest.mark.parametrize(
        ("field", "payload"),
        [
            ("dir", "/backup/prod\nExecStartPost=/bin/rm -rf /"),
            ("match", "*.dump\nExecStartPost=/bin/false"),
            ("schedule", "*-*-* 05:30:00\nOnCalendar=minutely"),
            ("name", "prod\nAlias=evil"),
            ("user", "root\nExecStart=/bin/sh"),
        ],
    )
    def test_newline_in_any_field_is_rejected(self, tmp_path, field, payload):
        """A newline reaching a unit file is arbitrary-directive injection.

        This is the security boundary of the retention half: `dir`, `match`
        and `user` are interpolated into `ReadWritePaths=`/`User=`, and
        `schedule` into `OnCalendar=`. One newline appends a directive
        systemd honours as readily as the ones fraisier wrote.
        """
        message = self._reject(tmp_path, {**REQUIRED, field: payload})
        assert f"retain[0].{field}" in message

    @pytest.mark.parametrize(
        ("field", "payload"),
        [
            ("dir", "/backup/%H"),
            ("match", "*%i*.dump"),
            ("name", "prod%n"),
            ("user", "user%%"),
        ],
    )
    def test_percent_specifiers_are_rejected(self, tmp_path, field, payload):
        """systemd expands `%H`, `%i`, `%n` … inside a unit file.

        An unescaped `%` means the path fraisier validated is not the path
        the unit acts on, which defeats the point of validating it.
        """
        message = self._reject(tmp_path, {**REQUIRED, field: payload})
        assert f"retain[0].{field}" in message

    @pytest.mark.parametrize("bad", ["prod duction", "prod/uction", "prod%n", "prod$X"])
    def test_name_must_be_a_safe_identifier(self, tmp_path, bad):
        """`name` becomes a unit filename and a `--name` argv element."""
        message = self._reject(tmp_path, {**REQUIRED, "name": bad})
        assert "backup.environments.development.retain[0].name" in message

    def test_a_derived_name_must_also_be_safe(self, tmp_path):
        """The basename default is not exempt from the identifier rule."""
        message = self._reject(tmp_path, {**REQUIRED, "dir": "/backup/prod.v2"})
        assert "backup.environments.development.retain[0]" in message
        assert "name:" in message  # tells the operator to declare one

    def test_duplicate_entry_names_in_one_environment_are_rejected_naming_both(
        self, tmp_path
    ):
        """Two dirs can share a basename; the derived names would collide.

        Not auto-disambiguated with a hash: unit names must never change
        silently underneath an operator.
        """
        message = self._reject(
            tmp_path,
            dict(REQUIRED),
            {**REQUIRED, "dir": "/archive/production", "retention_days": 9},
        )
        assert "retain[0]" in message
        assert "retain[1]" in message
        assert "production" in message

    @pytest.mark.parametrize("bad", [0, -1])
    def test_retention_days_must_be_positive(self, tmp_path, bad):
        message = self._reject(tmp_path, {**REQUIRED, "retention_days": bad})
        assert "retain[0].retention_days" in message

    def test_keep_minimum_must_be_non_negative(self, tmp_path):
        message = self._reject(tmp_path, {**REQUIRED, "keep_minimum": -1})
        assert "retain[0].keep_minimum" in message

    @pytest.mark.parametrize(
        "bad", ["every tuesday", "05:30; rm -rf /", "*-*-* 05:30:00 UTC$(id)"]
    )
    def test_schedule_must_look_like_an_oncalendar_expression(self, tmp_path, bad):
        """Conservative regex only.

        `systemd-analyze calendar` stays the on-host authority; shelling out
        to it at config load would make every `fraisier` invocation on a
        developer laptop depend on systemd being installed.
        """
        message = self._reject(tmp_path, {**REQUIRED, "schedule": bad})
        assert "retain[0].schedule" in message

    @pytest.mark.parametrize(
        "good",
        ["daily", "hourly", "weekly", "*-*-* 05:30:00 UTC", "Mon *-*-* 05:30:00"],
    )
    def test_conventional_schedules_are_accepted(self, tmp_path, good):
        config = write_entries(tmp_path, {**REQUIRED, "schedule": good})
        (entry,) = config.retain_entries("development")
        assert entry.schedule == good

    @pytest.mark.parametrize("bad", ["root; sh", "fraisier user", "-rf"])
    def test_user_must_be_a_safe_identifier(self, tmp_path, bad):
        message = self._reject(tmp_path, {**REQUIRED, "user": bad})
        assert "retain[0].user" in message

    @pytest.mark.parametrize("field", ["dir", "retention_days", "schedule"])
    def test_required_fields_are_required(self, tmp_path, field):
        entry = {k: v for k, v in REQUIRED.items() if k != field}
        message = self._reject(tmp_path, entry)
        assert f"retain[0].{field}" in message

    def test_retain_must_be_a_list(self, tmp_path):
        block = yaml.safe_dump(
            {"backup": {"environments": {"development": {"retain": dict(REQUIRED)}}}}
        )
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(FRAISES_HEADER + "\n" + block)
        with pytest.raises(ValidationError) as exc:
            FraisierConfig(str(cfg)).retain_entries("development")
        assert "backup.environments.development.retain" in str(exc.value)

    @pytest.mark.parametrize(
        "bad", ["../staging/*.dump", "/etc/*.dump", "sub/../*.dump"]
    )
    def test_match_must_not_escape_the_directory(self, tmp_path, bad):
        """A `match` of `../*` globs a tree `ReadWritePaths=` never granted.

        The prune would fail at runtime rather than delete the wrong thing,
        but a policy that cannot do what it says is the failure mode this
        release exists to remove.
        """
        message = self._reject(tmp_path, {**REQUIRED, "match": bad})
        assert "retain[0].match" in message
