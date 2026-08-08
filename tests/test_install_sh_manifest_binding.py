"""install.sh refuses a scaffold tree that is not the one it came from.

The manifest removes the drift between what ``render()`` wrote and what the
installer installs. On its own that just moves the problem up a level: an old
``install.sh`` pointed at a fresh scaffold dir — or a fresh one pointed at a
stale tree — would faithfully install files nobody described. #323's triage
found exactly that shape, v1.141.0-era rendered units sitting in an app repo
shadowing the current render.

So each artifact's sha256 is baked into the generated installer, and a
mismatch is a refusal rather than a warning. A missing source is likewise
fatal: the manifest says the render produced it, so its absence means the tree
is not the tree this script was generated for. That is the same reasoning
v0.56.0 applied to the webhook unit when it deleted the ``[ -f ]`` guard whose
silent skip caused #325 — now extended to every artifact.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

_YAML = """\
name: testapp
servers:
  example.com:
    machine_hostnames: [default-testrunner]
scaffold:
  deploy_user: testapp_deploy
fraises:
  api:
    type: api
    environments:
      production:
        server: example.com
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
"""


@pytest.fixture
def tree(tmp_path):
    """A rendered scaffold tree, plus a runner for its install.sh."""
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(_YAML)
    renderer = ScaffoldRenderer(FraisierConfig(cfg))
    renderer.output_dir = tmp_path / "generated"
    renderer.render()
    (tmp_path / "generated" / "install.sh").chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "hostname"
    fake.write_text("#!/bin/bash\necho default-testrunner\n")
    fake.chmod(0o755)
    return tmp_path


def _run(tree, scaffold_dir=None):
    env = os.environ.copy()
    env["PATH"] = f"{tree / 'bin'}:{env.get('PATH', '')}"
    return subprocess.run(
        [
            "bash",
            str(tree / "generated" / "install.sh"),
            "--dry-run",
            "--scaffold-dir",
            str(scaffold_dir or tree / "generated"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


class TestMatchingTreeInstalls:
    def test_the_tree_it_came_from_is_accepted(self, tree):
        assert _run(tree).returncode == 0

    def test_a_faithful_copy_is_accepted(self, tree):
        """Content, not location, is what is verified."""
        copy = tree / "elsewhere"
        shutil.copytree(tree / "generated", copy)

        assert _run(tree, copy).returncode == 0


class TestMismatchedTreeIsRefused:
    def test_tampered_artifact_is_refused(self, tree):
        target = tree / "generated" / "systemd" / "api.service"
        target.write_text(target.read_text() + "\n# edited by hand\n")

        result = _run(tree)

        assert result.returncode != 0
        assert "does not match the manifest" in result.stderr

    def test_the_refusal_names_the_artifact_and_both_hashes(self, tree):
        target = tree / "generated" / "systemd" / "api.service"
        target.write_text("replaced\n")

        result = _run(tree)

        assert "api.service" in result.stderr
        assert "expected sha256" in result.stderr
        assert "found    sha256" in result.stderr

    def test_the_refusal_says_how_to_fix_it(self, tree):
        target = tree / "generated" / "systemd" / "api.service"
        target.write_text("replaced\n")

        assert "fraisier scaffold" in _run(tree).stderr

    def test_missing_artifact_is_refused_not_skipped(self, tree):
        """The silent-skip failure mode of #325, generalised to every artifact."""
        (tree / "generated" / "systemd" / "api.service").unlink()

        result = _run(tree)

        assert result.returncode != 0
        assert "api.service" in result.stderr
        assert "missing" in result.stderr.lower()

    def test_nothing_is_installed_when_any_artifact_mismatches(self, tree):
        """Verification is a preflight pass, not a per-copy check.

        Checking as it goes would install whatever sorts first and refuse
        midway, leaving the host half-converted between two renders — the
        worst state to debug, on the one tool you would use to recover.
        """
        target = tree / "generated" / "systemd" / "api.service"
        target.write_text("replaced\n")

        result = _run(tree)

        assert "cp" not in result.stdout, (
            "an artifact was installed before the tree was fully verified"
        )

    def test_verification_reports_what_it_checked(self, tree):
        assert "artifact(s) match the manifest" in _run(tree).stdout


