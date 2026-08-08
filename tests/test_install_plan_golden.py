"""Golden equivalence pin for the generated installer's action plan.

This exists to make a refactor of the install path safe. ``install.sh`` is the
mechanism you would otherwise use to fix a bad release, so changing how it
decides what to install needs a pin that is stronger than "the tests still
pass": for a matrix of configs, the *ordered list of commands the installer
plans* must not move.

It is captured by running the real rendered ``install.sh --dry-run`` against
the real rendered tree, not by parsing the template. That distinction matters
— the plan includes the hand-written ordering that several fixes turned on
(the #279 re-bake's cp → daemon-reload → stop → enable → restart, the
systemctl-helper's daemon-reload before restart), and a template parser would
pin the copies while silently losing the sequences.

**When this test fails**, the refactor changed behaviour. That is not always
wrong — but it must be deliberate. Read the diff, convince yourself of every
line, then regenerate with::

    FRAISIER_UPDATE_INSTALL_GOLDEN=1 uv run pytest tests/test_install_plan_golden.py

and commit the golden file as part of the same change, so the behavioural
delta is reviewable in the diff rather than buried in a rerun.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

_GOLDEN = Path(__file__).parent / "golden" / "install_plan.json"

# One machine, every environment on it. The baseline: no host asymmetry to get
# wrong, so anything that differs here is unconditional drift.
_SINGLE_HOST = """\
name: proj
servers:
  only.example.io:
    machine_hostnames: [solo]
scaffold:
  deploy_user: deployer
fraises:
  api:
    type: api
    environments:
      development:
        server: only.example.io
        app_path: /var/www/api-dev
        systemd_service: api-dev.service
        git_repo: /var/git/api-dev.git
      production:
        server: only.example.io
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
"""

# The #325 shape, and the one the old matrix never covered: one host carries
# two environments, the other carries one. Exercised from BOTH hosts, because
# the failure mode was a host installing the *other* host's artifacts.
_ASYMMETRIC = """\
name: proj
servers:
  dev.example.io:
    machine_hostnames: [devbox]
  prod.example.io:
    machine_hostnames: [pio]
scaffold:
  deploy_user: deployer
fraises:
  api:
    type: api
    environments:
      development:
        server: dev.example.io
        app_path: /var/www/api-dev
        systemd_service: api-dev.service
        git_repo: /var/git/api-dev.git
      staging:
        server: dev.example.io
        app_path: /var/www/api-stg
        systemd_service: api-stg.service
        git_repo: /var/git/api-stg.git
      production:
        server: prod.example.io
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
"""

# `server:` declared only under fraises.*, never in the global environments:
# section — the shape that looked server-less to the renderer before v0.56.0.
_PER_FRAISE_SERVERS = """\
name: proj
servers:
  a.example.io:
    machine_hostnames: [abox]
  b.example.io:
    machine_hostnames: [bbox]
scaffold:
  deploy_user: deployer
fraises:
  api:
    type: api
    environments:
      production:
        server: a.example.io
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
  worker:
    type: api
    environments:
      production:
        server: b.example.io
        app_path: /var/www/worker
        systemd_service: worker.service
        git_repo: /var/git/worker.git
"""

# A separate install user, so the #279 install-helper re-bake sequence is in
# the plan and pinned in order.
_INSTALL_HELPER = """\
name: proj
servers:
  only.example.io:
    machine_hostnames: [solo]
scaffold:
  deploy_user: deployer
fraises:
  api:
    type: api
    install:
      user: app_user
      command: [bash, scripts/deploy-install.sh]
    environments:
      production:
        server: only.example.io
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
"""

# nginx vhosts (gateway + per-environment), so the copy+symlink pair is pinned.
_NGINX = """\
name: proj
servers:
  only.example.io:
    machine_hostnames: [solo]
scaffold:
  deploy_user: deployer
fraises:
  api:
    type: api
    environments:
      production:
        server: only.example.io
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
        nginx:
          server_name: api.example.com
"""

# An nginx vhost with NO explicit server_name, so the filename falls back to
# the derived stem. The renderer writes `{project}_{fraise}_{env}.conf` while
# install.sh used to look for `{fraise}_{env}.conf` — behind a `[ -f ]` guard,
# which made it a silent skip: the vhost was rendered and never installed.
_NGINX_DERIVED_NAME = """\
name: proj
servers:
  only.example.io:
    machine_hostnames: [solo]
scaffold:
  deploy_user: deployer
fraises:
  api:
    type: api
    environments:
      production:
        server: only.example.io
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
        nginx:
          proxy_pass: http://localhost:8000
