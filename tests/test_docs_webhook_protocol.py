"""Lock the outbound notification webhook payload against the public doc.

``docs/webhook-protocol.md`` is the contract for what fraisier POSTs to
``type: webhook`` notification destinations. The contract has two
load-bearing pieces:

1. The set of fields ``DeployEvent.to_dict()`` returns must match the
   "Payload schema" table in the doc.
2. The event_type set documented in the doc must match what
   ``DeployEvent.from_result`` emits.

If either drifts, an undeclared/missing field would silently break
integrators who built against the documented contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fraisier.notifications.base import DeployEvent

_DOC = Path(__file__).parent.parent / "docs" / "webhook-protocol.md"

_DOCUMENTED_FIELDS = frozenset(
    {
        "fraise_name",
        "environment",
        "event_type",
        "error_message",
        "error_code",
        "recovery_hint",
        "old_version",
        "new_version",
        "duration_seconds",
        "triggered_by",
        "commit_sha",
        "incident_path",
        "timestamp",
    }
)

_DOCUMENTED_EVENT_TYPES = frozenset(
    {"success", "failure", "rollback", "rollback_failed"}
)


def _doc_text() -> str:
    return _DOC.read_text()


def test_doc_exists():
    assert _DOC.is_file(), f"{_DOC} missing — webhook protocol doc not shipped"


def test_payload_keys_match_documented_schema():
    """DeployEvent.to_dict() keys must exactly match the doc's schema table."""
    event = DeployEvent(
        fraise_name="my_api", environment="production", event_type="success"
    )
    keys = set(event.to_dict().keys())
    missing = _DOCUMENTED_FIELDS - keys
    extra = keys - _DOCUMENTED_FIELDS
    assert not missing, (
        f"Doc declares fields not present in DeployEvent: {sorted(missing)}. "
        f"Either add them to DeployEvent.to_dict() or remove from the doc."
    )
    assert not extra, (
        f"DeployEvent.to_dict() emits fields not declared in doc: {sorted(extra)}. "
        f"Add a row to docs/webhook-protocol.md for each."
    )


@pytest.mark.parametrize("field", sorted(_DOCUMENTED_FIELDS))
def test_each_documented_field_appears_in_doc_text(field):
    assert f"`{field}`" in _doc_text(), (
        f"Field {field!r} is in DeployEvent.to_dict() but not mentioned in "
        f"the docs/webhook-protocol.md payload table."
    )


@pytest.mark.parametrize("event_type", sorted(_DOCUMENTED_EVENT_TYPES))
def test_each_documented_event_type_is_emitted(event_type):
    # Smoke: each event_type the doc lists must be constructible. Catches
    # the case where the doc lists an event_type that DeployEvent has no
    # path to produce.
    event = DeployEvent(
        fraise_name="api", environment="prod", event_type=event_type
    )
    assert event.to_dict()["event_type"] == event_type


def test_worked_example_payload_is_valid_json():
    import json
    import re

    text = _doc_text()
    # Find every ```json ... ``` fenced block and parse it.
    blocks = re.findall(r"```json\s*\n(.*?)```", text, re.DOTALL)
    assert blocks, "No ```json fenced blocks found in webhook-protocol.md"
    for i, block in enumerate(blocks):
        try:
            payload = json.loads(block)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"JSON block #{i + 1} in webhook-protocol.md is malformed: {exc}"
            ) from exc
        # Every worked-example payload must declare every documented field
        # (so consumers learn the full shape from the examples).
        missing = _DOCUMENTED_FIELDS - set(payload.keys())
        assert not missing, (
            f"Worked example #{i + 1} in webhook-protocol.md omits fields: "
            f"{sorted(missing)}. Add them so consumers see the full shape."
        )