class TestStaleInstallerAgainstFreshTree:
    """The shape #323's triage actually found on a live host."""

    def test_installer_from_an_earlier_render_refuses_the_new_tree(self, tree):
        old_installer = (tree / "generated" / "install.sh").read_text()

        # Re-render with a changed config: same artifact names, new content.
        cfg = tree / "fraises.yaml"
        cfg.write_text(
            _YAML.replace("deploy_user: testapp_deploy", "deploy_user: other")
        )
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tree / "generated"
        renderer.render()

        # Put the previous installer back beside the new artifacts.
        (tree / "generated" / "install.sh").write_text(old_installer)
        (tree / "generated" / "install.sh").chmod(0o755)

        result = _run(tree)

        assert result.returncode != 0
        assert "different renders" in result.stderr


_SCHEDULED_YAML = """\
name: testapp
servers:
  example.com:
    machine_hostnames: [default-testrunner]
scaffold:
  deploy_user: testapp_deploy
fraises:
  api:
    type: api
    environments:
      production:
        server: example.com
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
  nightly:
    type: scheduled
    environments:
      production:
        server: example.com
        app_path: /var/www/nightly
        jobs:
          reindex:
            systemd_service: testapp-reindex.service
            systemd_timer: testapp-reindex.timer
"""


@pytest.fixture
def scheduled_tree(tmp_path):
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(_SCHEDULED_YAML)
    renderer = ScaffoldRenderer(FraisierConfig(cfg))
    renderer.output_dir = tmp_path / "generated"
    renderer.render()
    (tmp_path / "generated" / "install.sh").chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "hostname"
    fake.write_text("#!/bin/bash\necho default-testrunner\n")
    fake.chmod(0o755)
    return tmp_path


_RESTORE_MIGRATE_YAML = """\
name: testapp
servers:
  example.com:
    machine_hostnames: [default-testrunner]
scaffold:
  deploy_user: testapp_deploy
fraises:
  api:
    type: api
    environments:
      staging:
        server: example.com
        app_path: /var/www/api-stg
        systemd_service: api-stg.service
        git_repo: /var/git/api-stg.git
        database:
          strategy: restore_migrate
"""


@pytest.fixture
def restore_tree(tmp_path, monkeypatch):
    """A tree containing a gap, synthesised rather than found.

    `_KNOWN_GAPS` has been empty since #341 installed restore-staging, its
    last entry. The gap machinery outlives its first user — it is the honest
    label for the next artifact that is rendered, needed and reached by no
    installer — so its reporting is exercised against a declared gap instead
    of against whichever unit happens to be broken today. A test that only
    passes while a bug exists disappears with the bug.
    """
    import fraisier.scaffold.artifacts as artifacts_mod

    monkeypatch.setattr(
        artifacts_mod,
        "_KNOWN_GAPS",
        {"systemd/restore-staging.timer": "nothing installs it; the nightly wont run"},
    )
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(_RESTORE_MIGRATE_YAML)
    renderer = ScaffoldRenderer(FraisierConfig(cfg))
    renderer.output_dir = tmp_path / "generated"
    renderer.render()
    (tmp_path / "generated" / "install.sh").chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "hostname"
    fake.write_text("#!/bin/bash\necho default-testrunner\n")
    fake.chmod(0o755)
    return tmp_path