"""

# A scheduled fraise, which brings timer units into the tree.
_SCHEDULED = """\
name: proj
servers:
  only.example.io:
    machine_hostnames: [solo]
scaffold:
  deploy_user: deployer
fraises:
  api:
    type: api
    environments:
      production:
        server: only.example.io
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
  nightly:
    type: scheduled
    environments:
      production:
        server: only.example.io
        app_path: /var/www/nightly
        systemd_service: nightly.service
        systemd_timer: nightly.timer
        script_path: /usr/local/bin/nightly.sh
"""

# #336's own shape, plus the artifact that must NOT follow the same rule.
# Three fraises share the name `production` across two servers and a fourth
# sits alone on a third, so one config exercises both predicates: `worker`
# reaches box-b and not box-a (fraise-owned), while the unit-installer helper
# reaches every host declaring `production` (env-owned, one per project+env).
_SCOPED_HOSTS = """\
name: proj
servers:
  a.example.io:
    machine_hostnames: [abox]
  b.example.io:
    machine_hostnames: [bbox]
  c.example.io:
    machine_hostnames: [cbox]
scaffold:
  deploy_user: deployer
fraises:
  api:
    type: api
    environments:
      production:
        server: a.example.io
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
  worker:
    type: api
    environments:
      production:
        server: b.example.io
        app_path: /var/www/worker
        systemd_service: worker.service
        git_repo: /var/git/worker.git
  nightly:
    type: scheduled
    environments:
      production:
        server: b.example.io
        app_path: /var/www/nightly
        systemd_service: nightly.service
        systemd_timer: nightly.timer
        script_path: /usr/local/bin/nightly.sh
  edge:
    type: api
    environments:
      staging:
        server: c.example.io
        app_path: /var/www/edge
        systemd_service: edge.service
        git_repo: /var/git/edge.git
"""

# #339's shape: a host that *receives* a backup corpus. `cbox` carries
# `edge`/staging and declares a retain policy for it, so it installs and
# ENABLES a retention pair. `abox` carries `api`/production and declares
# none, so it must plan neither — the retention units are env-owned, and
# "env-owned" must not degrade into "unconditional".
#
# Note the corpus has no producing fraise anywhere in this config. That is
# the point: it arrives by rsync from a machine fraisier does not manage,
# which is why the units gate on `_env_active` rather than `_scope_active`.
_RECEIVING_HOST = """\
name: proj
servers:
  a.example.io:
    machine_hostnames: [abox]
  c.example.io:
    machine_hostnames: [cbox]
scaffold:
  deploy_user: deployer
fraises:
  api:
    type: api
    environments:
      production:
        server: a.example.io
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
  edge:
    type: api
    environments:
      staging:
        server: c.example.io
        app_path: /var/www/edge
        systemd_service: edge.service
        git_repo: /var/git/edge.git
backup:
  environments:
    staging:
      retain:
        - dir: /backup/production
          match: "*_full_*.dump"
          retention_days: 3
          keep_minimum: 3
          schedule: "*-*-* 05:30:00 UTC"
          name: production-full
        - dir: /backup/analytics
          retention_days: 14
          schedule: "*-*-* 06:00:00 UTC"
