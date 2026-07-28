"""Sudoers rules must carry a fully-qualified command path (#287).

sudoers requires the ``Cmnd`` token to be an absolute path. A bare interpreter
name (``bash``, ``python3``) makes ``visudo`` reject the whole fragment, which
aborts ``scaffold-install`` before it installs the systemd units, the
per-fraise install-helper socket, nginx and the PostgreSQL config.
"""

from __future__ import annotations

import pytest

from fraisier.config import FraisierConfig, ValidationError
from fraisier.scaffold.renderer import ScaffoldRenderer

_YAML = """
name: tp
scaffold:
  deploy_user: deployer
  output_dir: {output}
fraises:
  my_api:
    type: api
    install:
      user: appuser
      command: {command}
    environments:
      production:
        app_path: /var/www/prod
"""


def _render_sudoers(tmp_path, command: str) -> str:
    """Render the scaffold for *command* and return the sudoers fragment."""
    p = tmp_path / "fraises.yaml"
    p.write_text(_YAML.format(output=str(tmp_path / "output"), command=command))
    ScaffoldRenderer(FraisierConfig(p)).render()
    return (tmp_path / "output" / "sudoers").read_text()


def _cmnd_line(content: str) -> str:
    """Return the single NOPASSWD rule line from a sudoers fragment."""
    lines = [ln for ln in content.splitlines() if "NOPASSWD:" in ln]
    assert len(lines) == 1, f"expected exactly one NOPASSWD rule, got {lines}"
    return lines[0]


def _cmnd_token(content: str) -> str:
    """Return the command token that sudoers will parse as the Cmnd."""
    return _cmnd_line(content).split("NOPASSWD:", 1)[1].split()[0]


class TestUnmappedInterpreterResolves:
    """Commands outside the hardcoded map still get an absolute path."""

    def test_bare_bash_resolves_to_absolute_path(self, tmp_path, monkeypatch):
        """`install.command: [bash, ...]` must not emit a bare `bash` Cmnd."""
        monkeypatch.setattr(
            "shutil.which", lambda cmd: "/usr/bin/bash" if cmd == "bash" else None
        )

        content = _render_sudoers(tmp_path, "[bash, scripts/install.sh]")

        assert _cmnd_token(content) == "/usr/bin/bash"


class TestFhsSearchFallback:
    """When `which` misses, fall back to the target server's likely FHS dirs.

    `fraisier scaffold` is often run from a dev box or a container whose PATH
    doesn't contain the server's tools, so `which` returning None must not be
    the end of the search.
    """

    def test_falls_back_to_fhs_dirs_when_which_misses(self, tmp_path, monkeypatch):
        """A token absent from PATH still resolves via the fixed search list."""
        monkeypatch.setattr("shutil.which", lambda _cmd: None)
        monkeypatch.setattr(
            "fraisier.scaffold.renderer._is_executable_file",
            lambda path: str(path) == "/usr/local/bin/frobctl",
        )

        content = _render_sudoers(tmp_path, "[frobctl, deploy]")

        assert _cmnd_token(content) == "/usr/local/bin/frobctl"

    def test_search_order_prefers_usr_bin(self, tmp_path, monkeypatch):
        """When a token exists in several dirs, the first match wins."""
        monkeypatch.setattr("shutil.which", lambda _cmd: None)
        monkeypatch.setattr(
            "fraisier.scaffold.renderer._is_executable_file",
            lambda path: str(path) in ("/usr/bin/frobctl", "/usr/local/bin/frobctl"),
        )

        content = _render_sudoers(tmp_path, "[frobctl, deploy]")

        assert _cmnd_token(content) == "/usr/bin/frobctl"


