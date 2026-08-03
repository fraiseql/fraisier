"""The webhook unit installed on a host must be the one rendered for it (#325).

The deploy path regenerates the scaffold tree with no ``--server``
(``deployers/base.py``), so ``_render_webhook_services`` takes its
auto-per-server branch and writes **only** slugged
``fraisier-{project}-webhook-{slug}.service`` files. Every installer — the
generated ``install.sh``, ``ServerSetup._plan_webhook_service`` and
``ScaffoldRenderer.get_install_mapping`` — asks for the *unslugged*
``fraisier-{project}-webhook.service``, a name that render never produced.
``install.sh`` guarded the copy with ``if [ -f ]``, so the step was silently
skipped, or silently copied whatever stale unslugged file survived in the
state dir from an earlier, differently-filtered render.

The reported symptom (printoptim.io, 2026-08-03) was a production-only host
running the webhook unit built for the dev host: ``ProtectSystem=strict`` plus
a ``ReadWritePaths=`` list holding the *other* machine's git/www trees, so
``git fetch`` in the bare repo failed with exit 255 and production could not
deploy. The per-server filter was never wrong; the file carrying its output
was never the file that got installed.

The fix puts the host in the *source* filename and selects at install time
from the hostname map ``install.sh`` already bakes. The destination unit name
is unchanged. Three invariants carry it, and each is pinned below:

(M) Mode is a function of the config alone, never of ``--server``.
(N) No fallback — a leftover unslugged unit is never installed.
(C) Every environment resolves to exactly one host, and every hosted
    environment's trees appear in that host's unit.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

# A printoptim-shaped config: two logical servers, development+staging sharing
# one machine, production alone on the other. `server:` in the global
# `environments:` section.
_GLOBAL_SERVERS = """\
name: myproj
scaffold:
  deploy_user: fraisier
  output_dir: {out}
servers:
  printoptim-dev:
    machine_hostnames: [pdev]
  printoptim-io:
    machine_hostnames: [pio]
environments:
  development:
    server: printoptim-dev
  staging:
    server: printoptim-dev
  production:
    server: printoptim-io
fraises:
  api:
    type: api
    environments:
      development:
        app_path: /var/www/api.dev
        git_repo: /var/git/api.dev.git
      staging:
        app_path: /var/www/api.st
        git_repo: /var/git/api.st.git
      production:
        app_path: /var/www/api.io
        git_repo: /var/git/api.io.git
"""

# The same topology expressed only under `fraises.*.environments.*`, with no
# global `environments:` section at all. `get_environments_for_server` reads
# both sources; `_collect_unique_servers` used to read only the global one, so
# this shape resolved to "no servers configured" and rendered a single unit
# carrying every host's trees — the #62 least-privilege leak by a second route.
_PER_FRAISE_SERVERS = """\
name: myproj
scaffold:
  deploy_user: fraisier
  output_dir: {out}
servers:
  printoptim-dev:
    machine_hostnames: [pdev]
  printoptim-io:
    machine_hostnames: [pio]
fraises:
  api:
    type: api
    environments:
      development:
        server: printoptim-dev
        app_path: /var/www/api.dev
        git_repo: /var/git/api.dev.git
      staging:
        server: printoptim-dev
        app_path: /var/www/api.st
        git_repo: /var/git/api.st.git
      production:
        server: printoptim-io
        app_path: /var/www/api.io
        git_repo: /var/git/api.io.git
"""

# One environment declares a server, the other does not — the shape a partial
# migration leaves behind. `get_environments_for_server` matches on exact
# equality, so `production` belongs to no logical server and its trees land in
# no webhook unit at all.
_PARTIAL_SERVERS = """\
name: myproj
scaffold:
  deploy_user: fraisier
  output_dir: {out}
servers:
  printoptim-dev:
    machine_hostnames: [pdev]
  printoptim-io:
    machine_hostnames: [pio]
environments:
  development:
    server: printoptim-dev
fraises:
  api:
    type: api
    environments:
      development:
        app_path: /var/www/api.dev
        git_repo: /var/git/api.dev.git
      production:
        app_path: /var/www/api.io
        git_repo: /var/git/api.io.git
"""

# Both declaration sites at once: the global section names the server, the
# per-fraise config repeats it. Whichever site a config uses, the answer to
# "which logical servers exist" must be the same.
_BOTH_SITES = """\
name: myproj
scaffold:
  deploy_user: fraisier
  output_dir: {out}
servers:
  printoptim-dev:
    machine_hostnames: [pdev]
  printoptim-io:
    machine_hostnames: [pio]
environments:
  development:
    server: printoptim-dev
  staging:
    server: printoptim-dev
  production:
    server: printoptim-io
fraises:
  api:
    type: api
    environments:
      development:
        server: printoptim-dev
        app_path: /var/www/api.dev
        git_repo: /var/git/api.dev.git
      staging:
        server: printoptim-dev
        app_path: /var/www/api.st
        git_repo: /var/git/api.st.git
      production:
        server: printoptim-io
        app_path: /var/www/api.io
        git_repo: /var/git/api.io.git
