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
