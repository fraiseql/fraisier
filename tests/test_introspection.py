"""Subcommand → config-section map + LazyEnv walker (#221 bundle B phase 01).

Foundation for the rest of bundle B: an internal API that, given a
parsed fraises.yaml and a subcommand name, answers "which ``!envvar``
references would this subcommand reach into?" without actually running
the subcommand.

Two layers:

1. Static ``SUBCOMMAND_CONFIG_SECTIONS`` map declares which top-level
   config sections each subcommand materializes.
2. Dynamic walker: ``reachable_envvars(config, subcommand)`` walks the
   declared sections of a parsed config and returns every reachable
   ``LazyEnv`` as ``EnvVarRef(name, yaml_path, is_set)``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fraisier.config._lazy_env import LazyEnv
from fraisier.introspection import (
    COMMANDS_WITHOUT_CONFIG_ACCESS,
    SUBCOMMAND_CONFIG_SECTIONS,
    ConfigPath,
    EnvVarRef,
    reachable_envvars,
)


class TestConfigPath:
    def test_literal_path_matches(self):
        p = ConfigPath("environments.production.database.post_migrate")
        assert p.matches("environments.production.database.post_migrate")
        assert not p.matches("environments.production.database")

    def test_glob_matches_single_segment(self):
        p = ConfigPath("environments.*.database.post_migrate")
        assert p.matches("environments.production.database.post_migrate")
        assert p.matches("environments.staging.database.post_migrate")
        assert not p.matches("environments.production.smoke_tests")

    def test_glob_matches_nested_prefix(self):
        # A ConfigPath of "environments.*" should match its declared
        # subtree (prefix match).
        p = ConfigPath("environments.*")
        assert p.matches("environments.production")
        assert p.matches("environments.staging.database")
        assert p.matches("environments.staging.smoke_tests[0].url")


class TestReachableEnvvarsWalker:
    def test_finds_envvar_under_declared_section(self, monkeypatch):
        monkeypatch.delenv("DB_URL", raising=False)
        config = {
            "fraises": {
                "my_api": {
                    "environments": {
                        "production": {
                            "database": {
                                "database_url": LazyEnv(
                                    "DB_URL",
                                    "fraises.my_api.environments.production.database.database_url",
                                ),
                            },
                        },
                    },
                },
            },
        }
        refs = reachable_envvars(
            config, "trigger-deploy", fraise="my_api", environment="production"
        )
        names = {r.name for r in refs}
        assert "DB_URL" in names
        for r in refs:
            if r.name == "DB_URL":
                assert r.is_set is False
                assert "database_url" in r.yaml_path

    def test_finds_envvar_in_nested_list(self, monkeypatch):
        monkeypatch.setenv("SMOKE_JWT", "x")
        config = {
            "fraises": {
                "api": {
                    "environments": {
                        "production": {
                            "smoke_tests": [
                                {
                                    "headers": {
                                        "Authorization": LazyEnv(
                                            "SMOKE_JWT",
                                            "fraises.api.environments.production.smoke_tests[0].headers.Authorization",
                                        ),
                                    },
                                },
                            ],
                        },
                    },
                },
            },
        }
        refs = reachable_envvars(
            config, "trigger-deploy", fraise="api", environment="production"
        )
        assert any(r.name == "SMOKE_JWT" and r.is_set is True for r in refs)

    def test_skips_sections_not_declared_for_subcommand(self, monkeypatch):
        # The "ship" subcommand declares only the ship + git sections; a
        # LazyEnv under environments.*.database is unreachable from it.
        monkeypatch.delenv("DB_URL", raising=False)
        config = {
            "ship": {"pr_base": LazyEnv("PR_BASE", "ship.pr_base")},
            "fraises": {
                "api": {
                    "environments": {
                        "production": {
                            "database": {
                                "database_url": LazyEnv("DB_URL", "fraises..."),
                            },
                        },
                    },
                },
            },
        }
        refs = reachable_envvars(config, "ship")
        names = {r.name for r in refs}
        assert "PR_BASE" in names
        assert "DB_URL" not in names

    def test_returns_empty_when_subcommand_declares_no_sections(self):
        refs = reachable_envvars({}, "version show")
        assert refs == []

    def test_does_not_resolve_lazy_env(self, monkeypatch):
        # Sanity: walker checks os.environ via name lookup but never
        # calls LazyEnv.resolve() (which would leak the secret in
        # logs and raise on missing).
        monkeypatch.setenv("PRESENT", "leak-me-if-you-can")
        config = {
            "ship": {"pr_base": LazyEnv("PRESENT", "ship.pr_base")},
        }
        refs = reachable_envvars(config, "ship")
        ref = next(r for r in refs if r.name == "PRESENT")
        assert ref.is_set is True
        # The walker never stores the resolved value.
        assert not any("leak-me" in str(field) for field in ref)


class TestSubcommandSectionMap:
    """Hand-curated map covers every registered CLI command."""

    def test_every_main_command_is_declared_or_allowlisted(self):
        from fraisier.cli.main import main as main_group

        def _leaves(group, prefix=()):
            for name, cmd in group.commands.items():
                path = (*prefix, name)
                if hasattr(cmd, "commands"):
                    yield from _leaves(cmd, path)
                else:
                    yield " ".join(path)

        for cmd_name in _leaves(main_group):
            assert (
                cmd_name in SUBCOMMAND_CONFIG_SECTIONS
                or cmd_name in COMMANDS_WITHOUT_CONFIG_ACCESS
            ), (
                f"Command {cmd_name!r} is registered but missing from both "
                f"SUBCOMMAND_CONFIG_SECTIONS and COMMANDS_WITHOUT_CONFIG_ACCESS"
            )

    def test_no_command_in_both_maps(self):
        overlap = SUBCOMMAND_CONFIG_SECTIONS.keys() & COMMANDS_WITHOUT_CONFIG_ACCESS
        assert not overlap, (
            f"Commands declared in both maps (pick one): {sorted(overlap)}"
        )


class TestDriftGuard:
    """Section map references only top-level keys that exist in
    fraises.example.yaml — catches typos and stale entries when keys are
    renamed."""

    @pytest.fixture(scope="class")
    def example_top_keys(self) -> frozenset[str]:
        import yaml

        from fraisier.config.loader import _FraisierYamlLoader

        text = (Path(__file__).parent.parent / "fraises.example.yaml").read_text()
        loaded = yaml.load(text, Loader=_FraisierYamlLoader) or {}
        fraises = loaded.get("fraises", {}) or {}
        env_keys: set[str] = set()
        for fraise in fraises.values():
            envs = (fraise or {}).get("environments", {}) or {}
            for env in envs.values():
                env_keys.update((env or {}).keys())
        env_keys.update(loaded.keys())
        # Augment with valid keys the example yaml documents only in
        # commented-out blocks (smoke_tests, post_migrate) or that the
        # loader exposes as properties without an example entry
        # (ship, scaffold, hooks, git, webhook, servers).
        env_keys.update(
            {
                "ship",
                "scaffold",
                "hooks",
                "git",
                "webhook",
                "servers",
                "smoke_tests",
                "post_migrate",
                "ssh",
            }
        )
        return frozenset(env_keys)

    def test_section_map_first_segments_are_grounded(self, example_top_keys):
        # The walker handles `environments.*.X` by indexing into
        # environments. The grounded check is on the literal first
        # segment of each ConfigPath. `*` is the full-config wildcard
        # used by `validate` / `scaffold` and is exempt.
        for cmd, paths in SUBCOMMAND_CONFIG_SECTIONS.items():
            for cp in paths:
                first = cp.parts[0]
                if first == "*":
                    continue  # full-config wildcard, no segment to ground
                segments = cp.parts
                if first == "environments" and len(segments) >= 3:
                    env_key = segments[2]
                    assert env_key in example_top_keys, (
                        f"Subcommand {cmd!r} declares "
                        f"environments.*.{env_key} but {env_key!r} is "
                        f"not present in fraises.example.yaml's "
                        f"environments. Stale entry?"
                    )
                elif first != "environments":
                    assert first in example_top_keys, (
                        f"Subcommand {cmd!r} declares top-level section "
                        f"{first!r} but it's not in fraises.example.yaml"
                    )


def test_envvarref_is_namedtuple_with_fields():
    # EnvVarRef is a NamedTuple — locked so consumers can destructure it.
    ref = EnvVarRef(name="X", yaml_path="a.b", is_set=False)
    name, yaml_path, is_set = ref
    assert name == "X"
    assert yaml_path == "a.b"
    assert is_set is False