"""

_DEV_TREES = ("/var/git/api.dev.git", "/var/www/api.dev")
_STAGING_TREES = ("/var/git/api.st.git", "/var/www/api.st")
_PROD_TREES = ("/var/git/api.io.git", "/var/www/api.io")


class _NullRunner:
    """ServerSetup only needs a runner to exist while planning."""

    def run(self, *args, **kwargs):  # pragma: no cover - never invoked
        raise AssertionError("plan() must not execute commands")


def _render(tmp_path, yaml_text: str, *, server: str | None = None):
    """Render *yaml_text* into ``tmp_path/out`` and return (config, out_dir)."""
    cfg_path = tmp_path / "fraises.yaml"
    out = tmp_path / "out"
    cfg_path.write_text(yaml_text.format(out=out))
    config = FraisierConfig(cfg_path)
    ScaffoldRenderer(config, server=server).render()
    return config, out


def _rw_paths(text: str) -> list[str]:
    return [
        ln.split("=", 1)[1].strip()
        for ln in text.splitlines()
        if ln.startswith("ReadWritePaths=")
    ]


def _run_install_sh(out_dir, hostname: str, *extra_args) -> subprocess.CompletedProcess:
    """Run the generated ``install.sh`` with ``hostname -s`` stubbed to *hostname*."""
    bin_dir = out_dir.parent / f"bin-{hostname}"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "hostname"
    fake.write_text(f"#!/bin/bash\necho {hostname}\n")
    fake.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    install_sh = out_dir / "install.sh"
    install_sh.chmod(0o755)
    return subprocess.run(
        ["bash", str(install_sh), "--standalone", "--dry-run", *extra_args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _installed_webhook_source(
    result: subprocess.CompletedProcess, project: str = "myproj"
) -> Path | None:
    """Return the source path install.sh would copy to the webhook unit slot.

    Parses the ``[would run] sudo cp <src> <dst>`` line whose destination is
    ``/etc/systemd/system/fraisier-{project}-webhook.service``. Returns None
    when the copy never happens — which is the #325 silent skip.
    """
    dst = f"/etc/systemd/system/fraisier-{project}-webhook.service"
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts[-1:] == [dst] and "cp" in parts:
            return Path(parts[-2])
    return None


class TestOneResolverForWhichEnvironmentsAreLocal:
    """Claim 4 — two functions must not disagree about where a server is declared.

    ``_collect_unique_servers`` read only the global ``environments:`` section
    while ``get_environments_for_server`` — and therefore
    ``get_machine_environment_map``, which bakes install.sh's host gating —
    read the per-fraise configs as well. A config declaring ``server:`` only
    under ``fraises.*`` looked server-less to the renderer (one unit, every
    host's trees) and correctly filtered to the installer.
    """

    @pytest.mark.parametrize(
        "yaml_text",
        [_GLOBAL_SERVERS, _PER_FRAISE_SERVERS, _BOTH_SITES],
        ids=["global", "per-fraise", "both"],
    )
    def test_server_set_is_identical_across_declaration_sites(
        self, tmp_path, yaml_text
    ):
        from fraisier.scaffold.renderer import _collect_unique_servers

        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(yaml_text.format(out=tmp_path / "out"))
        config = FraisierConfig(cfg_path)

        assert set(_collect_unique_servers(config)) == {
            "printoptim-dev",
            "printoptim-io",
        }

    @pytest.mark.parametrize(
        "yaml_text",
        [_GLOBAL_SERVERS, _PER_FRAISE_SERVERS, _BOTH_SITES],
        ids=["global", "per-fraise", "both"],
    )
    def test_renderer_and_installer_agree_on_the_environment_split(
        self, tmp_path, yaml_text
    ):
        """The two resolvers must partition environments the same way."""
        from fraisier.scaffold.renderer import _collect_unique_servers

        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(yaml_text.format(out=tmp_path / "out"))
        config = FraisierConfig(cfg_path)

        split = {
            server: set(config.get_environments_for_server(server))
            for server in _collect_unique_servers(config)
        }
        assert split == {
            "printoptim-dev": {"development", "staging"},
            "printoptim-io": {"production"},
        }


class TestTheUnitAHostInstallsIsTheUnitRenderedForIt:
    """RED 1.1 to 1.3 - end-to-end from hostname to the installed unit's contents.

    The assertion runs through the generated ``install.sh`` on purpose: the
    per-server *filter* was already correct, so any test that reads a slugged
    file directly passes even with the bug present. Only asking the installer
    which file it picks reproduces #325.
    """

    def test_prod_machine_gets_only_production_trees(self, tmp_path):
        """RED 1.1 — pio hosts production alone, and installs exactly that."""
        _, out = _render(tmp_path, _GLOBAL_SERVERS)

        result = _run_install_sh(out, "pio")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        src = _installed_webhook_source(result)
        assert src is not None, (
            "install.sh installed no webhook unit at all — the deploy-path "
            f"render writes only slugged files. stdout: {result.stdout}"
        )

        paths = _rw_paths((out / src.name).read_text())
        for tree in _PROD_TREES:
            assert tree in paths, f"production tree {tree} missing from {paths}"
        for tree in _DEV_TREES + _STAGING_TREES:
            assert tree not in paths, f"foreign tree {tree} present in {paths}"

    def test_dev_machine_gets_development_and_staging_trees(self, tmp_path):
        """RED 1.2 — the mirror, so the fix cannot be 'always emit everything'."""
        _, out = _render(tmp_path, _GLOBAL_SERVERS)

        result = _run_install_sh(out, "pdev")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        src = _installed_webhook_source(result)
        assert src is not None, f"no webhook unit installed. stdout: {result.stdout}"

        paths = _rw_paths((out / src.name).read_text())
        for tree in _DEV_TREES + _STAGING_TREES:
            assert tree in paths, f"hosted tree {tree} missing from {paths}"
        for tree in _PROD_TREES:
            assert tree not in paths, f"foreign tree {tree} present in {paths}"

    def test_per_fraise_server_declaration_still_filters(self, tmp_path):
        """RED 1.3 — `server:` under fraises.* only, the #62 leak's second route."""
        _, out = _render(tmp_path, _PER_FRAISE_SERVERS)

        result = _run_install_sh(out, "pio")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        src = _installed_webhook_source(result)
        assert src is not None, f"no webhook unit installed. stdout: {result.stdout}"

        paths = _rw_paths((out / src.name).read_text())
        for tree in _PROD_TREES:
            assert tree in paths, f"production tree {tree} missing from {paths}"
        for tree in _DEV_TREES + _STAGING_TREES:
            assert tree not in paths, (
                f"foreign tree {tree} present in {paths} — `server:` declared "
                "only per-fraise must filter exactly like the global section"
            )


class TestInstallShSelectsTheWebhookUnitByHostname:
    """RED 1.4 — the selection itself, at the shell level."""

    def test_each_machine_selects_its_own_slugged_source(self, tmp_path):
        _, out = _render(tmp_path, _GLOBAL_SERVERS)

        for machine, slug in (("pdev", "printoptim-dev"), ("pio", "printoptim-io")):
            result = _run_install_sh(out, machine)
            assert result.returncode == 0, f"{machine}: stderr={result.stderr}"
            src = _installed_webhook_source(result)
            assert src is not None, f"{machine}: no webhook unit installed"
            assert src.name == f"fraisier-myproj-webhook-{slug}.service", (
                f"{machine} selected {src!r}"
            )

    def test_destination_unit_name_is_unchanged(self, tmp_path):
        """Only the source filename gains the host — no unit rename on the box."""
        _, out = _render(tmp_path, _GLOBAL_SERVERS)

        result = _run_install_sh(out, "pio")
        assert "/etc/systemd/system/fraisier-myproj-webhook.service" in result.stdout, (
            result.stdout
        )


class TestNoFallbackToAnUnsluggedLeftover:
    """RED 1.5 — invariant (N). A stale unslugged unit is never installed."""

    def test_stale_unslugged_unit_is_ignored(self, tmp_path):
        """The exact #325 mechanism: a leftover shadows the host's real unit."""
        _, out = _render(tmp_path, _GLOBAL_SERVERS)
        stale = out / "fraisier-myproj-webhook.service"
        stale.write_text(
            "[Service]\n"
            "ProtectSystem=strict\n"
            "ReadWritePaths=/var/git/api.dev.git\n"
            "ReadWritePaths=/var/www/api.dev\n"
        )

        result = _run_install_sh(out, "pio")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        src = _installed_webhook_source(result)
        assert src is not None, "no webhook unit installed"
        assert src.name == "fraisier-myproj-webhook-printoptim-io.service", (
            f"installed the stale unslugged leftover instead: {src}"
        )

    def test_missing_slugged_unit_is_a_hard_error(self, tmp_path):
        """A known machine whose unit is absent must fail, never skip."""
        _, out = _render(tmp_path, _GLOBAL_SERVERS)
        (out / "fraisier-myproj-webhook-printoptim-io.service").unlink()

        result = _run_install_sh(out, "pio")
        assert result.returncode != 0, (
            "install.sh continued past a missing webhook unit — that silent "
            f"skip is #325. stdout: {result.stdout}"
        )
        assert "webhook" in result.stderr.lower(), result.stderr

    def test_missing_slugged_unit_does_not_reach_for_the_unslugged_one(self, tmp_path):
        _, out = _render(tmp_path, _GLOBAL_SERVERS)
        (out / "fraisier-myproj-webhook-printoptim-io.service").unlink()
        (out / "fraisier-myproj-webhook.service").write_text("[Service]\n")

        result = _run_install_sh(out, "pio")
        src = _installed_webhook_source(result)
        assert src is None, f"fell back to {src!r}"


class TestModeIsAFunctionOfTheConfigAlone:
    """Invariant (M) — ``--server`` narrows the render, it never flips the mode.

    A tree is in multi-host mode iff *any* environment declares a ``server:``.
    ``--server`` selects which slugged units a render emits; it can never make
    a multi-host config produce an unslugged unit, and it can never make a
    single-host config produce a slugged one.

    This is the test that licenses Phase 5's decision to leave
    ``_regenerate_scaffold`` unfiltered. That decision is conditional, not
    free: an unfiltered regen of a multi-host config is safe only because it
    emits every slug and no unslugged unit (M), and because the installer
    refuses to install an unslugged leftover (N). **If (M) or (N) is ever
    relaxed, ``_regenerate_scaffold`` must start passing ``--server`` in the
    same commit** — this class is the tripwire.
    """

    def test_multi_host_config_never_emits_an_unslugged_unit(self, tmp_path):
        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(_GLOBAL_SERVERS.format(out=tmp_path / "out"))
        config = FraisierConfig(cfg_path)

        files = ScaffoldRenderer(config).render(dry_run=True)

        assert "fraisier-myproj-webhook.service" not in files
        assert "fraisier-myproj-webhook-printoptim-dev.service" in files
        assert "fraisier-myproj-webhook-printoptim-io.service" in files

    def test_server_filter_narrows_which_slugs_not_the_mode(self, tmp_path):
        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(_GLOBAL_SERVERS.format(out=tmp_path / "out"))
        config = FraisierConfig(cfg_path)

        files = ScaffoldRenderer(config, server="printoptim-io").render(dry_run=True)

        assert "fraisier-myproj-webhook-printoptim-io.service" in files
        assert "fraisier-myproj-webhook-printoptim-dev.service" not in files
        assert "fraisier-myproj-webhook.service" not in files, (
            "a --server render wrote the host-agnostic name — that file is what "
            "goes stale and then gets installed on the wrong box (#325)"
        )

    def test_single_host_config_emits_only_the_unslugged_unit(self, tmp_path):
        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(
            _GLOBAL_SERVERS.format(out=tmp_path / "out")
            .replace("    server: printoptim-dev\n", "")
            .replace("    server: printoptim-io\n", "")
        )
        config = FraisierConfig(cfg_path)

        files = ScaffoldRenderer(config).render(dry_run=True)

        assert "fraisier-myproj-webhook.service" in files
        assert not [f for f in files if f.startswith("fraisier-myproj-webhook-")]

    def test_the_two_renders_agree_on_the_mode(self, tmp_path):
        """The same config never yields slugged units in one render and an
        unslugged one in another."""
        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(_GLOBAL_SERVERS.format(out=tmp_path / "out"))
        config = FraisierConfig(cfg_path)

        unfiltered = ScaffoldRenderer(config).render(dry_run=True)
        filtered = ScaffoldRenderer(config, server="printoptim-dev").render(
            dry_run=True
        )

        def slugged(files):
            return {f for f in files if f.startswith("fraisier-myproj-webhook")}

        assert all("-webhook-" in f for f in slugged(unfiltered))
        assert all("-webhook-" in f for f in slugged(filtered))
        assert slugged(filtered) <= slugged(unfiltered)


class TestTheMachineToUnitMapReachesTheContext:
    """Cycle 3.2 — the inversion of ``servers:`` install.sh selects from.

    ``validate_servers`` already rejects a machine listed under two logical
    servers, so the inversion is unambiguous and needs no second check here.
    """

    def test_every_machine_in_servers_has_an_entry(self, tmp_path):
        from fraisier.scaffold.renderer import _build_context

        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(_GLOBAL_SERVERS.format(out=tmp_path / "out"))
        config = FraisierConfig(cfg_path)

        assert _build_context(config)["machine_webhook_map"] == {
            "pdev": "fraisier-myproj-webhook-printoptim-dev.service",
            "pio": "fraisier-myproj-webhook-printoptim-io.service",
        }

    def test_single_host_mode_has_no_map(self, tmp_path):
        """No slugged units exist, so there is nothing to select between."""
        from fraisier.scaffold.renderer import _build_context

        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(
            _GLOBAL_SERVERS.format(out=tmp_path / "out")
            .replace("    server: printoptim-dev\n", "")
            .replace("    server: printoptim-io\n", "")
        )
        config = FraisierConfig(cfg_path)

        assert _build_context(config)["machine_webhook_map"] == {}


class TestServerSetupDoesNotDropItsServer:
    """``ServerSetup`` built its renderer with no server and dropped the filter.

    ``_resolve_allowed_environments`` auto-detects the host for its own
    actions, so the plan knew which environments were local while the tree it
    rendered did not. The two must not be able to disagree.
    """

    def test_explicit_server_reaches_the_renderer(self, tmp_path):
        from fraisier.setup import ServerSetup

        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(_GLOBAL_SERVERS.format(out=tmp_path / "out"))
        config = FraisierConfig(cfg_path)

        setup = ServerSetup(config, _NullRunner(), server="printoptim-io")

        assert setup._renderer.server == "printoptim-io"

    def test_the_plan_installs_the_unit_the_render_wrote(self, tmp_path):
        from fraisier.setup import ServerSetup

        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(_GLOBAL_SERVERS.format(out=tmp_path / "out"))
        config = FraisierConfig(cfg_path)

        setup = ServerSetup(config, _NullRunner(), server="printoptim-io")
        rendered = setup._renderer.render()
        (action,) = setup._plan_webhook_service()
        source = Path(action.command[-2])

        assert source.name in rendered, (
            f"the plan copies {source.name} but the render wrote {rendered}"
        )
        assert source.name == "fraisier-myproj-webhook-printoptim-io.service"
        assert (
            action.command[-1] == "/etc/systemd/system/fraisier-myproj-webhook.service"
        )


class TestStaleWebhookUnitsAreSweptFromTheTree:
    """The next render removes units no host can install.

    A file nothing writes and nothing deletes is the substrate of #325: the
    installer used to reach for exactly such a leftover. The sweep runs only
    on an unfiltered render — a ``--server`` render knows one host's share of
    the truth and must not delete units it was never asked to produce.
    """

    def test_unit_for_a_removed_server_is_deleted(self, tmp_path):
        _, out = _render(tmp_path, _GLOBAL_SERVERS)
        orphan = out / "fraisier-myproj-webhook-printoptim-old.service"
        orphan.write_text("[Service]\n")

        _render(tmp_path, _GLOBAL_SERVERS)

        assert not orphan.exists()

    def test_legacy_unslugged_unit_is_deleted_in_multi_host_mode(self, tmp_path):
        """The stale file #325 installed. Nothing in this mode may write it."""
        _, out = _render(tmp_path, _GLOBAL_SERVERS)
        legacy = out / "fraisier-myproj-webhook.service"
        legacy.write_text("[Service]\nReadWritePaths=/var/www/api.dev\n")

        _render(tmp_path, _GLOBAL_SERVERS)

        assert not legacy.exists()

    def test_a_filtered_render_does_not_sweep_other_hosts_units(self, tmp_path):
        _, out = _render(tmp_path, _GLOBAL_SERVERS)
        other = out / "fraisier-myproj-webhook-printoptim-dev.service"
        assert other.exists()

        _render(tmp_path, _GLOBAL_SERVERS, server="printoptim-io")

        assert other.exists(), (
            "a --server render deleted another host's unit — the state dir "
            "must stay valid for every machine"
        )

    def test_slugged_leftovers_are_deleted_in_single_host_mode(self, tmp_path):
        single_host = _GLOBAL_SERVERS.replace(
            "    server: printoptim-dev\n", ""
        ).replace("    server: printoptim-io\n", "")
        _, out = _render(tmp_path, single_host)
        leftover = out / "fraisier-myproj-webhook-printoptim-dev.service"
        leftover.write_text("[Service]\n")

        _render(tmp_path, single_host)

        assert not leftover.exists()
        assert (out / "fraisier-myproj-webhook.service").exists()


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores the mode bits this test relies on"
)
class TestDeployAbortsWhenItCannotWriteFromInsideTheSandbox:
    """Defense in depth: prove the write, do not infer it.

    The deploy already runs *inside* the unit's sandbox, so it needs no
    ``systemd-run`` — a create-and-unlink in each tree answers the exact
    question ``ProtectSystem=strict`` decides. This is the only check that
    catches the failure class generically, including for a hand-written unit
    no template fix can reach.

    A real write, not ``os.access``: ``access(2)`` consults the file mode and
    the caller's ids, which is a different question from "did the mount
    namespace this process is in make that path read-only", and it answers
    the wrong one confidently.

    The reported failure surfaced in ``_git_pull`` as ``git fetch`` exit 255.
    Landing one step earlier turns a raw exit code into a diagnosis.
    """

    @staticmethod
    def _deployer(tmp_path):
        from fraisier.deployers.api import APIDeployer

        app = tmp_path / "app"
        repo = tmp_path / "repo.git"
        app.mkdir()
        repo.mkdir()
        return (
            APIDeployer(
                {
                    "fraise_name": "api",
                    "environment": "production",
                    "app_path": str(app),
                    "git_repo": str(repo),
                }
            ),
            app,
            repo,
        )

    def test_unwritable_git_repo_aborts_with_a_diagnosis(self, tmp_path):
        from fraisier.errors import DeploymentError

        deployer, _, repo = self._deployer(tmp_path)
        repo.chmod(0o555)
        try:
            with pytest.raises(DeploymentError) as exc:
                deployer._validate_sandbox_writes()
        finally:
            repo.chmod(0o755)

        message = str(exc.value)
        assert str(repo) in message
        assert "ReadWritePaths" in message
        assert "webhook" in message

    def test_unwritable_app_path_aborts(self, tmp_path):
        from fraisier.errors import DeploymentError

        deployer, app, _ = self._deployer(tmp_path)
        app.chmod(0o555)
        try:
            with pytest.raises(DeploymentError) as exc:
                deployer._validate_sandbox_writes()
        finally:
            app.chmod(0o755)

        assert str(app) in str(exc.value)

    def test_writable_trees_pass_and_leave_nothing_behind(self, tmp_path):
        deployer, app, repo = self._deployer(tmp_path)

        deployer._validate_sandbox_writes()

        assert list(app.iterdir()) == []
        assert list(repo.iterdir()) == []

    def test_missing_tree_is_not_this_check_s_business(self, tmp_path):
        """A path that does not exist is a different diagnosis, made elsewhere."""
        from fraisier.deployers.api import APIDeployer

        deployer = APIDeployer(
            {
                "fraise_name": "api",
                "app_path": str(tmp_path / "nope"),
                "git_repo": str(tmp_path / "nope.git"),
            }
        )

        deployer._validate_sandbox_writes()

    def test_probe_runs_before_the_git_pull(self, tmp_path):
        """Ordering is the point: a diagnosis instead of `git fetch` exit 255."""
        import inspect

        from fraisier.deployers.api import APIDeployer

        body = inspect.getsource(APIDeployer.execute)
        assert body.index("_validate_sandbox_writes") < body.index("_git_pull")
        assert body.index("_validate_wrapper_scripts") < body.index(
            "_validate_sandbox_writes"
        )


