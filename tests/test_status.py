"""Tests for deployment status file (state machine, atomic writes)."""

import json
import threading

from fraisier.status import DeploymentStatusFile, read_status, write_status


class TestDeploymentStatusFile:
    """Test the DeploymentStatusFile dataclass."""

    def test_default_state_is_idle(self):
        """New status defaults to idle state."""
        status = DeploymentStatusFile(fraise_name="myfraise", environment="production")
        assert status.state == "idle"

    def test_all_optional_fields_default_to_none(self):
        """Optional fields default to None."""
        status = DeploymentStatusFile(fraise_name="myfraise", environment="production")
        assert status.version is None
        assert status.commit_sha is None
        assert status.started_at is None
        assert status.finished_at is None
        assert status.error_message is None
        assert status.migration_report is None
        assert status.last_error is None


class TestWriteReadRoundtrip:
    """Test write + read roundtrip for DeploymentStatusFile."""

    def test_roundtrip_idle(self, tmp_path):
        """Write and read back an idle status."""
        status = DeploymentStatusFile(fraise_name="myfraise", environment="production")
        write_status(status, status_dir=tmp_path)
        loaded = read_status("myfraise", status_dir=tmp_path)

        assert loaded is not None
        assert loaded.state == "idle"
        assert loaded.fraise_name == "myfraise"
        assert loaded.environment == "production"

    def test_roundtrip_with_all_fields(self, tmp_path):
        """Write and read back a status with all fields populated."""
        status = DeploymentStatusFile(
            state="success",
            fraise_name="myfraise",
            environment="production",
            version="1.2.3",
            commit_sha="abc1234",
            started_at="2026-03-22T10:00:00+00:00",
            finished_at="2026-03-22T10:01:30+00:00",
            error_message=None,
            migration_report={"applied": ["001_init"]},
            last_error=None,
        )
        write_status(status, status_dir=tmp_path)
        loaded = read_status("myfraise", status_dir=tmp_path)

        assert loaded is not None
        assert loaded.state == "success"
        assert loaded.version == "1.2.3"
        assert loaded.commit_sha == "abc1234"
        assert loaded.started_at == "2026-03-22T10:00:00+00:00"
        assert loaded.finished_at == "2026-03-22T10:01:30+00:00"
        assert loaded.migration_report == {"applied": ["001_init"]}

    def test_read_nonexistent_returns_none(self, tmp_path):
        """Reading a status for a fraise with no file returns None."""
        result = read_status("nonexistent", status_dir=tmp_path)
        assert result is None


class TestAtomicWrite:
    """Test that writes are atomic (temp file + rename)."""

    def test_no_partial_json_on_disk(self, tmp_path):
        """Status file is never a partial JSON (atomic rename)."""
        status = DeploymentStatusFile(fraise_name="myfraise", environment="production")
        write_status(status, status_dir=tmp_path)

        # The final file must exist and be valid JSON
        status_file = tmp_path / "myfraise.status.json"
        assert status_file.exists()
        data = json.loads(status_file.read_text())
        assert data["fraise_name"] == "myfraise"

    def test_no_temp_file_left_behind(self, tmp_path):
        """No .tmp file remains after a successful write."""
        status = DeploymentStatusFile(fraise_name="myfraise", environment="production")
        write_status(status, status_dir=tmp_path)

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_concurrent_reads_see_valid_json(self, tmp_path):
        """Concurrent reads during writes always see valid JSON."""
        errors = []

        def writer():
            for i in range(50):
                status = DeploymentStatusFile(
                    state="deploying" if i % 2 == 0 else "success",
                    fraise_name="myfraise",
                    environment="production",
                    version=f"1.0.{i}",
                )
                write_status(status, status_dir=tmp_path)

        def reader():
            for _ in range(50):
                try:
                    result = read_status("myfraise", status_dir=tmp_path)
                    if result is not None:
                        # Must be a valid, complete status
                        assert result.fraise_name == "myfraise"
                except (json.JSONDecodeError, AssertionError) as e:
                    errors.append(e)

        # Seed an initial file so reader has something
        write_status(
            DeploymentStatusFile(fraise_name="myfraise", environment="production"),
            status_dir=tmp_path,
        )

        t_write = threading.Thread(target=writer)
        t_read = threading.Thread(target=reader)
        t_write.start()
        t_read.start()
        t_write.join()
        t_read.join()

        assert errors == [], f"Concurrent read saw invalid data: {errors}"


