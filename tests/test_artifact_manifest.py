"""The artifact manifest: what render() produced, and who installs it (#323).

``fraisier scaffold`` already knows precisely what it wrote — ``render()``
returns the list. That knowledge was thrown away, and three other components
reconstructed it by hand: sixteen hardcoded names in ``install.sh.j2``,
``get_install_mapping()``, and ``scheduled_install``'s directory scan. Every
bug in the "rendered ≠ installed" class lives in that gap.

The manifest closes it by construction. Its load-bearing property is not the
generic install — it is that **every rendered file must be classified**. An
artifact nobody dispositioned is a hard error naming the file, so a new
rendered artifact cannot be added without someone stating, in reviewable code,
whether it gets installed.
"""

from __future__ import annotations

import pytest

from fraisier.config import FraisierConfig
from fraisier.errors import ValidationError
from fraisier.scaffold.artifacts import (
    Disposition,
    build_artifact_manifest,
)
from fraisier.scaffold.renderer import ScaffoldRenderer
from tests.test_install_plan_golden import _GOLDEN
from tests.test_install_plan_golden import MATRIX as _MATRIX

_CONFIG = """\
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
        nginx:
          server_name: api.example.com
"""


# restore-staging's units render only under this strategy, and both of them
# are uninstalled — the one gap class this release deliberately leaves open,
# because unlike the others it is self-consistent: no installed timer fires
# into a missing unit.
_RESTORE_MIGRATE_CONFIG = """\
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
      staging:
        server: only.example.io
        app_path: /var/www/api-stg
        systemd_service: api-stg.service
        git_repo: /var/git/api-stg.git
        database:
          strategy: restore_migrate
"""


# A scheduled fraise, which is what brings the unit-installer helper into the
# tree — one per environment that has one.
_SCHEDULED_CONFIG = """\
name: proj
servers:
  only.example.io:
    machine_hostnames: [solo]
scaffold:
  deploy_user: deployer
fraises:
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


@pytest.fixture
def manifest(tmp_path):
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(_CONFIG)
    renderer = ScaffoldRenderer(FraisierConfig(cfg))
    renderer.output_dir = tmp_path / "out"
    rendered = renderer.render()
    return build_artifact_manifest(renderer, rendered)


def _by_source(manifest, source):
    for artifact in manifest.artifacts:
        if artifact.source == source:
            return artifact
    raise AssertionError(
        f"{source!r} not in manifest: {[a.source for a in manifest.artifacts]}"
    )


class TestCoverage:
    """The point of the whole exercise."""

    def test_every_rendered_file_is_classified(self, tmp_path):
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_CONFIG)
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tmp_path / "out"
        rendered = renderer.render()

        manifest = build_artifact_manifest(renderer, rendered)

        assert {a.source for a in manifest.artifacts} == set(rendered)

    def test_unclassified_artifact_is_a_hard_error_naming_the_file(self, tmp_path):
        """A new rendered artifact cannot slip in undispositioned."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_CONFIG)
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tmp_path / "out"
        rendered = renderer.render()

        with pytest.raises(ValidationError) as exc:
            build_artifact_manifest(renderer, [*rendered, "systemd/brand-new.service"])

        assert "brand-new.service" in str(exc.value)

    def test_the_error_says_what_to_do(self, tmp_path):
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_CONFIG)
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tmp_path / "out"
        rendered = renderer.render()

        with pytest.raises(ValidationError) as exc:
            build_artifact_manifest(renderer, [*rendered, "systemd/brand-new.service"])

        assert "disposition" in str(exc.value).lower()


