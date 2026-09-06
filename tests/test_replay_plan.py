"""What gets replayed, in what order, at which ref (#367).

Three of #367's four decisions live here, and all three can make things worse
than the manual re-fire they replace:

- **Which ref.** Without the bug, the refused push would have deployed and any
  later push would have deployed after it — the end state is *branch head
  deployed*. Replaying the recorded sha is a regression whenever newer commits
  exist, and is never more correct.
- **What order.** One host serving staging and production has two entries and
  no defined order. Production last, otherwise alphabetical: if the replay
  mechanism is itself broken, it breaks on a lower-stakes target first.
- **Whether at all.** A target that no longer exists in the configuration is
  dropped rather than guessed at.

The fourth — when to fire — is the handoff marker, tested separately.
"""

from __future__ import annotations

from fraisier.refused_dispatch_record import RefusedDispatch
from fraisier.replay_plan import ReplayTarget, plan_replays


def _entry(fraise: str, environment: str, branch: str = "main") -> RefusedDispatch:
    return RefusedDispatch(
        fraise=fraise,
        environment=environment,
        branch=branch,
        commit_sha="abc1234",
        webhook_id=1,
        refused_at="2026-09-06T00:00:00+00:00",
    )


def _config(targets: dict[tuple[str, str], dict] | None = None):
    """A stand-in for FraisierConfig that answers get_fraise_environment."""

    class _Config:
        def __init__(self, known):
            self._known = known

        def get_fraise_environment(self, fraise, environment):
            return self._known.get((fraise, environment))

    if targets is None:
        targets = {
            ("api", "staging"): {"app_path": "/srv/api"},
            ("api", "production"): {"app_path": "/srv/api"},
            ("worker", "staging"): {"app_path": "/srv/worker"},
        }
    return _Config(targets)


class TestOrdering:
    def test_production_goes_last(self):
        plan = plan_replays(
            [_entry("api", "production"), _entry("api", "staging")], _config()
        )

        assert [t.environment for t in plan] == ["staging", "production"]

    def test_the_rest_is_alphabetical_by_env_then_fraise(self):
        plan = plan_replays(
            [
                _entry("worker", "staging"),
                _entry("api", "production"),
                _entry("api", "staging"),
            ],
            _config(),
        )

        assert [(t.environment, t.fraise) for t in plan] == [
            ("staging", "api"),
            ("staging", "worker"),
            ("production", "api"),
        ]

    def test_the_order_does_not_depend_on_ledger_order(self):
        entries = [
            _entry("api", "production"),
            _entry("worker", "staging"),
            _entry("api", "staging"),
        ]
        forward = plan_replays(entries, _config())
        backward = plan_replays(list(reversed(entries)), _config())

        assert [t.target for t in forward] == [t.target for t in backward]


class TestWhichRef:
    def test_the_branch_is_carried_not_the_recorded_sha(self):
        """The deploy resolves the branch head. Pinning the recorded sha would
        redeploy old code over anything pushed since the refusal."""
        plan = plan_replays([_entry("api", "staging", branch="release")], _config())

        assert plan[0].branch == "release"
        assert not hasattr(plan[0], "commit_sha")

    def test_an_entry_with_no_branch_is_dropped(self):
        """Nothing to resolve a head from; the entry stands for `doctor`."""
        plan = plan_replays([_entry("api", "staging", branch="")], _config())

        assert plan == []


class TestWhatIsDropped:
    def test_a_target_gone_from_config_is_dropped(self):
        plan = plan_replays([_entry("removed", "staging")], _config())

        assert plan == []

    def test_the_surviving_targets_are_still_planned(self):
        plan = plan_replays(
            [_entry("removed", "staging"), _entry("api", "staging")], _config()
        )

        assert [t.fraise for t in plan] == ["api"]

    def test_an_empty_ledger_plans_nothing(self):
        assert plan_replays([], _config()) == []

    def test_the_config_it_carries_is_the_env_config(self):
        plan = plan_replays([_entry("api", "staging")], _config())

        assert plan[0].fraise_config == {"app_path": "/srv/api"}


class TestReplayTarget:
    def test_target_is_the_ledger_key(self):
        target = ReplayTarget(
            fraise="api", environment="staging", branch="main", fraise_config={}
        )

        assert target.target == ("api", "staging")