class TestStateTransitions:
    """Test valid state transitions for the deployment lifecycle."""

    def test_idle_to_deploying_to_success(self, tmp_path):
        """Happy path: idle -> deploying -> success."""
        # Start idle
        status = DeploymentStatusFile(fraise_name="myfraise", environment="production")
        write_status(status, status_dir=tmp_path)

        # Transition to deploying
        status.state = "deploying"
        status.started_at = "2026-03-22T10:00:00+00:00"
        status.commit_sha = "abc1234"
        write_status(status, status_dir=tmp_path)

        loaded = read_status("myfraise", status_dir=tmp_path)
        assert loaded is not None
        assert loaded.state == "deploying"
        assert loaded.started_at == "2026-03-22T10:00:00+00:00"

        # Transition to success
        status.state = "success"
        status.finished_at = "2026-03-22T10:01:30+00:00"
        status.version = "1.2.3"
        write_status(status, status_dir=tmp_path)

        loaded = read_status("myfraise", status_dir=tmp_path)
        assert loaded is not None
        assert loaded.state == "success"
        assert loaded.finished_at == "2026-03-22T10:01:30+00:00"
        assert loaded.version == "1.2.3"

    def test_idle_to_deploying_to_failed(self, tmp_path):
        """Failure path: idle -> deploying -> failed."""
        status = DeploymentStatusFile(fraise_name="myfraise", environment="production")
        write_status(status, status_dir=tmp_path)

        # Transition to deploying
        status.state = "deploying"
        status.started_at = "2026-03-22T10:00:00+00:00"
        write_status(status, status_dir=tmp_path)

        # Transition to failed
        status.state = "failed"
        status.finished_at = "2026-03-22T10:02:00+00:00"
        status.error_message = "Health check timed out"
        status.migration_report = {"applied": ["001_init"], "failed": "002_add_users"}
        status.last_error = {
            "message": "Health check timed out",
            "timestamp": "2026-03-22T10:02:00+00:00",
        }
        write_status(status, status_dir=tmp_path)

        loaded = read_status("myfraise", status_dir=tmp_path)
        assert loaded is not None
        assert loaded.state == "failed"
        assert loaded.error_message == "Health check timed out"
        assert loaded.migration_report == {
            "applied": ["001_init"],
            "failed": "002_add_users",
        }
        assert loaded.last_error == {
            "message": "Health check timed out",
            "timestamp": "2026-03-22T10:02:00+00:00",
        }


class TestFailedStatusDetails:
    """Test that failed status includes all required error details."""

    def test_failed_includes_error_message(self, tmp_path):
        """Failed status preserves error_message through roundtrip."""
        status = DeploymentStatusFile(
            state="failed",
            fraise_name="myfraise",
            environment="production",
            error_message="Connection refused",
        )
        write_status(status, status_dir=tmp_path)
        loaded = read_status("myfraise", status_dir=tmp_path)

        assert loaded is not None
        assert loaded.error_message == "Connection refused"

    def test_failed_includes_migration_report(self, tmp_path):
        """Failed status preserves migration_report through roundtrip."""
        report = {
            "applied": ["001_init", "002_users"],
            "failed": "003_permissions",
            "error": "column already exists",
        }
        status = DeploymentStatusFile(
            state="failed",
            fraise_name="myfraise",
            environment="production",
            migration_report=report,
        )
        write_status(status, status_dir=tmp_path)
        loaded = read_status("myfraise", status_dir=tmp_path)

        assert loaded is not None
        assert loaded.migration_report == report

    def test_failed_includes_last_error_with_timestamp(self, tmp_path):
        """Failed status preserves last_error dict with timestamp."""
        last_error = {
            "message": "SIGTERM received",
            "timestamp": "2026-03-22T10:05:00+00:00",
            "code": "DEPLOYMENT_TIMEOUT",
        }
        status = DeploymentStatusFile(
            state="failed",
            fraise_name="myfraise",
            environment="production",
            last_error=last_error,
        )
        write_status(status, status_dir=tmp_path)
        loaded = read_status("myfraise", status_dir=tmp_path)

        assert loaded is not None
        assert loaded.last_error == last_error
        assert loaded.last_error["timestamp"] == "2026-03-22T10:05:00+00:00"
