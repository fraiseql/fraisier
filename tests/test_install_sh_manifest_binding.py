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