"""

# (case name, config, hostname). Two entries for the asymmetric config: the
# whole point is that the two hosts must plan *different* installs.
MATRIX = [
    ("single_host", _SINGLE_HOST, "solo"),
    ("asymmetric_dev_staging_host", _ASYMMETRIC, "devbox"),
    ("asymmetric_prod_only_host", _ASYMMETRIC, "pio"),
    ("per_fraise_servers_a", _PER_FRAISE_SERVERS, "abox"),
    ("per_fraise_servers_b", _PER_FRAISE_SERVERS, "bbox"),
    ("scoped_hosts_fraise_owned", _SCOPED_HOSTS, "abox"),
    ("scoped_hosts_with_env_owned_helper", _SCOPED_HOSTS, "bbox"),
    ("scoped_hosts_other_environment", _SCOPED_HOSTS, "cbox"),
    ("install_helper_rebake", _INSTALL_HELPER, "solo"),
    ("nginx_vhosts", _NGINX, "solo"),
    ("nginx_derived_vhost_name", _NGINX_DERIVED_NAME, "solo"),
    ("scheduled_fraise", _SCHEDULED, "solo"),
    ("receiving_host_with_retention", _RECEIVING_HOST, "cbox"),
    ("non_receiving_host", _RECEIVING_HOST, "abox"),
]


def _render(tmp_path: Path, yaml_text: str) -> Path:
    """Render the scaffold and return the output directory."""
    out = tmp_path / "generated"
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(yaml_text)
    renderer = ScaffoldRenderer(FraisierConfig(cfg))
    renderer.output_dir = out
    renderer.render()
    return out


def _install_plan(tmp_path: Path, yaml_text: str, hostname: str) -> list[str]:
    """The ordered commands the rendered installer plans, for *hostname*.

    Runs the real script so the plan reflects every runtime conditional —
    ``_env_active`` gating, per-host webhook selection, ``[ -f ]`` guards
    against the actually-rendered tree.
    """
    out = _render(tmp_path, yaml_text)
    install_sh = out / "install.sh"
    install_sh.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "hostname"
    fake.write_text(f"#!/bin/bash\necho {hostname}\n")
    fake.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    result = subprocess.run(
        [
            "bash",
            str(install_sh),
            "--dry-run",
            "--scaffold-dir",
            str(out),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        detail = (
            f"installer exited {result.returncode} for {hostname}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        pytest.fail(detail)  # ty: ignore[invalid-argument-type]

    markers = ("[would run] ", "[would validate] ")
    plan = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        marker = next((m for m in markers if stripped.startswith(m)), None)
        if marker is not None:
            # Absolute tmp paths differ per run; the scaffold dir is the only
            # one that appears, so collapse it to a stable token.
            plan.append(stripped[len(marker) :].replace(str(out), "$SCAFFOLD"))
    return plan


def _current_plans(tmp_path_factory) -> dict[str, list[str]]:
    plans = {}
    for case, yaml_text, hostname in MATRIX:
        tmp = tmp_path_factory.mktemp(case)
        plans[case] = _install_plan(tmp, yaml_text, hostname)
    return plans


@pytest.fixture(scope="module")
def plans(tmp_path_factory) -> dict[str, list[str]]:
    return _current_plans(tmp_path_factory)


def test_install_plan_matches_golden(plans):
    """The refactor must not move a single planned command."""
    if os.environ.get("FRAISIER_UPDATE_INSTALL_GOLDEN"):
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(json.dumps(plans, indent=2) + "\n")
        regenerated = "golden regenerated — review the diff before committing"
        pytest.skip(regenerated)  # ty: ignore[too-many-positional-arguments]

    assert _GOLDEN.exists(), (
        "golden file missing; regenerate with FRAISIER_UPDATE_INSTALL_GOLDEN=1"
    )
    expected = json.loads(_GOLDEN.read_text())

    assert set(expected) == set(plans), "matrix cases changed"
    for case in expected:
        assert plans[case] == expected[case], f"install plan drifted for {case!r}"


class TestTheMatrixIsMeaningful:
    """Guards on the pin itself — a golden of nothing pins nothing."""

    def test_every_case_plans_something(self, plans):
        for case, plan in plans.items():
            assert plan, f"{case} planned no commands; the pin would be vacuous"

    def test_asymmetric_hosts_plan_different_installs(self, plans):
        """The #325 shape: two hosts, same tree, deliberately different plans."""
        dev = plans["asymmetric_dev_staging_host"]
        prod = plans["asymmetric_prod_only_host"]

        assert dev != prod
        assert any("api-dev.service" in c for c in dev)
        assert not any("api-dev.service" in c for c in prod), (
            "the production-only host planned an install of a development unit"
        )

    def test_hosts_do_not_provision_each_others_directories(self, plans):
        """#325's shape, one layer down: the directory block was ungated.

        Units were host-filtered while the paths those units live in were not,
        so a production-only host created — and chowned — the dev host's
        git_repo and app_path. Empty and wrongly-present, they read as a
        half-provisioned environment on a box that should have no trace of it.
        """
        prod = plans["asymmetric_prod_only_host"]
        touched = [c for c in prod if any(v in c for v in ("mkdir", "chown", "chmod"))]

        assert not [c for c in touched if "api-dev" in c or "api-stg" in c], (
            "the production-only host provisioned another host's directories"
        )

    def test_a_host_still_provisions_its_own_directories(self, plans):
        """The gate has to let this host's own paths through."""
        prod = " ".join(plans["asymmetric_prod_only_host"])

        assert "mkdir -p /var/www/api" in prod
        assert "mkdir -p /var/git/api.git" in prod

    def test_shared_directories_survive_the_gate(self, plans):
        """Paths no environment owns are unconditional and must stay so."""
        for case in ("single_host", "asymmetric_prod_only_host"):
            plan = " ".join(plans[case])

            assert "mkdir -p /var/lib/fraisier" in plan, case
            assert "mkdir -p /opt/fraisier" in plan, case

    def test_host_scoping_is_by_fraise_and_environment(self, plans):
        """#336, from the other side: each host plans only its own fraise.

        The pin this replaces asserted the cross-install as a known
        limitation and said to delete it when scoping became fraise-aware.
        It has; ``tests/test_host_scope_gate.py`` carries the full case,
        and this keeps the golden matrix honest about what changed in it.
        """
        abox = " ".join(plans["per_fraise_servers_a"])

        assert "api.service /etc/systemd/system/api.service" in abox
        assert "worker.service" not in abox
        assert "mkdir -p /var/www/api" in abox
        assert "mkdir -p /var/www/worker" not in abox

    def test_each_host_installs_its_own_webhook_unit(self, plans):
        """The unit whose name carries the host is the one copied (#325)."""
        dev = " ".join(plans["asymmetric_dev_staging_host"])
        prod = " ".join(plans["asymmetric_prod_only_host"])

        assert "webhook-dev-example-io.service" in dev
        assert "webhook-prod-example-io.service" in prod

    def test_install_helper_rebake_order_is_captured(self, plans):
        """#279's sequence must be *in* the pin, or the pin cannot protect it.

        Scoped to the per-(fraise, env) helper: the scaffold-install-helper is
        a different unit with a deliberately different sequence (it must not
        restart itself), and merging the two would compare unrelated verbs.
        """
        unit = "fraisier-proj-api-production-install-helper"
        helper_ops = [c for c in plans["install_helper_rebake"] if unit in c]
        verbs = [c.split()[2] if "systemctl" in c else "cp" for c in helper_ops]

        assert verbs.count("cp") == 2, f"expected socket+service copies: {helper_ops}"
        assert verbs.index("cp") < verbs.index("stop") < verbs.index("restart"), (
            f"re-bake order not captured in the golden plan: {helper_ops}"
        )
        assert verbs.index("stop") < verbs.index("enable"), (
            "enable --now is a no-op on a running unit; the stop must precede it"
        )