class TestDispositions:
    """Each artifact routes to the handling its install actually needs."""

    def test_deploy_units_are_plain(self, manifest):
        artifact = _by_source(manifest, "systemd/fraisier-api-production.socket")

        assert artifact.disposition is Disposition.PLAIN
        assert artifact.destination == (
            "/etc/systemd/system/fraisier-api-production.socket"
        )
        assert artifact.environment == "production"

    def test_webhook_unit_keeps_its_own_sequence(self, manifest):
        artifact = _by_source(manifest, "fraisier-proj-webhook-only-example-io.service")

        assert artifact.disposition is Disposition.WEBHOOK
        # Source carries the host; destination never does (#325).
        assert (
            artifact.destination == "/etc/systemd/system/fraisier-proj-webhook.service"
        )

    def test_install_helper_units_are_rebake(self, manifest):
        """#279's sequence is not expressible as a generic copy."""
        artifact = _by_source(
            manifest, "systemd/fraisier-proj-api-production-install-helper.socket"
        )

        assert artifact.disposition is Disposition.HELPER_REBAKE

    def test_sudoers_carries_its_mode(self, manifest):
        artifact = _by_source(manifest, "sudoers")

        assert artifact.disposition is Disposition.SUDOERS
        assert artifact.mode == 0o440
        assert artifact.destination == "/etc/sudoers.d/proj"

    def test_nginx_vhost_is_its_own_disposition(self, manifest):
        """copy + sites-enabled symlink, not a plain copy."""
        artifact = _by_source(manifest, "nginx/gateway.conf")

        assert artifact.disposition is Disposition.NGINX_VHOST
        assert artifact.destination == "/etc/nginx/sites-available/proj"

    def test_scripts_run_from_the_scaffold_tree_are_not_installed(self, manifest):
        """install.sh, backup.sh and friends are consumed in place."""
        for source in ("install.sh", "backup.sh", "confiture.yaml", "deploy.yml"):
            assert _by_source(manifest, source).disposition is (
                Disposition.SCAFFOLD_LOCAL
            ), source

    def test_env_gated_artifacts_carry_their_environment(self, manifest):
        """So install.sh can gate on _env_active without re-deriving it."""
        artifact = _by_source(manifest, "systemd/api.service")

        assert artifact.environment == "production"

    def test_unconditional_artifacts_have_no_environment(self, manifest):
        assert _by_source(manifest, "sudoers").environment is None


class TestKnownGapsAreNamedNotHidden:
    """Artifacts that are rendered and should be installed, but are not.

    Classifying these as ``MANUAL`` would launder four live bugs into
    "intentional". They get their own disposition and a reason, so the gap is
    visible in the manifest and in ``doctor`` instead of being implied by the
    absence of an install line.
    """

    def test_every_gap_explains_its_consequence(self, manifest):
        """A gap without a stated consequence is indistinguishable from a
        deliberate omission, which is what the disposition exists to prevent."""
        for artifact in manifest.artifacts:
            if artifact.disposition is Disposition.UNINSTALLED_GAP:
                assert artifact.note, f"{artifact.source} is a gap with no note"

    def test_gaps_are_reported_where_they_are_rendered(self, tmp_path):
        """restore-staging renders only under a ``restore_migrate`` strategy.

        Unlike the four gaps this release closed, its .service and .timer are
        *both* uninstalled — self-consistent, so nothing fires into a missing
        unit. It stays a named gap rather than a silent one.
        """
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_RESTORE_MIGRATE_CONFIG)
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tmp_path / "out"
        rendered = renderer.render()

        manifest = build_artifact_manifest(renderer, rendered)
        gaps = {a.source for a in manifest.gaps()}

        assert "systemd/restore-staging.service" in gaps
        assert "systemd/restore-staging.timer" in gaps


