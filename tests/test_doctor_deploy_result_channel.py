"""Can each installed deploy service actually answer a `--wait` client? (#356)

v0.64.0 gave `deploy_daemon` a result channel and made `--wait` exit 1 when no
result arrives. Together those turn a host whose deploy unit still runs an older
fraisier into a host where every `--wait` deploy fails — and that is the *normal*
upgrade order, not an edge case: the CLI replaces itself via self-upgrade while
the deploy unit's binary only changes when someone re-runs a scaffold install.

The unit file itself is unchanged by the fix (the result goes to fd 0, which
`StandardInput=socket` already provided), so the discriminator is the version of
the fraisier binary the installed unit names in `ExecStart=`. This check reads
the installed unit and asks that binary.
"""

from __future__ import annotations

import stat

import pytest

from fraisier.doctor import DOCTOR_CHECKS

DEPLOY_UNIT = "fraisier-api-staging@.service"


@pytest.fixture
def unit_dir(tmp_path, monkeypatch):
    d = tmp_path / "systemd"
    d.mkdir()
    monkeypatch.setattr("fraisier.doctor.SYSTEMD_UNIT_DIR", d)
    return d


@pytest.fixture
def bindir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    return d


def _fraisier_reporting(bindir, version, *, name="fraisier"):
    """A stand-in binary that answers `--version` the way fraisier does."""
    p = bindir / name
    p.write_text(f"#!/bin/sh\necho 'fraisier, version {version}'\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def _fraisier_refusing(bindir, *, name="fraisier"):
    """A binary that is there but will not say what it is."""
    p = bindir / name
    p.write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def _deploy_unit(unit_dir, binary, *, name=DEPLOY_UNIT, project="api"):
    (unit_dir / name).write_text(
        "[Unit]\nDescription=Fraisier Deploy Service\n\n"
        "[Service]\nType=simple\n"
        f"ExecStart={binary} deploy-daemon --project={project}\n"
        "StandardInput=socket\nStandardOutput=journal\n"
    )


def _run(config=None):
    return DOCTOR_CHECKS["deploy_result_channel"].fn(config)


class TestAnOldDeployUnitIsFound:
    def test_a_pre_v0_64_binary_cannot_answer(self, unit_dir, bindir):
        _deploy_unit(unit_dir, _fraisier_reporting(bindir, "0.63.0"))

        result = _run()

        assert result.status == "fail"
        assert DEPLOY_UNIT in result.detail
        assert "0.63.0" in result.detail
        assert result.fix_hint is not None
        assert "scaffold-install" in result.fix_hint

    def test_a_v0_64_binary_can_answer(self, unit_dir, bindir):
        _deploy_unit(unit_dir, _fraisier_reporting(bindir, "0.64.0"))

        result = _run()

        assert result.status == "pass"

    def test_a_later_binary_can_answer(self, unit_dir, bindir):
        _deploy_unit(unit_dir, _fraisier_reporting(bindir, "1.2.3"))

        result = _run()

        assert result.status == "pass"


class TestItReportsPerFraiseAndEnvironment:
    """v0.59.0's scoping: the unit name is the (fraise, environment) identity."""

    def test_only_the_stale_unit_is_named(self, unit_dir, bindir):
        old = _fraisier_reporting(bindir, "0.61.0", name="fraisier-old")
        new = _fraisier_reporting(bindir, "0.64.0", name="fraisier-new")
        _deploy_unit(unit_dir, old, name="fraisier-api-staging@.service")
        _deploy_unit(unit_dir, new, name="fraisier-api-production@.service")

        result = _run()

        assert result.status == "fail"
        assert "fraisier-api-staging@.service" in result.detail
        assert "fraisier-api-production@.service" not in result.detail


class TestUnverifiableIsNotBad:
    """`ArchiveCheck`'s three-valued precedent: cannot-read is not broken."""

    def test_a_binary_that_will_not_report_its_version_is_a_warning(
        self, unit_dir, bindir
    ):
        _deploy_unit(unit_dir, _fraisier_refusing(bindir))

        result = _run()

        assert result.status == "warn"
        assert result.status != "fail"

    def test_a_missing_binary_is_a_warning(self, unit_dir, bindir):
        _deploy_unit(unit_dir, bindir / "not-installed")

        result = _run()

        assert result.status == "warn"

    def test_an_unreadable_unit_directory_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("fraisier.doctor.SYSTEMD_UNIT_DIR", tmp_path / "nope")

        result = _run()

        assert result.status == "skip"


class TestAScanThatMatchedNothingIsNotAPass:
    """A tree-scanner that silently matches nothing looks exactly like a pass.

    That mistake has cost this project four times, so it is pinned rather than
    trusted: the check must be *able* to fail, and must say so when it had
    nothing to look at.
    """

    def test_no_deploy_units_installed_is_skip_not_pass(self, unit_dir, bindir):
        binary = _fraisier_reporting(bindir, "0.63.0")
        # A webhook unit names a fraisier binary but is not a deploy service:
        # if it were counted, this stale binary would produce a false failure.
        (unit_dir / "fraisier-proj-webhook.service").write_text(
            f"[Service]\nExecStart={binary} webhook-server\n"
        )

        result = _run()

        assert result.status == "skip"
        assert result.status not in {"pass", "fail"}

    def test_an_empty_unit_directory_is_skip_not_pass(self, unit_dir):
        result = _run()

        assert result.status == "skip"

    def test_the_check_can_fail_on_the_same_tree_that_passes(self, unit_dir, bindir):
        """Same scanner, same tree shape, opposite verdicts — so it is looking."""
        _deploy_unit(unit_dir, _fraisier_reporting(bindir, "0.64.0", name="new"))
        assert _run().status == "pass"

        _deploy_unit(unit_dir, _fraisier_reporting(bindir, "0.63.0", name="old"))
        assert _run().status == "fail"
