"""Every rendered unit that runs a deploy carries the install-helper routing (#324).

When a fraise declares an ``install.user`` different from the deploy user, the
install step cannot just ``sudo -u`` — every deploy-capable unit is hardened
with ``NoNewPrivileges``, under which sudo refuses to run at all. The way out
is a per-(fraise, env) install-helper socket, and a unit reaches it only if it
was given ``FRAISIER_INSTALL_SOCKET_<FRAISE>_<ENV>`` in its environment
(``deployers/mixins.py`` reads exactly that key, then falls back to sudo).

The webhook unit had the routing. The deploy-daemon unit — the one behind
every ``trigger-deploy``, and so behind every timer and CLI deploy — did not.
Same config, same render, two units that run the same code, one wired.
Reported from printoptim.dev (#324): the deploy failed in ~3s with
*"sudo: The 'no new privileges' flag is set"* while the identical deploy
through the webhook succeeded.

These tests are written as a symmetry property rather than a patch: the sweep
below finds every rendered unit whose ExecStart runs a deploy **in process**
and asserts each carries the routing the others have, so a third deploy-capable
unit cannot be added unwired.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

if TYPE_CHECKING:
    from pathlib import Path

# Two fraises, two environments, both with an install user that differs from
# the deploy user — so every install-helper socket is genuinely required.
CONFIG = """\
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {output}
fraises:
  api:
    type: api
    install:
      user: app_user
      command: [bash, scripts/deploy-install.sh]
    environments:
      development:
        app_path: /var/www/api-dev
        systemd_service: api-dev.service
        git_repo: /var/git/api-dev.git
      staging:
        app_path: /var/www/api-stg
        systemd_service: api-stg.service
        git_repo: /var/git/api-stg.git
  worker:
    type: api
    install:
      user: app_user
      command: [bash, scripts/deploy-install.sh]
    environments:
      production:
        app_path: /var/www/worker
        systemd_service: worker.service
        git_repo: /var/git/worker.git
"""

# ExecStart fragments that mean "this unit performs a deployment in process",
# and therefore runs the install step and needs the socket routing. Units that
# merely *ask* for a deploy (poll-deploy runs `trigger-deploy`, which writes to
# a socket and hands off) are deliberately absent: the install runs in the
# daemon on the other end, not in them.
_IN_PROCESS_DEPLOY_MARKERS = ("fraisier-webhook", "deploy-daemon", "fraisier deploy ")

_INSTALL_SOCKET_RE = re.compile(
    r"^Environment=(FRAISIER_INSTALL_SOCKET_[A-Z0-9_]+)=(\S+)$", re.MULTILINE
)


@pytest.fixture
def rendered(tmp_path) -> Path:
    """Render the scaffold once and return the output root.

    The root, not ``systemd/``: the webhook unit is written at the top level
    while deploy units land under ``systemd/``, and a sweep that looked in
    only one of those places would miss exactly the pair being compared.
    """
    out = tmp_path / "output"
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(CONFIG.format(output=out))
    ScaffoldRenderer(FraisierConfig(cfg)).render()
    return out


def _install_sockets(unit: Path) -> dict[str, str]:
    return dict(_INSTALL_SOCKET_RE.findall(unit.read_text()))


def _deploy_capable_units(output_root: Path) -> list[Path]:
    """Rendered units whose ExecStart performs a deploy in process."""
    found = []
    for unit in sorted(output_root.rglob("*.service")):
        execstarts = [
            line
            for line in unit.read_text().splitlines()
            if line.startswith("ExecStart=")
        ]
        if any(m in line for line in execstarts for m in _IN_PROCESS_DEPLOY_MARKERS):
            found.append(unit)
    return found


class TestTheReportedGap:
    """#324 as reported: the deploy-daemon unit had no routing at all."""

    def test_deploy_service_carries_install_socket_env(self, rendered):
        unit = rendered / "systemd" / "fraisier-api-development@.service"
        assert unit.exists(), sorted(p.name for p in rendered.rglob("*.service"))

        assert _install_sockets(unit), (
            "deploy-daemon unit has no FRAISIER_INSTALL_SOCKET_* entry, so the "
            "install falls back to sudo and NoNewPrivileges denies it"
        )

    def test_deploy_service_matches_the_webhook_set(self, rendered):
        """Symmetry, stated directly: same config, same routing."""
        webhook = rendered / "fraisier-myproj-webhook.service"
        deploy = rendered / "systemd" / "fraisier-api-development@.service"

        assert _install_sockets(deploy) == _install_sockets(webhook)

    def test_routing_covers_every_install_user_env(self, rendered):
        """All three fraise+env pairs declare an install user, so all appear."""
        deploy = rendered / "systemd" / "fraisier-api-development@.service"

        assert set(_install_sockets(deploy)) == {
            "FRAISIER_INSTALL_SOCKET_API_DEVELOPMENT",
            "FRAISIER_INSTALL_SOCKET_API_STAGING",
            "FRAISIER_INSTALL_SOCKET_WORKER_PRODUCTION",
        }


class TestSymmetryAcrossEveryDeployCapableUnit:
    """The property, so the next unit type cannot be added unwired."""

    def test_sweep_finds_both_known_deploy_paths(self, rendered):
        """Guards the sweep itself: a marker that matches nothing proves nothing."""
        names = {u.name for u in _deploy_capable_units(rendered)}

        assert "fraisier-myproj-webhook.service" in names
        assert any(name.endswith("@.service") for name in names)

    def test_every_deploy_capable_unit_has_identical_routing(self, rendered):
        """No unit that runs a deploy may be missing what the others have."""
        units = _deploy_capable_units(rendered)
        assert len(units) > 1, "sweep must compare at least two units"

        routing = {u.name: _install_sockets(u) for u in units}
        distinct = {tuple(sorted(v.items())) for v in routing.values()}

        assert len(distinct) == 1, (
            f"deploy-capable units disagree about install-helper routing: {routing}"
        )

    def test_every_deploy_capable_unit_is_hardened(self, rendered):
        """Why routing is mandatory: on these units sudo cannot work.

        If this ever fails, the socket routing stopped being load-bearing and
        the reasoning in this file needs revisiting — it must not be relaxed by
        quietly dropping NoNewPrivileges instead.
        """
        for unit in _deploy_capable_units(rendered):
            text = unit.read_text()
            assert re.search(r"^NoNewPrivileges=(yes|true)$", text, re.MULTILINE), (
                f"{unit.name} is not NoNewPrivileges-hardened"
            )


class TestNoRoutingWhenNoneIsNeeded:
    """A config with no separate install user emits no socket entries."""

    def test_absent_install_user_emits_no_socket_env(self, tmp_path):
        out = tmp_path / "output"
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            f"""\
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {out}
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
"""
        )
        ScaffoldRenderer(FraisierConfig(cfg)).render()

        for unit in _deploy_capable_units(out):
            assert _install_sockets(unit) == {}