class TestDoctorReadsTheInstalledUnitAgainstTheHostedTrees:
    """The #317 check's shape, widened from dump dirs to hosted env trees.

    Reads the *installed* unit rather than the rendered one on purpose: that
    is the upgrade-without-re-scaffold case, which is the likeliest way to
    still be broken after a template fix, and it is also the only way to see a
    hand-written unit.

    Warn, not fail — matching #317. A hard failure here would break hosts
    limping along on a hand-edited unit that works.
    """

    def _run(self, config):
        from fraisier import doctor

        return doctor.DOCTOR_CHECKS["webhook_hosted_trees_writable"].fn(config)

    def _config(self, tmp_path):
        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(_GLOBAL_SERVERS.format(out=tmp_path / "out"))
        return FraisierConfig(cfg_path)

    def _install(self, tmp_path, monkeypatch, body: str):
        unit = tmp_path / "installed-webhook.service"
        unit.write_text(body)
        monkeypatch.setattr("fraisier.doctor._installed_webhook_unit", lambda _p: unit)
        return unit

    def _pretend_host_is(self, monkeypatch, machine: str):
        monkeypatch.setattr(
            "fraisier.doctor._resolve_local_server",
            lambda _config: {"pdev": "printoptim-dev", "pio": "printoptim-io"}[machine],
        )

    def test_registered(self):
        from fraisier import doctor

        assert "webhook_hosted_trees_writable" in doctor.DOCTOR_CHECKS

    def test_warns_when_a_hosted_tree_is_missing(self, tmp_path, monkeypatch):
        """The exact production state: prod host, dev host's allowlist."""
        self._pretend_host_is(monkeypatch, "pio")
        self._install(
            tmp_path,
            monkeypatch,
            "[Service]\nProtectSystem=strict\n"
            "ReadWritePaths=/var/lib/fraisier\n"
            "ReadWritePaths=/var/git/api.dev.git\n"
            "ReadWritePaths=/var/www/api.dev\n",
        )

        result = self._run(self._config(tmp_path))

        assert result.status == "warn"
        assert "/var/git/api.io.git" in result.detail
        assert result.fix_hint is not None and "scaffold" in result.fix_hint

    def test_passes_when_every_hosted_tree_is_allowed(self, tmp_path, monkeypatch):
        self._pretend_host_is(monkeypatch, "pio")
        self._install(
            tmp_path,
            monkeypatch,
            "[Service]\nProtectSystem=strict\n"
            "ReadWritePaths=/var/lib/fraisier\n"
            "ReadWritePaths=/var/git/api.io.git\n"
            "ReadWritePaths=/var/www/api.io\n",
        )

        assert self._run(self._config(tmp_path)).status == "pass"

    def test_a_foreign_tree_is_not_this_check_s_finding(self, tmp_path, monkeypatch):
        """#62 is the other direction; this check owns the missing side only."""
        self._pretend_host_is(monkeypatch, "pio")
        self._install(
            tmp_path,
            monkeypatch,
            "[Service]\nProtectSystem=strict\n"
            "ReadWritePaths=/var/git/api.io.git\n"
            "ReadWritePaths=/var/www/api.io\n"
            "ReadWritePaths=/var/www/api.dev\n",
        )

        assert self._run(self._config(tmp_path)).status == "pass"

    def test_skips_when_the_host_is_unresolvable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "fraisier.doctor._resolve_local_server", lambda _config: None
        )
        self._install(tmp_path, monkeypatch, "[Service]\nProtectSystem=strict\n")

        assert self._run(self._config(tmp_path)).status == "skip"

    def test_skips_when_the_unit_is_not_installed(self, tmp_path, monkeypatch):
        self._pretend_host_is(monkeypatch, "pio")
        monkeypatch.setattr(
            "fraisier.doctor._installed_webhook_unit",
            lambda _p: tmp_path / "nope.service",
        )

        assert self._run(self._config(tmp_path)).status == "skip"

    def test_passes_when_the_sandbox_is_not_strict(self, tmp_path, monkeypatch):
        self._pretend_host_is(monkeypatch, "pio")
        self._install(
            tmp_path, monkeypatch, "[Service]\nReadWritePaths=/opt/fraisier\n"
        )

        assert self._run(self._config(tmp_path)).status == "pass"

    def test_skips_without_config(self):
        assert self._run(None).status == "skip"