class TestDeployCheckerServiceIsInstalled:
    """``deploy-checker.timer`` fires into ``deploy-checker.service``.

    The unit was rendered as ``poll-deploy.service``, at the tree root rather
    than under ``systemd/``, so the timer activated a name that existed
    nowhere. Renaming the rendered unit — rather than pointing the timer at
    the old name with ``Unit=`` — is what makes the fix land on hosts that are
    already running the timer: the timer file they have keeps working the
    moment the correctly-named service appears beside it.
    """

    def test_the_unit_is_rendered_under_the_name_its_timer_activates(self, manifest):
        artifact = _by_source(manifest, "systemd/deploy-checker.service")

        assert artifact.disposition is Disposition.PLAIN
        assert artifact.destination == "/etc/systemd/system/deploy-checker.service"

    def test_the_old_name_is_gone_entirely(self, manifest):
        """Not renamed *and* kept: two names for one unit is the bug."""
        sources = {a.source for a in manifest.artifacts}

        assert "poll-deploy.service" not in sources
        assert "systemd/poll-deploy.service" not in sources

    def test_it_is_no_longer_a_gap(self, manifest):
        assert "poll-deploy.service" not in {a.source for a in manifest.gaps()}

    def test_the_installed_name_is_the_one_the_timer_activates(
        self, manifest, tmp_path
    ):
        """With no ``Unit=``, the activated unit is the timer's own stem."""
        timer_unit = (tmp_path / "out" / "systemd" / "deploy-checker.timer").read_text()
        assert not any(ln.startswith("Unit=") for ln in timer_unit.splitlines()), (
            "the timer now names its target explicitly; this pin must follow it"
        )

        timer = _by_source(manifest, "systemd/deploy-checker.timer")
        service = _by_source(manifest, "systemd/deploy-checker.service")
        assert timer.destination is not None
        assert service.destination is not None

        activated = timer.destination.removesuffix(".timer")
        installed = service.destination.removesuffix(".service")

        assert activated == installed


class TestBackupServiceIsInstalled:
    """``backup.timer`` fires into ``backup.service``, which had to exist.

    The timer carries no ``Unit=``, so systemd activates the unit with its own
    stem. The timer was installed and the service was not, so every firing hit
    a missing unit.
    """

    def test_the_backup_service_is_installed(self, manifest):
        artifact = _by_source(manifest, "systemd/backup.service")

        assert artifact.disposition is Disposition.PLAIN
        assert artifact.destination == "/etc/systemd/system/backup.service"

    def test_it_installs_wherever_its_timer_does(self, manifest):
        """Both unconditional: an env gate on one and not the other is the
        asymmetry that produced this bug in the first place."""
        service = _by_source(manifest, "systemd/backup.service")
        timer = _by_source(manifest, "systemd/backup.timer")

        assert service.environment == timer.environment is None

    def test_the_installed_name_is_the_one_the_timer_activates(
        self, manifest, tmp_path
    ):
        """With no ``Unit=``, the activated unit is the timer's own stem."""
        timer_unit = (tmp_path / "out" / "systemd" / "backup.timer").read_text()
        assert not any(ln.startswith("Unit=") for ln in timer_unit.splitlines()), (
            "the timer now names its target explicitly; this pin must follow it"
        )

        timer = _by_source(manifest, "systemd/backup.timer")
        service = _by_source(manifest, "systemd/backup.service")
        assert timer.destination is not None
        assert service.destination is not None

        activated = timer.destination.removesuffix(".timer")
        installed = service.destination.removesuffix(".service")

        assert activated == installed