class TestUnitInstallerRebakeOrderIsCaptured:
    """#240's helper bakes its allowlist into argv, so it re-bakes like #279.

    Same hazard, same sequence: a running .service holds the OLD ``--allow``
    argv and ``enable --now`` is a no-op on it, so the stop must land between
    the copy and the socket restart or the stale allowlist survives a green
    install.
    """

    def test_the_sequence_is_in_the_pin(self, plans):
        unit = "fraisier-proj-production-unit-installer"
        ops = [c for c in plans["scheduled_fraise"] if unit in c]
        verbs = [c.split()[2] if "systemctl" in c else "cp" for c in ops]

        assert verbs.count("cp") == 2, f"expected socket+service copies: {ops}"
        assert verbs.index("cp") < verbs.index("stop") < verbs.index("restart"), (
            f"re-bake order not captured in the golden plan: {ops}"
        )
        assert verbs.index("stop") < verbs.index("enable"), (
            "enable --now is a no-op on a running unit; the stop must precede it"
        )

    def test_the_socket_is_copied_before_the_service(self, plans):
        """The .service `Requires=` the socket; copying it first would load a
        unit whose dependency is not on disk yet."""
        unit = "fraisier-proj-production-unit-installer"
        copies = [c for c in plans["scheduled_fraise"] if unit in c and "cp" in c]

        assert copies[0].endswith(".socket")
        assert copies[1].endswith(".service")

    def test_a_config_without_scheduled_fraises_installs_no_helper(self, plans):
        """One helper per environment that has one — not one per host."""
        assert not [c for c in plans["single_host"] if "unit-installer" in c]


class TestDerivedVhostNameIsInstalled:
    """A vhost with no explicit ``server_name`` reaches the host.

    Three components computed that filename independently, and the
    installer's copy omitted the project prefix — so the renderer wrote
    ``{project}_{fraise}_{env}.conf`` and ``install.sh`` looked for
    ``{fraise}_{env}.conf``. The old ``[ -f ]`` guard turned the miss into a
    silent skip, so the vhost was rendered, never installed, and nothing said
    so. Both sides now read the same manifest entry.
    """

    def test_the_rendered_vhost_is_the_one_installed(self, plans):
        plan = plans["nginx_derived_vhost_name"]
        copies = [c for c in plan if "sites-available" in c and c.startswith("sudo cp")]

        assert any("proj_api_production.conf" in c for c in copies), (
            f"the derived-name vhost was not installed: {copies}"
        )

    def test_it_is_symlinked_into_sites_enabled(self, plans):
        plan = plans["nginx_derived_vhost_name"]

        assert any(
            "sites-enabled/proj_api_production" in c for c in plan if "ln -sf" in c
        )


