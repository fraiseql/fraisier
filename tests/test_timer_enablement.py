"""`scaffold.systemd.timers` — which copied timers this host actually runs (#341).

`install.sh` has always copied `backup.timer` and `deploy-checker.timer` and
enabled neither, so they sit inert on every host that has ever run
`scaffold-install`. That was not a decision anyone recorded; it was the absence
of one, and it is why three broken units went years without being observed.

The fix is not "enable them" — a nightly `pg_dump | gzip` and a nightly staging
restore are not things a follow-up gets to start on hosts that never asked.
It is to make the inertness *declared*: a knob per timer family, defaulting off,
so the state is chosen rather than inherited, and so `install.sh` and `doctor`
can say what they did not do and how to change it.

#339's retention units use a better idiom — the config entry's *presence* is
the opt-in, so no boolean exists — but it does not transfer here: these three
pairs are rendered unconditionally, or by deployment strategy, so there is no
entry whose presence could carry the intent.
"""

from __future__ import annotations

import pytest
import yaml

from fraisier.config import FraisierConfig
from fraisier.errors import ValidationError

_BASE = {
    "name": "tp",
    "fraises": {
        "api": {
            "type": "api",
            "environments": {"production": {"app_path": "/var/www/api"}},
        }
    },
}


def _timers(tmp_path, timers=None) -> dict[str, bool]:
    """The parsed `timers` mapping for a config declaring *timers*.

    Returns the mapping rather than the config because parsing is lazy — the
    section is validated when `scaffold` is first read, so a test that only
    constructs a FraisierConfig asserts nothing at all.
    """
    raw = dict(_BASE)
    scaffold: dict = {"output_dir": str(tmp_path / "output")}
    if timers is not None:
        scaffold["systemd"] = {"timers": timers}
    raw["scaffold"] = scaffold
    path = tmp_path / "fraises.yaml"
    path.write_text(yaml.safe_dump(raw))
    return FraisierConfig(path).scaffold.systemd.timers


class TestDefaults:
    def test_absent_timers_key_means_every_timer_is_off(self, tmp_path):
        """Upgrading must not start anything.

        The whole point of the knob is that the default reproduces today's
        behaviour exactly — no host gains a nightly job by installing a
        release that fixes the units those jobs would have run.
        """
        timers = _timers(tmp_path)

        assert timers == {
            "backup": False,
            "deploy_checker": False,
            "restore_staging": False,
        }

    def test_a_partial_mapping_leaves_the_others_off(self, tmp_path):
        """Naming one timer must not imply anything about the rest."""
        timers = _timers(tmp_path, {"backup": True})

        assert timers == {
            "backup": True,
            "deploy_checker": False,
            "restore_staging": False,
        }


class TestRejection:
    def test_an_unknown_timer_name_is_a_config_error(self, tmp_path):
        """Silently ignoring it is how an operator believes they enabled a
        nightly backup that never runs — this area's signature failure."""
        with pytest.raises(ValidationError) as exc:
            _timers(tmp_path, {"backupp": True})

        message = str(exc.value)
        assert "backupp" in message
        assert "backup" in message and "deploy_checker" in message

    def test_a_non_boolean_value_is_a_config_error(self, tmp_path):
        """YAML reads bare `yes` as True but quoted `"yes"` as a string.

        Coercing a truthy string would enable a nightly `pg_dump` for someone
        who wrote something slightly wrong, which is the wrong direction to
        fail in.
        """
        with pytest.raises(ValidationError) as exc:
            _timers(tmp_path, {"backup": "yes"})

        assert "backup" in str(exc.value)

    def test_a_non_mapping_timers_key_is_a_config_error(self, tmp_path):
        """`timers: [backup]` is the shape an operator reaches for first."""
        with pytest.raises(ValidationError) as exc:
            _timers(tmp_path, ["backup"])

        assert "timers" in str(exc.value)


# --------------------------------------------------------------------------
# The knob decides the disposition, and install.sh does the rest
# --------------------------------------------------------------------------

_RENDER_BASE = {
    "name": "tp",
    "fraises": {
        "api": {
            "type": "api",
            "environments": {
                "production": {
                    "app_path": "/var/www/api",
                    "git_repo": "/srv/git/api.git",
                },
                # restore-staging renders only under this strategy.
                "staging": {
                    "app_path": "/var/www/api-staging",
                    "git_repo": "/srv/git/api-staging.git",
                    "database": {"strategy": "restore_migrate"},
                },
            },
        }
    },
}

# Every family, and the two units each one switches together. A timer whose
# .service is classified differently is a firing into a unit that may not be
# installed, which is how backup.timer and backup.service drifted apart.
_UNITS = {
    "backup": ("systemd/backup.timer", "systemd/backup.service"),
    "deploy_checker": (
        "systemd/deploy-checker.timer",
        "systemd/deploy-checker.service",
    ),
    "restore_staging": (
        "systemd/restore-staging.timer",
        "systemd/restore-staging.service",
    ),
}