class TestCoverageReport:
    """#323: the two installers stop covering disjoint sets *silently*.

    The complaint was never that scaffold-install and scheduled-install own
    different units — they do, from genuinely different source trees. It is
    that neither said so: scaffold-install exited 0 having never touched units
    the operator assumed it owned, and scheduled-install pointed at
    scaffold-install to bootstrap a socket scaffold-install never installed.
    """

    def test_app_managed_units_are_named_with_their_source_and_installer(
        self, scheduled_tree
    ):
        out = _run(scheduled_tree).stdout

        assert "testapp-reindex.service" in out
        assert "testapp-reindex.timer" in out
        assert "/var/www/nightly/scripts/systemd" in out
        assert "fraisier scheduled-install" in out

    def test_app_managed_units_are_not_installed_by_this_script(self, scheduled_tree):
        """Named, deliberately not copied — the sets stay disjoint on purpose.

        A wildcard install over the scaffold dir was rejected as the fix: that
        directory accumulates, so a wildcard promotes a leftover file into an
        installed unit — #325's failure mode generalised.
        """
        out = _run(scheduled_tree).stdout

        assert "cp" in out, "sanity: the run installed something"
        for line in out.splitlines():
            if "cp" in line:
                assert "testapp-reindex" not in line

    def test_known_gaps_are_reported_with_their_consequence(self, restore_tree):
        out = _run(restore_tree).stdout

        assert "installed by nothing" in out
        assert "restore-staging.timer" in out
        assert "the nightly wont run" in out  # the note explains what breaks

    def test_a_tree_with_no_gaps_prints_no_gap_section(self, tree):
        """The report has to be able to go quiet, or operators learn to skip it."""
        assert "installed by nothing" not in _run(tree).stdout

    def test_a_config_without_scheduled_fraises_reports_no_app_managed(self, tree):
        assert "scheduled-install" not in _run(tree).stdout


class TestEveryInstalledArtifactIsVerified:
    """Not just the generically-copied ones.

    Each of these blocks still guards its own copy with ``[ -f ]``, which on
    its own is a silent skip — the exact shape of #325. The manifest says the
    file was rendered, so a missing one means the tree is not this installer's,
    and the preflight refuses before those guards are ever reached.
    """

    @pytest.mark.parametrize(
        ("relative", "why"),
        [
            ("sudoers", "sudoers fragment"),
            ("nginx/gateway.conf", "nginx gateway vhost"),
            (
                "systemd/fraisier-testapp-systemctl-helper.socket",
                "systemctl helper socket",
            ),
            (
                "systemd/fraisier-testapp-scaffold-install-helper.service",
                "scaffold-install helper",
            ),
        ],
    )
    def test_missing_artifact_refuses(self, tree, relative, why):
        (tree / "generated" / relative).unlink()

        result = _run(tree)

        assert result.returncode != 0, f"{why} was silently skipped"
        assert relative in result.stderr

    def test_tampered_sudoers_refuses(self, tree):
        """A sudoers fragment is installed 0440 and validated by visudo — but
        neither proves it is the fragment this installer was generated for."""
        (tree / "generated" / "sudoers").write_text("ALL ALL=(ALL) NOPASSWD: ALL\n")

        result = _run(tree)

        assert result.returncode != 0
        assert "does not match the manifest" in result.stderr

    def test_tampered_webhook_unit_refuses(self, tree):
        """Verified against the hash of the unit selected for THIS host."""
        unit = next((tree / "generated").glob("fraisier-*-webhook*.service"))
        unit.write_text(unit.read_text() + "\n# edited\n")

        result = _run(tree)

        assert result.returncode != 0
        assert "does not match the manifest" in result.stderr

    def test_install_helper_units_are_verified(self, tmp_path):
        """The #279 re-bake copies are _run_strict, but a missing source was
        still a silent skip before the preflight covered them."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            _YAML.replace(
                "  api:\n    type: api\n",
                "  api:\n    type: api\n"
                "    install:\n      user: app_user\n"
                "      command: [bash, scripts/i.sh]\n",
            )
        )
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tmp_path / "generated"
        renderer.render()
        (tmp_path / "generated" / "install.sh").chmod(0o755)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "hostname"
        fake.write_text("#!/bin/bash\necho default-testrunner\n")
        fake.chmod(0o755)

        helper = next(
            (tmp_path / "generated" / "systemd").glob("*install-helper.service")
        )
        helper.unlink()

        result = _run(tmp_path)

        assert result.returncode != 0
        assert "install-helper.service" in result.stderr