class TestRetentionUnitsReachOnlyTheReceivingHost:
    """#339, pinned in the plan rather than only in the renderer's tests.

    The renderer's own tests assert what it writes. This asserts what a
    given host *plans to install* after `_env_active` has run against a
    real hostname — which is where "env-owned" would quietly become
    "unconditional".
    """

    def test_only_the_receiving_host_installs_the_retention_units(self, plans):
        receiving = " ".join(plans["receiving_host_with_retention"])
        other = " ".join(plans["non_receiving_host"])

        assert "retain-production-full.service" in receiving
        assert "retain-production-full.timer" in receiving
        assert "retain-" not in other, (
            "a host with no retain policy for its environments planned a "
            "retention install; the units are env-owned, not unconditional"
        )

    def test_both_entries_in_the_environment_get_a_pair(self, plans):
        """The second entry takes its name from the dir basename."""
        receiving = " ".join(plans["receiving_host_with_retention"])

        assert "retain-analytics.service" in receiving
        assert "retain-analytics.timer" in receiving

    def test_the_timer_is_enabled_and_the_service_is_not(self, plans):
        """The timer is the schedule; enabling the service too would run the
        prune once at boot as well."""
        ops = [c for c in plans["receiving_host_with_retention"] if "retain-" in c]
        enables = [c for c in ops if "enable" in c]

        assert len(enables) == 2, f"expected one enable per timer: {enables}"
        assert all(c.endswith(".timer") for c in enables), enables

    def test_the_pair_is_copied_before_the_timer_is_enabled(self, plans):
        """Enabling a timer whose .service is not on disk is backup.timer's
        bug with the ordering inverted."""
        ops = [c for c in plans["receiving_host_with_retention"] if "retain-" in c]
        verbs = [c.split()[2] if "systemctl" in c else "cp" for c in ops]

        assert verbs.count("cp") == 4, f"expected two pairs copied: {ops}"
        assert max(i for i, v in enumerate(verbs) if v == "cp") < verbs.index(
            "enable"
        ), f"a timer is enabled before every unit is on disk: {ops}"

    def test_daemon_reload_separates_the_copies_from_the_enable(self, plans):
        """`enable --now` on a unit systemd has not re-read starts the old one."""
        plan = plans["receiving_host_with_retention"]
        last_copy = max(i for i, c in enumerate(plan) if "retain-" in c and "cp" in c)
        first_enable = min(
            i for i, c in enumerate(plan) if "retain-" in c and "enable" in c
        )
        between = plan[last_copy:first_enable]

        assert any("daemon-reload" in c for c in between), (
            f"no daemon-reload between the copies and the enable: {between}"
        )

    def test_the_pre_existing_timers_are_still_not_enabled(self, plans):
        """Decision 3, pinned in the plan for every case in the matrix.

        `Disposition.TIMER` enables the retention timers and only those.
        Enabling backup.timer on upgrade would start a legacy
        `pg_dump | gzip` on every host that has ever run scaffold-install,
        as a side effect of a retention fix. If this test is failing, that
        is what you are shipping — do it in its own release.
        """
        for case, plan in plans.items():
            enabled = [c for c in plan if "enable" in c]
            for inert in ("backup.timer", "deploy-checker.timer"):
                assert not [c for c in enabled if c.endswith(inert)], (
                    f"{case}: {inert} is now enabled on upgrade"
                )

    def test_the_receiving_host_does_not_create_the_corpus_directory(self, plans):
        """A directory install.sh conjured into existence would turn a typo'd
        `dir:` into a prune that quietly finds nothing, replacing the loud
        error `backup prune` raises today."""
        plan = " ".join(plans["receiving_host_with_retention"])

        assert "mkdir -p /backup/production" not in plan
        assert "mkdir -p /backup/analytics" not in plan

    def test_a_non_receiving_host_plans_nothing_for_retention(self, plans):
        """Not even a daemon-reload.

        The tree is rendered for every environment in the config, so the
        retention block is present in install.sh on hosts that install none
        of it. An unconditional reload there is a planned command matching
        no work — caught by this pin on the first regeneration.
        """
        receiving = plans["receiving_host_with_retention"]
        other = plans["non_receiving_host"]
        reloads = "sudo systemctl daemon-reload"

        assert not [c for c in other if "retain-" in c]
        assert receiving.count(reloads) == other.count(reloads) + 1, (
            "the retention block must add exactly one reload, and only where "
            "it installs something"
        )