class TestOptInActiveSandboxProbe:
    """``fraisier doctor --probe-sandbox`` — check the unit *before* installing.

    The passive checks read a path list and reason about it. This one runs a
    real write under a real ``ProtectSystem=strict`` sandbox built from the
    **rendered** unit's ``ReadWritePaths=``, so an operator can find out
    before ``scaffold-install`` rather than on the next deploy.

    Off by default and skipped without root: ``systemd-run`` needs
    privileges, and a check that fails for lack of them is noise.
    """

    def _run(self, config):
        from fraisier import doctor

        return doctor.DOCTOR_CHECKS["sandbox_write_probe"].fn(config)

    def _config(self, tmp_path):
        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(_GLOBAL_SERVERS.format(out=tmp_path / "out"))
        return FraisierConfig(cfg_path)

    def test_registered_and_opt_in(self):
        from fraisier import doctor

        entry = doctor.DOCTOR_CHECKS["sandbox_write_probe"]
        assert entry.privileged, "the probe must not run in a default doctor pass"

    def test_default_doctor_run_skips_it(self, tmp_path):
        from fraisier import doctor

        results = {
            r.name: r for r in doctor.run_all(self._config(tmp_path), skip_network=True)
        }
        assert results["sandbox_write_probe"].status == "skip"

    def test_skips_without_root(self, tmp_path, monkeypatch):
        from fraisier import doctor

        monkeypatch.setattr("fraisier.doctor.os.geteuid", lambda: 1000)
        result = doctor.run_all(
            self._config(tmp_path), only=["sandbox_write_probe"], probe_sandbox=True
        )[0]

        assert result.status == "skip"
        assert "root" in result.detail

    def test_probes_the_rendered_paths_for_this_host(self, tmp_path, monkeypatch):
        from fraisier import doctor

        monkeypatch.setattr("fraisier.doctor.os.geteuid", lambda: 0)
        monkeypatch.setattr(
            "fraisier.doctor._resolve_local_server", lambda _config: "printoptim-io"
        )
        seen: list[list[str]] = []
        monkeypatch.setattr(
            "fraisier.doctor._run_sandbox_probe",
            lambda paths: (seen.append(list(paths)), (0, ""))[1],
        )

        result = doctor.run_all(
            self._config(tmp_path), only=["sandbox_write_probe"], probe_sandbox=True
        )[0]

        assert result.status == "pass"
        assert seen and "/var/git/api.io.git" in seen[0]
        assert "/var/git/api.dev.git" not in seen[0], (
            "probed another host's trees — the probe must use the unit rendered "
            "for this machine"
        )

    def test_a_failing_probe_is_reported_as_fail(self, tmp_path, monkeypatch):
        from fraisier import doctor

        monkeypatch.setattr("fraisier.doctor.os.geteuid", lambda: 0)
        monkeypatch.setattr(
            "fraisier.doctor._resolve_local_server", lambda _config: "printoptim-io"
        )
        monkeypatch.setattr(
            "fraisier.doctor._run_sandbox_probe",
            lambda _paths: (1, "/var/git/api.io.git: Read-only file system"),
        )

        result = doctor.run_all(
            self._config(tmp_path), only=["sandbox_write_probe"], probe_sandbox=True
        )[0]

        assert result.status == "fail"
        assert "Read-only file system" in result.detail

    def test_probe_command_is_a_strict_sandbox_over_the_given_paths(self):
        from fraisier.doctor import _sandbox_probe_command

        argv = _sandbox_probe_command(["/var/git/api.io.git", "/var/www/api.io"])

        assert argv[0] == "systemd-run"
        assert "-pProtectSystem=strict" in argv
        assert "-pReadWritePaths=/var/git/api.io.git /var/www/api.io" in argv