class TestBackupAlertUnitIsInstalled:
    """``backup.service``'s ``OnFailure=`` target has to exist to fire.

    A missing ``OnFailure=`` target does not fail loudly — systemd logs that it
    could not enqueue the job and the backup failure itself goes unannounced,
    which is precisely the case the alert unit exists to cover.
    """

    def test_the_alert_unit_is_installed(self, manifest):
        artifact = _by_source(manifest, "systemd/fraisier-proj-backup-alert@.service")

        assert artifact.disposition is Disposition.PLAIN
        assert (
            artifact.destination
            == "/etc/systemd/system/fraisier-proj-backup-alert@.service"
        )

    def test_it_installs_on_every_host(self, manifest):
        """Rendered unconditionally, so it cannot be gated on an environment."""
        artifact = _by_source(manifest, "systemd/fraisier-proj-backup-alert@.service")

        assert artifact.environment is None

    def test_the_name_backup_service_references_is_the_name_installed(
        self, manifest, tmp_path
    ):
        """The referenced unit and the installed unit must be the same name.

        ``OnFailure=`` names a unit; the manifest installs a file. Two places
        deriving one name is the shape this whole bundle exists to remove, so
        the reference is checked against the destination rather than assumed.
        """
        alert = _by_source(manifest, "systemd/fraisier-proj-backup-alert@.service")
        backup_unit = (tmp_path / "out" / "systemd" / "backup.service").read_text()

        on_failure = next(
            line.split("=", 1)[1].strip()
            for line in backup_unit.splitlines()
            if line.startswith("OnFailure=")
        )
        # OnFailure passes the failed unit as the instance (`@%n.service`);
        # the installed file is the template it instantiates.
        template = on_failure.split("@", 1)[0] + "@.service"

        assert alert.destination == f"/etc/systemd/system/{template}"


