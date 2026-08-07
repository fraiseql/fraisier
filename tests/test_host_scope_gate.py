"""A host installs the units of the fraises that run on it, and only those.

``machine_env_map`` — the authority the installer's gate read — was built
from environment *names*, with the declaring fraise thrown away. So two
fraises putting the same environment name on different servers made each
host see that name as active: each installed the other's units and created
the other's directories (#336).

The fix is two predicates, because there are genuinely two kinds of
artifact. ``_scope_active <fraise> <env>`` gates what one fraise owns —
app services, deploy sockets, install-helper pairs, nginx vhosts, managed
paths. ``_env_active <env>`` gates what no single fraise owns: the
unit-installer helper is one per (project, environment) by design (#240),
and the postgresql logging conf is per environment. Forcing those through
a fraise-keyed gate would mean inventing an owner for them, which is how a
second host authority gets born.

Exercised by running the real rendered ``install.sh --dry-run`` for a given
hostname, so what is asserted is what a host would actually do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.test_install_plan_golden import (
    _SCOPED_HOSTS as SAME_NAME_DIFFERENT_SERVER,
)
from tests.test_install_plan_golden import _install_plan, _render

if TYPE_CHECKING:
    from pathlib import Path

# The same two fraises, but `server:` declared once in the global
# environments: section. That declaration has no owning fraise and binds
# every fraise using the name — so both fraises are local to the one host,
# exactly as before the fix.
GLOBAL_DECLARATION = """\
name: proj
servers:
  shared.example.io:
    machine_hostnames: [shared]
scaffold:
  deploy_user: deployer
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
  worker:
    type: api
    environments:
      production:
        app_path: /var/www/worker
        systemd_service: worker.service
        git_repo: /var/git/worker.git
environments:
  production:
    server: shared.example.io