class TestUnresolvableCommandFailsLoudly:
    """An unresolvable token must not be written into a sudoers Cmnd.

    Emitting it produces a fragment `visudo` rejects, which aborts the whole
    `scaffold-install` run — the failure #287 reports.
    """

    def test_unresolvable_command_raises(self, tmp_path, monkeypatch):
        """Scaffold fails rather than emitting a fragment that cannot parse."""
        monkeypatch.setattr("shutil.which", lambda _cmd: None)
        monkeypatch.setattr(
            "fraisier.scaffold.renderer._is_executable_file", lambda _path: False
        )

        with pytest.raises(ValidationError) as exc_info:
            _render_sudoers(tmp_path, "[frobctl, deploy]")

        message = str(exc_info.value)
        assert "frobctl" in message
        assert "install.command" in message
        assert "/usr/local/bin" in message, "message should list the searched dirs"

    def test_error_names_the_owning_fraise(self, tmp_path, monkeypatch):
        """The message points at the config to edit, not just the token."""
        monkeypatch.setattr("shutil.which", lambda _cmd: None)
        monkeypatch.setattr(
            "fraisier.scaffold.renderer._is_executable_file", lambda _path: False
        )

        with pytest.raises(ValidationError) as exc_info:
            _render_sudoers(tmp_path, "[frobctl, deploy]")

        assert "my_api" in str(exc_info.value)


class TestResolutionInvariants:
    """Properties the resolution chain must keep as it grows."""

    def test_absolute_command_passes_through_untouched(self, tmp_path, monkeypatch):
        """An operator-supplied absolute path is never second-guessed."""
        monkeypatch.setattr(
            "shutil.which", lambda _cmd: "/somewhere/else/bash"
        )  # must be ignored

        content = _render_sudoers(tmp_path, "[/opt/tools/bash, scripts/install.sh]")

        assert _cmnd_token(content) == "/opt/tools/bash"

    def test_hardcoded_map_wins_over_which(self, tmp_path, monkeypatch):
        """`uv` keeps its server path even when scaffolding from a dev box.

        `shutil.which` on a developer machine returns a per-user path
        (~/.local/bin/uv) that is wrong for the target server, so the map
        must outrank it.
        """
        monkeypatch.setattr(
            "shutil.which", lambda _cmd: "/home/dev/.local/bin/uv"
        )  # dev-box path

        content = _render_sudoers(tmp_path, "[uv, sync, --frozen]")

        assert _cmnd_token(content) == "/usr/local/bin/uv"

    def test_arguments_are_preserved_verbatim(self, tmp_path, monkeypatch):
        """Only the first token is rewritten; the argument string is untouched."""
        monkeypatch.setattr(
            "shutil.which", lambda cmd: "/usr/bin/bash" if cmd == "bash" else None
        )

        content = _render_sudoers(
            tmp_path, '[bash, -c, "uv sync && uv run manage.py migrate"]'
        )

        line = _cmnd_line(content)
        assert "/usr/bin/bash -c uv sync && uv run manage.py migrate" in line


def _render_install_sh(tmp_path) -> str:
    """Render install.sh for a minimal config."""
    p = tmp_path / "fraises.yaml"
    p.write_text(
        _YAML.format(output=str(tmp_path / "output"), command="[/usr/bin/bash, x.sh]")
    )
    renderer = ScaffoldRenderer(FraisierConfig(p))
    return renderer.env.get_template("core/install.sh.j2").render(**renderer.context)


class TestSudoersValidatedBeforeInstall:
    """install.sh must not write a fragment it is about to reject.

    Validating `${SUDOERS_DST}` after `install`-ing it leaves an invalid file in
    /etc/sudoers.d/ — which sudo treats as fatal — while printing "File was not
    installed."
    """

    def test_validation_targets_the_source_file(self, tmp_path):
        """`visudo -c` runs against the staged source, not the installed copy."""
        content = _render_install_sh(tmp_path)

        assert 'visudo -c -f "${SUDOERS_SRC}"' in content
        assert 'visudo -c -f "${SUDOERS_DST}"' not in content

    def test_install_happens_after_validation(self, tmp_path):
        """The `install` call is ordered after the `visudo` check."""
        content = _render_install_sh(tmp_path)

        validate_at = content.index('visudo -c -f "${SUDOERS_SRC}"')
        install_at = content.index('install -m 0440 "${SUDOERS_SRC}"')
        assert validate_at < install_at, "sudoers must be validated before installing"

    def test_failure_message_is_truthful(self, tmp_path):
        """The message may only claim non-installation if that is now true."""
        content = _render_install_sh(tmp_path)

        assert "File was not installed." in content
