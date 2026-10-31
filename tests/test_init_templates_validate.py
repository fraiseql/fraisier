"""Every config ``fraisier init`` writes passes ``validate-deployment``.

``init`` is the first thing a new project runs, and ``validate-deployment``
the next. A template whose database block the config loader refuses, or that
the readiness check judges by keys no config has, fails a project before its
first deploy. Only the host is stubbed here: the app directory, ``systemctl``,
the sudoers file and the health endpoint. The config is ``init``'s own.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from fraisier.cli.main import main
from fraisier.init_templates import TEMPLATES

_REAL_EXISTS = Path.exists


def _sudoers_installed(path: Path) -> bool:
    return str(path).startswith("/etc/sudoers.d/") or _REAL_EXISTS(path)


def _systemctl_active(cmd, *args, **kwargs):
    return subprocess.CompletedProcess(cmd, 0, stdout=b"active\n", stderr=b"")


def _healthy_response() -> MagicMock:
    response = MagicMock()
    response.status = 200
    response.read.return_value = b'{"status": "ok"}'
    response.__enter__.return_value = response
    return response


def _init(tmp_path: Path, template: str) -> Path:
    result = CliRunner().invoke(main, ["init", "-t", template, "-o", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path / "fraises.yaml"


def _targets(config_file: Path) -> list[tuple[str, str]]:
    fraises = yaml.safe_load(config_file.read_text())["fraises"]
    return [(f, e) for f, cfg in fraises.items() for e in cfg["environments"]]


def _with_app_paths_on(config_file: Path, root: Path) -> None:
    """Point each ``app_path`` at a directory that exists on this host."""
    config = yaml.safe_load(config_file.read_text())
    for fraise in config["fraises"].values():
        for env in fraise["environments"].values():
            app = root / env["app_path"].lstrip("/")
            app.mkdir(parents=True, exist_ok=True)
            env["app_path"] = str(app)
    config_file.write_text(yaml.safe_dump(config))


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_every_environment_init_writes_is_ready(tmp_path, template):
    config_file = _init(tmp_path, template)
    _with_app_paths_on(config_file, tmp_path / "host")
    wrapper = tmp_path / "systemctl-wrapper"
    wrapper.touch(mode=0o755)

    for fraise, environment in _targets(config_file):
        with (
            patch.dict("os.environ", {"FRAISIER_SYSTEMCTL_WRAPPER": str(wrapper)}),
            patch("subprocess.run", side_effect=_systemctl_active),
            patch.object(Path, "exists", _sudoers_installed),
            patch("urllib.request.urlopen", return_value=_healthy_response()),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "-c",
                    str(config_file),
                    "validate-deployment",
                    fraise,
                    environment,
                    "--json",
                ],
            )

        where = f"{template}: {fraise}/{environment}"
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"{where}: {result.exception!r}"
        )
        failed = [
            check
            for check in json.loads(result.output)["checks"]
            if not check["passed"]
        ]
        assert failed == [], where
        assert result.exit_code == 0, where


def test_a_config_the_loader_refuses_is_a_clean_error(tmp_path):
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "fraises": {
                    "acme": {
                        "type": "api",
                        "environments": {
                            "development": {
                                "app_path": str(tmp_path),
                                "database": {"name": "acme", "strategy": "rebuild"},
                            }
                        },
                    }
                }
            }
        )
    )

    result = CliRunner().invoke(
        main, ["-c", str(config_file), "validate-deployment", "acme", "development"]
    )

    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert result.exit_code == 1
    assert "requires database.admin_url" in result.output