"""


@pytest.fixture(scope="module")
def abox(tmp_path_factory) -> list[str]:
    tmp = tmp_path_factory.mktemp("abox")
    return _install_plan(tmp, SAME_NAME_DIFFERENT_SERVER, "abox")


@pytest.fixture(scope="module")
def bbox(tmp_path_factory) -> list[str]:
    tmp = tmp_path_factory.mktemp("bbox")
    return _install_plan(tmp, SAME_NAME_DIFFERENT_SERVER, "bbox")


@pytest.fixture(scope="module")
def cbox(tmp_path_factory) -> list[str]:
    tmp = tmp_path_factory.mktemp("cbox")
    return _install_plan(tmp, SAME_NAME_DIFFERENT_SERVER, "cbox")


@pytest.fixture(scope="module")
def shared(tmp_path_factory) -> list[str]:
    tmp = tmp_path_factory.mktemp("shared")
    return _install_plan(tmp, GLOBAL_DECLARATION, "shared")


class TestScopeActive:
    """Fraise-owned artifacts reach the host their fraise runs on."""

    def test_scope_active_matches_only_the_declaring_fraise(self, abox, bbox):
        a = " ".join(abox)
        b = " ".join(bbox)

        assert "api.service" in a
        assert "worker.service" not in a, (
            "box-a installed the unit of a fraise that does not run on it"
        )
        assert "worker.service" in b
        assert "api.service" not in b

    def test_deploy_sockets_follow_their_fraise(self, abox):
        a = " ".join(abox)

        assert "fraisier-api-production" in a
        assert "fraisier-worker-production" not in a

    def test_hosts_do_not_provision_each_others_directories_under_a_shared_name(
        self, abox
    ):
        """The directory gate follows for free — asserted, not trusted.

        The pin this replaces predicted it: gate the units by (fraise, env)
        and the paths derived from the same declaration follow. That is a
        prediction about code neither test can see, so it is checked.
        """
        touched = [c for c in abox if any(v in c for v in ("mkdir", "chown", "chmod"))]
        joined = " ".join(touched)

        assert "/var/www/api" in joined
        assert "/var/www/worker" not in joined, (
            "box-a created the directories of a fraise that does not run on it"
        )
        assert "/var/git/worker.git" not in joined

    def test_shared_directories_still_have_no_owner(self, abox):
        joined = " ".join(abox)

        assert "/var/lib/fraisier" in joined
        assert "/opt/fraisier" in joined


class TestEnvActive:
    """Artifacts no fraise owns are gated by environment, on purpose."""

    def test_env_active_means_a_local_fraise_declares_this_env(self, abox, bbox, cbox):
        """The unit-installer helper is one per (project, environment) (#240).

        It is env-owned on purpose. Box-a and box-b both declare
        ``production`` — through different fraises — so both install the
        ``production`` helper, and that is not the bug #336 describes: the
        helper is a single shared artifact, not one host's copy of the
        other's unit. Box-c declares only ``staging``, so it installs none,
        which is what makes this a gate rather than an unconditional copy.
        """
        assert [c for c in abox if "unit-installer" in c]
        assert [c for c in bbox if "unit-installer" in c]
        assert not [c for c in cbox if "unit-installer" in c]

    def test_the_helper_is_not_keyed_to_one_fraise(self, bbox):
        """Its name carries the project and env, never a fraise."""
        helper = [c for c in bbox if "unit-installer" in c]

        assert helper
        for command in helper:
            assert "fraisier-proj-production-unit-installer" in command


class TestGlobalDeclarationsAreUnaffected:
    """The claim that halves the blast radius, asserted rather than assumed."""

    def test_a_globally_declared_environment_binds_every_fraise(self, shared):
        joined = " ".join(shared)

        assert "api.service" in joined
        assert "worker.service" in joined

    def test_and_provisions_every_fraise_directory(self, shared):
        joined = " ".join(shared)

        assert "/var/www/api" in joined
        assert "/var/www/worker" in joined


class TestTheGateStillFailsLoudly:
    """The rewrite must not soften the unregistered-machine error."""

    def test_unregistered_machine_still_fails_loudly(self, tmp_path):
        import os
        import subprocess

        out = _render(tmp_path, SAME_NAME_DIFFERENT_SERVER)
        install_sh = out / "install.sh"
        install_sh.chmod(0o755)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        fake = bin_dir / "hostname"
        fake.write_text("#!/bin/bash\necho stranger\n")
        fake.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

        result = subprocess.run(
            ["bash", str(install_sh), "--dry-run", "--scaffold-dir", str(out)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode != 0
        assert "not registered in fraises.yaml" in result.stderr
        assert "abox" in result.stderr and "bbox" in result.stderr


class TestPairKeysReachingBashAreValidated:
    """`fraise:env` is a new composite entering a shell string."""

    def test_a_name_with_a_separator_is_refused(self, tmp_path: Path):
        from fraisier.config import FraisierConfig
        from fraisier.scaffold.renderer import ScaffoldRenderer

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            SAME_NAME_DIFFERENT_SERVER.replace("  worker:\n", "  wo:rker:\n")
        )
        config = FraisierConfig(cfg)

        with pytest.raises(ValueError, match="a-zA-Z0-9_-"):
            ScaffoldRenderer(config)

    def test_an_environment_name_with_a_separator_is_refused(self, tmp_path: Path):
        """Also from the global section, which no other check walks."""
        from fraisier.config import FraisierConfig
        from fraisier.scaffold.renderer import ScaffoldRenderer

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            GLOBAL_DECLARATION.replace(
                "environments:\n  production:\n    server: shared.example.io",
                "environments:\n  prod uction:\n    server: shared.example.io",
            )
        )
        config = FraisierConfig(cfg)

        with pytest.raises(ValueError, match="a-zA-Z0-9_-"):
            ScaffoldRenderer(config)


class _NullRunner:
    """ServerSetup requires a runner; these tests never reach one."""

    def run(self, cmd, **kwargs):  # pragma: no cover - never called
        raise AssertionError(f"unexpected command: {cmd}")


class TestTheOtherHostFilters:
    """Everything else that decides what a host acts on, keyed the same way.

    ``install.sh`` is not the only consumer that answers "is this mine?".
    ``setup`` creates users, chowns trees and enables units; ``doctor``
    decides which trees this host's webhook must be able to write. Both
    filtered on the environment name, so both inherited #336 — and after
    the installer was fixed, doctor's copy would have called a correctly
    scoped webhook unit broken for not granting a neighbour's trees.
    """

    @staticmethod
    def _config(tmp_path):
        from fraisier.config import FraisierConfig

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(SAME_NAME_DIFFERENT_SERVER)
        return FraisierConfig(cfg)

    def test_doctor_hosted_trees_are_this_hosts_only(self, tmp_path):
        from fraisier.doctor import _hosted_trees

        trees = _hosted_trees(self._config(tmp_path), "a.example.io")

        assert "/var/www/api" in trees
        assert "/var/www/worker" not in trees
        assert "/var/www/nightly" not in trees

    def test_setup_iterates_only_this_hosts_environments(self, tmp_path):
        from fraisier.setup import ServerSetup

        setup = ServerSetup(
            self._config(tmp_path), _NullRunner(), server="a.example.io"
        )
        pairs = {(f, e) for f, e, _ in setup._iter_fraise_environments()}

        assert pairs == {("api", "production")}

    def test_setup_still_honours_an_explicit_environment(self, tmp_path):
        """``--environment`` is an operator's answer, not a host derivation."""
        from fraisier.setup import ServerSetup

        setup = ServerSetup(
            self._config(tmp_path), _NullRunner(), environment="staging"
        )
        pairs = {(f, e) for f, e, _ in setup._iter_fraise_environments()}

        assert pairs == {("edge", "staging")}

    def test_setup_still_refuses_a_server_hosting_nothing(self, tmp_path):
        from fraisier.errors import ValidationError
        from fraisier.setup import ServerSetup

        setup = ServerSetup(
            self._config(tmp_path), _NullRunner(), server="nowhere.example.io"
        )

        with pytest.raises(ValidationError):
            list(setup._iter_fraise_environments())