class TestEveryEnvironmentResolvesToExactlyOneHost:
    """RED 1.6 — invariant (C), claim 6.

    An environment with no ``server:`` in a multi-host config belongs to no
    logical server, so its trees are rendered into no webhook unit. That is
    the #325 failure reached from a config that reads like a partial
    migration rather than a mistake. Reject at render: treating it as hosted
    *everywhere* would re-create the #62 leak by default and make the
    permissive reading the safe-looking one.
    """

    def test_a_unit_missing_a_hosted_tree_fails_the_render(self, tmp_path):
        """(C)'s other half, checked against the rendered text.

        #62 is this invariant violated one way (a host's unit carries another
        host's trees); #325 is the same invariant violated the other (a host's
        unit is missing its own). Asserting on the rendered
        ``ReadWritePaths=`` rather than on the context catches both, and
        catches them however the filtering was reached — a template
        regression included, which is what this stands in for.
        """
        templates = tmp_path / "templates" / "core"
        templates.mkdir(parents=True)
        (templates / "fraisier-webhook.service.j2").write_text(
            "[Service]\n"
            "ProtectSystem=strict\n"
            "ReadWritePaths=/var/lib/fraisier\n"
            "ReadWritePaths=/run/fraisier\n"
        )

        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(
            _GLOBAL_SERVERS.format(out=tmp_path / "out").replace(
                "  deploy_user: fraisier\n",
                "  deploy_user: fraisier\n  template_dir: templates\n",
            )
        )
        config = FraisierConfig(cfg_path)

        with pytest.raises(ValueError) as exc:
            ScaffoldRenderer(config).render()

        message = str(exc.value)
        assert "ReadWritePaths" in message
        assert "/var/git/api" in message or "/var/www/api" in message

    def test_environment_without_a_server_is_rejected(self, tmp_path):
        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(_PARTIAL_SERVERS.format(out=tmp_path / "out"))
        config = FraisierConfig(cfg_path)

        with pytest.raises(ValueError) as exc:
            ScaffoldRenderer(config).render()

        message = str(exc.value)
        assert "production" in message, message
        assert "server" in message.lower(), message

    def test_error_names_the_servers_it_could_be_assigned_to(self, tmp_path):
        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(_PARTIAL_SERVERS.format(out=tmp_path / "out"))
        config = FraisierConfig(cfg_path)

        with pytest.raises(ValueError) as exc:
            ScaffoldRenderer(config).render()

        assert "printoptim-dev" in str(exc.value), str(exc.value)