class TestUnitInstallerHelperIsInstalled:
    """The socket ``scheduled-install`` requires, and told operators to get by
    running the installer that never installed it.

    Both sides pointed at each other: ``scheduled_install`` raises "run
    'fraisier scaffold-install --yes' to bootstrap" and ``scaffold-install``
    had no line for these units at all.
    """

    @pytest.fixture
    def scheduled_manifest(self, tmp_path):
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_SCHEDULED_CONFIG)
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tmp_path / "out"
        rendered = renderer.render()
        return build_artifact_manifest(renderer, rendered)

    def test_both_units_are_installed(self, scheduled_manifest):
        for suffix in ("socket", "service"):
            artifact = _by_source(
                scheduled_manifest,
                f"systemd/fraisier-proj-production-unit-installer.{suffix}",
            )

            assert artifact.disposition is Disposition.UNIT_INSTALLER
            assert artifact.destination == (
                f"/etc/systemd/system/fraisier-proj-production-unit-installer.{suffix}"
            )

    def test_they_are_gated_on_their_environment(self, scheduled_manifest):
        """One helper per environment: a prod-only host must not install dev's."""
        artifact = _by_source(
            scheduled_manifest,
            "systemd/fraisier-proj-production-unit-installer.socket",
        )

        assert artifact.environment == "production"

    def test_they_are_no_longer_a_gap(self, scheduled_manifest):
        assert not [
            a for a in scheduled_manifest.gaps() if "unit-installer" in a.source
        ]

    def test_the_pair_is_returned_together(self, scheduled_manifest):
        """The re-bake acts on both units at once, so it needs them paired."""
        pairs = scheduled_manifest.unit_installer_pairs()

        assert len(pairs) == 1
        assert pairs[0].environment == "production"
        assert pairs[0].socket_unit == "fraisier-proj-production-unit-installer.socket"
        assert (
            pairs[0].service_unit == "fraisier-proj-production-unit-installer.service"
        )

    def test_a_half_rendered_pair_is_an_error_not_a_skip(self, tmp_path):
        """The sequence cannot run on half a pair, and a silent skip is #323."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_SCHEDULED_CONFIG)
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tmp_path / "out"
        rendered = renderer.render()
        without_socket = [
            s for s in rendered if not s.endswith("unit-installer.socket")
        ]

        manifest = build_artifact_manifest(renderer, without_socket)

        with pytest.raises(ValidationError) as exc:
            manifest.unit_installer_pairs()

        assert "production" in str(exc.value)

    def test_the_units_are_named_by_the_shared_helper(self, scheduled_manifest):
        """Renderer and manifest must not derive this name independently."""
        from fraisier.naming import unit_installer_unit_names

        socket_unit, service_unit = unit_installer_unit_names("proj", "production")
        sources = {a.source for a in scheduled_manifest.artifacts}

        assert f"systemd/{socket_unit}" in sources
        assert f"systemd/{service_unit}" in sources


class TestBatchHashBindsManifestToTree:
    """An old manifest against new renders reintroduces the gap one level up."""

    def test_manifest_records_a_batch_hash(self, manifest):
        assert manifest.batch_hash

    def test_each_artifact_records_its_content_hash(self, manifest):
        artifact = _by_source(manifest, "sudoers")

        assert artifact.sha256 and len(artifact.sha256) == 64

    def test_batch_hash_moves_when_an_artifact_changes(self, tmp_path):
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_CONFIG)
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tmp_path / "out"
        rendered = renderer.render()
        before = build_artifact_manifest(renderer, rendered).batch_hash

        (tmp_path / "out" / "sudoers").write_text("tampered\n")
        after = build_artifact_manifest(renderer, rendered).batch_hash

        assert before != after

    def test_batch_hash_covers_routing_not_just_content(self, tmp_path):
        """Two manifests over identical bytes but different destinations differ.

        Otherwise a stale manifest listing matching filenames would satisfy the
        check while sending them somewhere else.
        """
        from dataclasses import replace

        from fraisier.scaffold.artifacts import _batch_hash

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_CONFIG)
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tmp_path / "out"
        artifacts = list(build_artifact_manifest(renderer, renderer.render()).artifacts)

        moved = [
            replace(artifacts[0], destination="/etc/somewhere/else"),
            *artifacts[1:],
        ]

        assert _batch_hash(artifacts) != _batch_hash(moved)

    def test_batch_hash_is_stable_across_identical_renders(self, tmp_path):
        hashes = set()
        for i in range(2):
            cfg = tmp_path / f"fraises{i}.yaml"
            cfg.write_text(_CONFIG)
            renderer = ScaffoldRenderer(FraisierConfig(cfg))
            renderer.output_dir = tmp_path / f"out{i}"
            rendered = renderer.render()
            hashes.add(build_artifact_manifest(renderer, rendered).batch_hash)

        assert len(hashes) == 1


class TestManifestModelsTheRealInstaller:
    """The bridge that makes the refactor safe.

    The manifest is only useful if it describes what ``install.sh`` actually
    does. This asserts that for every config in the golden matrix — both sides
    of the multi-host asymmetry included — the artifacts the manifest says get
    installed on a host are exactly the ones the real installer copies there.

    Run before install.sh consumes the manifest, so the model is proven
    faithful *first*; kept afterwards, so the two cannot drift apart later —
    which is the very failure this whole bundle is about.
    """

    @staticmethod
    def _installed_on_host(renderer, manifest, hostname: str) -> set[str]:
        """Manifest entries this host installs, applying the same two filters
        install.sh applies at runtime: ``_env_active`` and per-host webhook
        selection."""
        envs = set(renderer.context["machine_env_map"].get(hostname, []))
        webhook = renderer.context["machine_webhook_map"].get(hostname)
        return {
            a.source
            for a in manifest.installed()
            if (a.environment is None or a.environment in envs)
            and (a.disposition is not Disposition.WEBHOOK or a.source == webhook)
        }

    @pytest.mark.parametrize(
        ("case", "yaml_text", "hostname"),
        [pytest.param(*row, id=row[0]) for row in _MATRIX],
    )
    def test_manifest_install_set_equals_the_golden_plan(
        self, tmp_path, case, yaml_text, hostname
    ):
        import json

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(yaml_text)
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tmp_path / "out"
        manifest = build_artifact_manifest(renderer, renderer.render())

        golden = json.loads(_GOLDEN.read_text())[case]
        planned = {
            token.removeprefix("$SCAFFOLD/")
            for command in golden
            for token in command.split()
            if token.startswith("$SCAFFOLD/")
        }

        assert self._installed_on_host(renderer, manifest, hostname) == planned

    def test_every_matrix_config_classifies_completely(self, tmp_path):
        """The coverage assertion holds for every shape, not just the fixture."""
        for i, (case, yaml_text, _) in enumerate(_MATRIX):
            cfg = tmp_path / f"fraises{i}.yaml"
            cfg.write_text(yaml_text)
            renderer = ScaffoldRenderer(FraisierConfig(cfg))
            renderer.output_dir = tmp_path / f"out{i}"
            rendered = renderer.render()

            manifest = build_artifact_manifest(renderer, rendered)

            assert {a.source for a in manifest.artifacts} == set(rendered), case


class TestInstallMappingDerivesFromTheManifest:
    """scaffold-diff and install.sh cannot disagree about destinations.

    ``get_install_mapping`` was the third hand-maintained authority, and it had
    drifted in three ways from the installer it was supposed to mirror.
    """

    def test_mapping_matches_the_manifest(self, tmp_path):
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_CONFIG)
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tmp_path / "out"
        renderer.render()

        mapping = renderer.get_install_mapping()

        for source, dest in mapping.items():
            artifact = _by_source(renderer.artifact_manifest, source)
            assert str(dest) == artifact.destination

    def test_mapping_no_longer_claims_uninstalled_artifacts(self, tmp_path):
        """It used to map poll-deploy.service and the restore-staging units.

        None of them is installed by anything, and poll-deploy.service was
        mapped under ``systemd/`` while the renderer writes it to the tree
        root — so the entry could never have matched a rendered file at all.
        """
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_CONFIG)
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tmp_path / "out"
        renderer.render()

        mapping = renderer.get_install_mapping()

        assert "systemd/poll-deploy.service" not in mapping
        assert "poll-deploy.service" not in mapping
        assert "systemd/restore-staging.service" not in mapping

    def test_only_this_hosts_webhook_unit_is_mapped(self, tmp_path):
        """One entry, not one per logical server (#325)."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_MATRIX[1][1])  # the asymmetric two-host config
        renderer = ScaffoldRenderer(FraisierConfig(cfg), server="prod.example.io")
        renderer.output_dir = tmp_path / "out"
        renderer.render()

        webhooks = [s for s in renderer.get_install_mapping() if "webhook" in s]

        assert webhooks == ["fraisier-proj-webhook-prod-example-io.service"]

    def test_mapping_is_scoped_to_this_hosts_fraises(self, tmp_path):
        """It used to walk every fraise, so a multi-host box diffed units it
        does not install and reported them missing."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_MATRIX[1][1])
        renderer = ScaffoldRenderer(FraisierConfig(cfg), server="prod.example.io")
        renderer.output_dir = tmp_path / "out"
        renderer.render()

        mapping = renderer.get_install_mapping()

        assert not [s for s in mapping if "development" in s or "api-dev" in s]


class TestDoctorSurfacesCoverage:
    """The assertion must reach an operator before it reaches a live host.

    ``install.sh`` runs on a live host mid-deploy, under the self-upgrade
    dynamic. If the first place an undispositioned artifact could surface were
    the installer, the first thing to see it would be a production webhook. So
    the identical check runs at render time and in ``doctor``; the deploy-time
    one is the backstop, not the discovery mechanism.
    """

    @staticmethod
    def _run(config):
        from fraisier import doctor

        return doctor.DOCTOR_CHECKS["scaffold_artifact_coverage"].fn(config)

    def test_reports_the_gaps_as_a_warning(self, tmp_path):
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_RESTORE_MIGRATE_CONFIG)

        result = self._run(FraisierConfig(cfg))

        assert result.status == "warn"
        assert "installed by nothing" in result.detail
        assert "restore-staging" in result.detail

    def test_a_tree_with_no_gaps_reports_ok(self, tmp_path):
        """The warning has to be able to clear, or it is wallpaper."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_CONFIG)

        result = self._run(FraisierConfig(cfg))

        assert result.status == "pass"

    def test_undispositioned_artifact_fails_the_check(self, tmp_path, monkeypatch):
        """A new rendered artifact with no disposition is a hard finding."""
        import fraisier.scaffold.artifacts as artifacts_mod

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_CONFIG)

        real_classify = artifacts_mod._classify
        monkeypatch.setattr(
            artifacts_mod,
            "_classify",
            lambda r, s: None if s == "sudoers" else real_classify(r, s),
        )

        result = self._run(FraisierConfig(cfg))

        assert result.status == "fail"
        assert "sudoers" in result.detail

    def test_the_failure_says_where_to_fix_it(self, tmp_path, monkeypatch):
        import fraisier.scaffold.artifacts as artifacts_mod

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_CONFIG)
        real_classify = artifacts_mod._classify
        monkeypatch.setattr(
            artifacts_mod,
            "_classify",
            lambda r, s: None if s == "sudoers" else real_classify(r, s),
        )

        result = self._run(FraisierConfig(cfg))

        assert "artifacts.py" in (result.fix_hint or "")

    def test_writes_nothing(self, tmp_path):
        """A dry-run render: doctor must not mutate the scaffold tree."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_CONFIG)
        before = sorted(p.name for p in tmp_path.iterdir())

        self._run(FraisierConfig(cfg))

        assert sorted(p.name for p in tmp_path.iterdir()) == before

    def test_no_config_skips(self):
        assert self._run(None).status == "skip"


class TestManifestFileOnDisk:
    """The JSON written beside the tree is a shipped artifact in its own right.

    Nothing in fraisier parses it back — ``install.sh`` has its contents baked
    in, and ``doctor``/``scaffold-diff`` build the manifest in-process — so it
    is the record a human reads and diffs between renders. That makes its shape
    worth pinning even though no code depends on it.
    """

    @staticmethod
    def _written(tmp_path):
        import json

        from fraisier.scaffold.artifacts import ARTIFACT_MANIFEST_NAME

        tmp_path.mkdir(parents=True, exist_ok=True)
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_CONFIG)
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tmp_path / "out"
        renderer.render()
        return json.loads((tmp_path / "out" / ARTIFACT_MANIFEST_NAME).read_text())

    def test_render_writes_it(self, tmp_path):
        payload = self._written(tmp_path)

        assert payload["schema_version"] == 1
        assert payload["batch_hash"]
        assert payload["artifacts"]

    def test_every_artifact_carries_its_routing(self, tmp_path):
        payload = self._written(tmp_path)

        for entry in payload["artifacts"]:
            assert set(entry) == {
                "source",
                "disposition",
                "destination",
                "mode",
                "environment",
                "sha256",
                "note",
            }

    def test_it_matches_the_in_memory_manifest(self, tmp_path):
        """The file and the object install.sh was generated from agree."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_CONFIG)
        renderer = ScaffoldRenderer(FraisierConfig(cfg))
        renderer.output_dir = tmp_path / "out"
        renderer.render()

        import json

        from fraisier.scaffold.artifacts import ARTIFACT_MANIFEST_NAME

        payload = json.loads((tmp_path / "out" / ARTIFACT_MANIFEST_NAME).read_text())

        assert payload["batch_hash"] == renderer.artifact_manifest.batch_hash
        assert [a["source"] for a in payload["artifacts"]] == [
            a.source for a in renderer.artifact_manifest.artifacts
        ]

    def test_it_is_not_itself_a_classified_artifact(self, tmp_path):
        """Metadata about the render, not a product of it — so it is neither
        installed nor subject to the coverage assertion."""
        payload = self._written(tmp_path)

        assert "artifact-manifest.json" not in {
            a["source"] for a in payload["artifacts"]
        }

    def test_it_is_stable_across_identical_renders(self, tmp_path):
        """A diff between two renders should show real changes only."""
        first = self._written(tmp_path / "a")
        second = self._written(tmp_path / "b")

        assert first == second