def _render(tmp_path, timers=None):
    """Render a full tree; return (output dir, manifest artifacts by source)."""
    import json

    from fraisier.scaffold.renderer import ScaffoldRenderer

    raw = dict(_RENDER_BASE)
    scaffold: dict = {"output_dir": str(tmp_path / "output"), "deploy_user": "deployer"}
    if timers is not None:
        scaffold["systemd"] = {"timers": timers}
    raw["scaffold"] = scaffold
    path = tmp_path / "fraises.yaml"
    path.write_text(yaml.safe_dump(raw))
    ScaffoldRenderer(FraisierConfig(path)).render()

    out = tmp_path / "output"
    manifest = json.loads((out / "artifact-manifest.json").read_text())
    return out, {a["source"]: a for a in manifest["artifacts"]}


class TestKnobSelectsDisposition:
    @pytest.mark.parametrize("family", sorted(_UNITS))
    def test_off_is_copied_and_inert(self, tmp_path, family):
        """`plain` is copied by install.sh and enabled by nothing — today's
        behaviour, now chosen rather than inherited."""
        _out, by_source = _render(tmp_path, {family: False})

        for source in _UNITS[family]:
            assert by_source[source]["disposition"] == "plain"
            assert (
                by_source[source]["destination"]
                == f"/etc/systemd/system/{source.removeprefix('systemd/')}"
            )

    @pytest.mark.parametrize("family", sorted(_UNITS))
    def test_on_is_installed_and_enabled(self, tmp_path, family):
        """`timer` is `plain` plus `systemctl enable --now` after the reload."""
        out, by_source = _render(tmp_path, {family: True})

        for source in _UNITS[family]:
            assert by_source[source]["disposition"] == "timer"

        timer_unit = _UNITS[family][0].removeprefix("systemd/")
        assert (
            f"systemctl enable --now {timer_unit}" in (out / "install.sh").read_text()
        )

    @pytest.mark.parametrize("family", sorted(_UNITS))
    def test_one_knob_does_not_move_the_others(self, tmp_path, family):
        """Enabling a nightly backup must not start a nightly staging wipe."""
        _out, by_source = _render(tmp_path, {family: True})

        for other, sources in _UNITS.items():
            if other == family:
                continue
            for source in sources:
                assert by_source[source]["disposition"] == "plain"

    def test_a_timer_is_never_enabled_without_its_service(self, tmp_path):
        """Enabling a timer whose .service is not installed is a firing into
        a missing unit — backup.timer's own drift, and #339's ordering bug."""
        _out, by_source = _render(tmp_path, dict.fromkeys(_UNITS, True))

        enabled = {
            source
            for source, a in by_source.items()
            if a["disposition"] == "timer" and source.endswith(".timer")
        }
        for timer in enabled:
            service = timer.removesuffix(".timer") + ".service"
            assert by_source[service]["disposition"] == "timer", (
                f"{timer} is enabled but {service} is not classified with it"
            )


class TestRestoreStagingIsInstalled:
    def test_both_units_are_installed(self, tmp_path):
        """Scope item 4: it stops being an UNINSTALLED_GAP.

        The pair renders only when a fraise declares `restore_migrate`, so
        installation is already gated by intent; firing nightly is not, and
        stays behind the knob.
        """
        _out, by_source = _render(tmp_path)

        for source in _UNITS["restore_staging"]:
            assert by_source[source]["destination"] is not None
            assert by_source[source]["disposition"] != "uninstalled_gap"

    def test_the_default_tree_declares_no_gaps(self, tmp_path):
        """restore-staging was the last one."""
        _out, by_source = _render(tmp_path)

        gaps = [
            s for s, a in by_source.items() if a["disposition"] == "uninstalled_gap"
        ]
        assert not gaps


class TestOneAuthority:
    def test_every_config_family_has_units_and_the_reverse(self):
        """A family the config offers but nothing classifies is a switch wired
        to nothing — an operator sets it, `scaffold` accepts it, and no timer
        ever starts. That is this area's signature failure, so the two tables
        are asserted equal rather than kept equal by hand."""
        from fraisier.config.schema import TIMER_FAMILIES
        from fraisier.scaffold.artifacts import _TIMER_FAMILY_UNITS

        assert set(_TIMER_FAMILY_UNITS) == set(TIMER_FAMILIES)

    def test_each_family_owns_exactly_one_timer_and_one_service(self):
        from fraisier.scaffold.artifacts import _TIMER_FAMILY_UNITS

        for family, sources in _TIMER_FAMILY_UNITS.items():
            suffixes = sorted(s.rsplit(".", 1)[1] for s in sources)
            assert suffixes == ["service", "timer"], f"{family}: {sources}"