class TestTheInvariantSweep:
    """The whole contract, as a loop over the machine map.

    Written as a sweep rather than per-host literals so that adding a machine
    to a config cannot leave a hole in the test. Both directions of the same
    invariant are asserted together, because #62 and #325 are that invariant
    violated in opposite directions — too many paths, and too few.
    """

    @pytest.mark.parametrize(
        "yaml_text",
        [_GLOBAL_SERVERS, _PER_FRAISE_SERVERS, _BOTH_SITES],
        ids=["global", "per-fraise", "both"],
    )
    def test_every_machine_installs_exactly_its_own_trees(self, tmp_path, yaml_text):
        config, out = _render(tmp_path, yaml_text)

        every_tree = {
            str(env_config[key])
            for fraise in config.fraises.values()
            for env_config in fraise.get("environments", {}).values()
            for key in ("git_repo", "app_path")
            if env_config.get(key)
        }

        for machine in sorted(m for ms in config.servers.values() for m in ms):
            result = _run_install_sh(out, machine)
            assert result.returncode == 0, f"{machine}: {result.stderr}"
            source = _installed_webhook_source(result)
            assert source is not None, f"{machine}: no webhook unit installed"

            allowed = set(_rw_paths((out / source.name).read_text()))
            hosted_envs = {
                env
                for logical, machines in config.servers.items()
                if machine in machines
                for env in config.get_environments_for_server(logical)
            }
            mine = {
                str(env_config[key])
                for fraise in config.fraises.values()
                for env_name, env_config in fraise.get("environments", {}).items()
                if env_name in hosted_envs
                for key in ("git_repo", "app_path")
                if env_config.get(key)
            }

            assert mine <= allowed, f"{machine} is missing {sorted(mine - allowed)}"
            foreign = (every_tree - mine) & allowed
            assert not foreign, f"{machine} may write another host's {sorted(foreign)}"

    def test_state_dirs_are_shared_by_every_machine(self, tmp_path):
        """The three fraisier state dirs are identical on every host."""
        config, out = _render(tmp_path, _GLOBAL_SERVERS)
        shared = {"/opt/fraisier", "/var/lib/fraisier", "/run/fraisier"}

        for machine in sorted(m for ms in config.servers.values() for m in ms):
            source = _installed_webhook_source(_run_install_sh(out, machine))
            assert source is not None
            allowed = set(_rw_paths((out / source.name).read_text()))
            assert shared <= allowed, f"{machine} lacks {sorted(shared - allowed)}"


