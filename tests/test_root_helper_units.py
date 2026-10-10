"""The root helpers run root-owned code, never the deploy user's (#433, path 1).

They used to run ``/home/<deploy>/.local/bin/fraisier-*-helper``: a venv the
deploy user owns, so any module, ``.pth`` or ``sitecustomize`` in it ran as
root. Now each runs the root-owned interpreter of the root tool install, with
``-I`` so that neither ``PYTHONPATH``, the working directory nor a user site
can add to what it imports.
"""

from __future__ import annotations

import re
import subprocess
import sys

import pytest

from fraisier.root_install import ROOT_PYTHON
from tests.test_install_plan_golden import _PGBACKREST_HELPER, _render

SCHEDULED_TOO = (
    _PGBACKREST_HELPER
    + """\
  nightly:
    type: scheduled
    environments:
      staging:
        server: a.example.io
        app_path: /var/www/nightly
        systemd_service: nightly.service
        systemd_timer: nightly.timer
        script_path: /usr/local/bin/nightly.sh
"""
)

ROOT_HELPERS = {
    "fraisier-proj-systemctl-helper.service": "fraisier.systemctl_helper",
    "fraisier-proj-scaffold-install-helper.service": "fraisier.scaffold_install_helper",
    "fraisier-proj-staging-unit-installer.service": "fraisier.unit_installer_helper",
    "fraisier-proj-api-staging-pgbackrest-helper.service": "fraisier.pgbackrest_helper",
}


@pytest.fixture(scope="module")
def systemd(tmp_path_factory):
    return _render(tmp_path_factory.mktemp("render"), SCHEDULED_TOO) / "systemd"


def test_the_root_interpreter_is_under_the_root_tool_dir():
    assert ROOT_PYTHON == "/usr/local/lib/fraisier-root/tools/fraisier/bin/python"


@pytest.mark.parametrize(("unit", "module"), ROOT_HELPERS.items())
def test_each_root_helper_runs_the_root_interpreter_isolated(systemd, unit, module):
    text = (systemd / unit).read_text()
    [exec_start] = re.findall(r"^ExecStart=(.*)$", text, re.MULTILINE)
    assert exec_start.startswith(f"{ROOT_PYTHON} -I -m {module} "), exec_start


@pytest.mark.parametrize("unit", ROOT_HELPERS)
def test_no_root_helper_names_the_deploy_users_home(systemd, unit):
    assert "/home/" not in (systemd / unit).read_text()


@pytest.mark.parametrize("module", sorted(set(ROOT_HELPERS.values())))
def test_running_the_module_runs_its_main(module):
    """Without a ``__main__`` guard, ``-m`` imports the module and exits 0."""
    result = subprocess.run(
        [sys.executable, "-I", "-m", module],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=60,
        check=False,
    )
    assert result.returncode != 0, result.stderr
