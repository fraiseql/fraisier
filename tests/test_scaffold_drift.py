"""Tests for scaffold drift detection module."""

from pathlib import Path

import pytest

from fraisier.scaffold.drift import DriftResult, _hash_file, detect_drift


class TestDriftResult:
    """Test DriftResult dataclass."""

    def test_drift_result_basic(self):
        """DriftResult can be created with required fields."""
        result = DriftResult(name="file.txt", drifted=False)
        assert result.name == "file.txt"
        assert result.drifted is False
        assert result.message == ""

    def test_drift_result_with_message(self):
        """DriftResult can include a message."""
        result = DriftResult(
            name="file.txt",
            drifted=True,
            message="File was modified",
        )
        assert result.name == "file.txt"
        assert result.drifted is True
        assert result.message == "File was modified"


class TestHashFile:
    """Test _hash_file helper function."""

    def test_hash_file_consistent(self, tmp_path):
        """Same file content produces same hash."""
        file1 = tmp_path / "file1.txt"
        file1.write_text("test content")

        hash1 = _hash_file(file1)
        hash2 = _hash_file(file1)

        assert hash1 == hash2
        assert hash1.startswith("sha256:")

    def test_hash_file_different_for_different_content(self, tmp_path):
        """Different content produces different hashes."""
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file1.write_text("content1")
        file2.write_text("content2")

        hash1 = _hash_file(file1)
        hash2 = _hash_file(file2)

        assert hash1 != hash2

    def test_hash_file_binary(self, tmp_path):
        """Binary files can be hashed."""
        file1 = tmp_path / "binary.bin"
        file1.write_bytes(b"\x00\x01\x02\x03")

        hash1 = _hash_file(file1)
        assert hash1.startswith("sha256:")


class TestDetectDrift:
    """Test drift detection function."""

    def test_detect_drift_no_drift(self, tmp_path):
        """No drift when files match expected hashes."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        file1 = output_dir / "file1.txt"
        file1.write_text("original content")

        hash1 = _hash_file(file1)
        template_hashes = {"file1.txt": hash1}

        results = detect_drift(output_dir, template_hashes)

        assert len(results) == 0

    def test_detect_drift_modified(self, tmp_path):
        """Drift detected when file content changes."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        file1 = output_dir / "file1.txt"
        file1.write_text("original content")

        # Record hash of original
        original_hash = _hash_file(file1)

        # Modify file
        file1.write_text("modified content")

        template_hashes = {"file1.txt": original_hash}

        results = detect_drift(output_dir, template_hashes)

        assert len(results) == 1
        assert results[0].name == "file1.txt"
        assert results[0].drifted is True
        assert "differs from template" in results[0].message

    def test_detect_drift_missing_file(self, tmp_path):
        """Drift detected when file is missing."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        template_hashes = {"file1.txt": "sha256:something"}

        results = detect_drift(output_dir, template_hashes)

        assert len(results) == 1
        assert results[0].name == "file1.txt"
        assert results[0].drifted is True
        assert "not found" in results[0].message

    def test_detect_drift_ignore(self, tmp_path):
        """Ignored files are not checked for drift."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        file1 = output_dir / "file1.txt"
        file1.write_text("original")

        original_hash = _hash_file(file1)

        # Modify file
        file1.write_text("modified")

        template_hashes = {"file1.txt": original_hash}
        ignore = {"file1.txt"}

        results = detect_drift(output_dir, template_hashes, ignore)

        assert len(results) == 0

    def test_detect_drift_multiple_files(self, tmp_path):
        """Can check multiple files for drift."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        file1 = output_dir / "file1.txt"
        file1.write_text("content1")
        hash1 = _hash_file(file1)

        file2 = output_dir / "file2.txt"
        file2.write_text("content2")
        hash2 = _hash_file(file2)

        file3 = output_dir / "file3.txt"
        file3.write_text("content3")
        hash3 = _hash_file(file3)

        # Modify file2
        file2.write_text("modified2")

        template_hashes = {
            "file1.txt": hash1,
            "file2.txt": hash2,
            "file3.txt": hash3,
        }

        results = detect_drift(output_dir, template_hashes)

        assert len(results) == 1
        assert results[0].name == "file2.txt"
        assert results[0].drifted is True

    def test_detect_drift_ignore_multiple(self, tmp_path):
        """Can ignore multiple files."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        file1 = output_dir / "file1.txt"
        file1.write_text("content1")
        hash1 = _hash_file(file1)

        file2 = output_dir / "file2.txt"
        file2.write_text("content2")
        hash2 = _hash_file(file2)

        # Modify both
        file1.write_text("modified1")
        file2.write_text("modified2")

        template_hashes = {"file1.txt": hash1, "file2.txt": hash2}
        ignore = {"file1.txt", "file2.txt"}

        results = detect_drift(output_dir, template_hashes, ignore)

        assert len(results) == 0

    def test_detect_drift_empty_templates(self, tmp_path):
        """Empty template hashes yields no drift."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        file1 = output_dir / "file1.txt"
        file1.write_text("content")

        results = detect_drift(output_dir, {})

        assert len(results) == 0

    def test_detect_drift_empty_ignore_set(self, tmp_path):
        """Empty ignore set is same as None."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        file1 = output_dir / "file1.txt"
        file1.write_text("original")
        original_hash = _hash_file(file1)

        # Modify
        file1.write_text("modified")

        template_hashes = {"file1.txt": original_hash}

        results1 = detect_drift(output_dir, template_hashes)
        results2 = detect_drift(output_dir, template_hashes, set())

        assert len(results1) == len(results2) == 1