class TestTheActualDeployPathChain:
    """The exact argv `_regenerate_scaffold` runs, then the install (#325).

    Every other test in this file constructs the renderer directly. This one
    drives the real CLI with the real deploy-path arguments — ``-c <cfg>
    scaffold --output-dir <state_dir>``, **no ``--server``** — because that is
    the mode that produced the incident. The bootstrap path, which does pass
    ``--server``, was never the broken one.

    Kept separate and named for the caller so that a future change to
    `_regenerate_scaffold`'s argv (notably: starting to pass ``--server``,
    which invariants (M) and (N) currently make unnecessary) has to come past
    a test that spells the old argv out.
    """

    def _regenerate_as_the_deployer_does(self, tmp_path):
        from click.testing import CliRunner

        from fraisier.cli.main import main

        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(_GLOBAL_SERVERS.format(out=tmp_path / "unused-output-dir"))
        state_dir = tmp_path / "state" / "scaffold"

        result = CliRunner().invoke(
            main,
            ["-c", str(cfg_path), "scaffold", "--output-dir", str(state_dir)],
        )
        assert result.exit_code == 0, result.output
        return state_dir

    def test_regeneration_then_install_lands_the_prod_unit_on_the_prod_host(
        self, tmp_path
    ):
        state_dir = self._regenerate_as_the_deployer_does(tmp_path)

        install = _run_install_sh(state_dir, "pio")
        assert install.returncode == 0, install.stderr
        source = _installed_webhook_source(install)
        assert source is not None, (
            "the deploy path regenerated and the installer took nothing — "
            f"this is #325 verbatim. stdout: {install.stdout}"
        )
        assert source.name == "fraisier-myproj-webhook-printoptim-io.service"

        paths = _rw_paths((state_dir / source.name).read_text())
        for tree in _PROD_TREES:
            assert tree in paths
        for tree in _DEV_TREES + _STAGING_TREES:
            assert tree not in paths

    def test_the_same_regeneration_serves_the_dev_host(self, tmp_path):
        """One tree, valid for every machine — what the state dir is for."""
        state_dir = self._regenerate_as_the_deployer_does(tmp_path)

        install = _run_install_sh(state_dir, "pdev")
        assert install.returncode == 0, install.stderr
        source = _installed_webhook_source(install)
        assert source is not None
        assert source.name == "fraisier-myproj-webhook-printoptim-dev.service"

        paths = _rw_paths((state_dir / source.name).read_text())
        for tree in _DEV_TREES + _STAGING_TREES:
            assert tree in paths
        for tree in _PROD_TREES:
            assert tree not in paths

    def test_the_regeneration_writes_no_host_agnostic_unit(self, tmp_path):
        """Invariant (M) at the deploy path — the file that used to go stale."""
        state_dir = self._regenerate_as_the_deployer_does(tmp_path)

        assert not (state_dir / "fraisier-myproj-webhook.service").exists()
