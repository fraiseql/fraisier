"""The deploy side of the root helper's answer (#433).

The helper no longer writes the deferred-restart ledger: it runs as root, and
the ledger lives at a path the deploy's config chooses. The deploy records the
restarts it was told were deferred, as itself. And a refused or pending apply
stops the deploy *and* forgets the config hash, so the next deploy asks again
instead of reading "config unchanged" as "nothing to install".
"""

from __future__ import annotations

import json
import socket
import threading
import types
from unittest.mock import patch

import pytest

from fraisier.config_watcher import ConfigWatcher
from fraisier.deferred_restart import (
    DEFERRED_RESTART_FILE,
    read_deferred_restarts,
    record_deferred_restarts,
)
from fraisier.deployers.api import APIDeployer
from fraisier.errors import DeploymentError


def test_recording_merges_with_an_earlier_debt(tmp_path):
    (tmp_path / DEFERRED_RESTART_FILE).write_text("a.service\nb.service\n")
    record_deferred_restarts(tmp_path, ["c.service", "b.service"])
    assert read_deferred_restarts(tmp_path) == ["a.service", "b.service", "c.service"]


def test_recording_nothing_writes_nothing(tmp_path):
    record_deferred_restarts(tmp_path, [])
    assert not (tmp_path / DEFERRED_RESTART_FILE).exists()


def _serve_once(path, response: dict) -> threading.Thread:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def handle():
        conn, _ = server.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(json.dumps(response).encode() + b"\n")
        server.close()

    thread = threading.Thread(target=handle, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def deployer(tmp_path):
    config_dir = tmp_path / "opt"
    config_dir.mkdir()
    config_path = config_dir / "fraises.yaml"
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    config_path.write_text(
        f"name: demo\ndeployment:\n  lock_dir: {lock_dir}\nfraises: {{}}\n"
    )
    deployer = APIDeployer({})
    return types.SimpleNamespace(
        deployer=deployer, config_path=config_path, lock_dir=lock_dir, tmp=tmp_path
    )


def _install(env, response: dict) -> None:
    sock = env.tmp / "helper.sock"
    thread = _serve_once(sock, response)
    with patch(
        "fraisier.deployers.base._get_scaffold_socket_path", return_value=str(sock)
    ):
        try:
            env.deployer._install_scaffold(config_path=env.config_path)
        finally:
            thread.join(timeout=5)


def test_deferred_restarts_land_in_the_ledger(deployer):
    _install(
        deployer,
        {
            "ok": True,
            "stdout": "installed 1 unit(s)",
            "stderr": "",
            "deferred_restarts": ["fraisier-demo-webhook.service"],
        },
    )
    assert read_deferred_restarts(deployer.lock_dir) == [
        "fraisier-demo-webhook.service"
    ]


def test_a_pending_apply_stops_the_deploy_and_names_it(deployer):
    ConfigWatcher(deployer.config_path.parent).save_hash()
    with pytest.raises(DeploymentError) as raised:
        _install(
            deployer,
            {
                "ok": False,
                "stdout": "installed 0 unit(s), 3 unchanged",
                "stderr": "pending operator install: /etc/sudoers.d/demo: differs",
                "pending": ["/etc/sudoers.d/demo: differs"],
                "refused": {"x.service": ["[user] User='root'"]},
            },
        )
    assert "pending operator install: /etc/sudoers.d/demo" in str(raised.value)
    assert raised.value.context["pending"] == ["/etc/sudoers.d/demo: differs"]
    assert raised.value.context["refused"] == {"x.service": ["[user] User='root'"]}


def test_a_stopped_apply_forgets_the_config_hash(deployer):
    watcher = ConfigWatcher(deployer.config_path.parent)
    watcher.save_hash()
    with pytest.raises(DeploymentError):
        _install(deployer, {"ok": False, "stdout": "", "stderr": "pending"})
    assert watcher.get_previous_hash() is None


def test_a_completed_apply_keeps_the_config_hash(deployer):
    watcher = ConfigWatcher(deployer.config_path.parent)
    watcher.save_hash()
    _install(deployer, {"ok": True, "stdout": "", "stderr": ""})
    assert watcher.get_previous_hash() is not None
