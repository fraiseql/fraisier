"""Tests for version management and CI/CD templates."""

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner


class TestVersionInfo:
    """Test VersionInfo dataclass and serialization."""

    def test_create_version_info(self):
        from fraisier.versioning import VersionInfo

        v = VersionInfo(version="1.2.3")
        assert v.version == "1.2.3"

    def test_version_info_all_fields(self):
        from fraisier.versioning import VersionInfo

        v = VersionInfo(
            version="1.2.3",
            commit="abc1234",
            branch="main",
            timestamp="2026-03-22T10:00:00Z",
            environment="production",
            schema_hash="sha256:abcdef",
            database_version="2026.03.22.001",
        )
        assert v.version == "1.2.3"
        assert v.commit == "abc1234"
        assert v.schema_hash == "sha256:abcdef"
        assert v.database_version == "2026.03.22.001"

    def test_version_info_to_dict(self):
        from fraisier.versioning import VersionInfo

        v = VersionInfo(version="1.0.0", commit="abc")
        d = v.to_dict()
        assert d["version"] == "1.0.0"
        assert d["commit"] == "abc"
        assert isinstance(d, dict)

    def test_version_info_from_dict(self):
        from fraisier.versioning import VersionInfo

        d = {
            "version": "2.0.0",
            "commit": "def456",
            "branch": "dev",
        }
        v = VersionInfo.from_dict(d)
        assert v.version == "2.0.0"
        assert v.commit == "def456"
        assert v.branch == "dev"

    def test_version_info_ignores_unknown_fields(self):
        from fraisier.versioning import VersionInfo

        d = {"version": "1.0.0", "unknown_field": "value"}
        v = VersionInfo.from_dict(d)
        assert v.version == "1.0.0"


class TestVersionFileIO:
    """Test reading/writing version.json."""

    def test_write_version_json(self, tmp_path):
        from fraisier.versioning import VersionInfo, write_version

        v = VersionInfo(version="1.0.0", commit="abc")
        path = tmp_path / "version.json"
        write_version(v, path)

        data = json.loads(path.read_text())
        assert data["version"] == "1.0.0"
        assert data["commit"] == "abc"

    def test_read_version_json(self, tmp_path):
        from fraisier.versioning import read_version

        path = tmp_path / "version.json"
        path.write_text(
            json.dumps(
                {
                    "version": "1.2.3",
                    "commit": "xyz",
                }
            )
        )

        v = read_version(path)
        assert v.version == "1.2.3"
        assert v.commit == "xyz"

    def test_read_version_returns_none_for_missing(self, tmp_path):
        from fraisier.versioning import read_version

        path = tmp_path / "nonexistent.json"
        v = read_version(path)
        assert v is None

    def test_roundtrip(self, tmp_path):
        from fraisier.versioning import VersionInfo, read_version, write_version

        original = VersionInfo(
            version="3.1.4",
            commit="pi",
            branch="release",
            timestamp="2026-03-22T12:00:00Z",
            environment="staging",
            schema_hash="sha256:hash123",
            database_version="2026.03.22.002",
        )
        path = tmp_path / "version.json"
        write_version(original, path)
        loaded = read_version(path)

        assert loaded is not None
        assert loaded.version == original.version
        assert loaded.schema_hash == original.schema_hash
        assert loaded.database_version == original.database_version


class TestSemverValidation:
    """Test semver format validation."""

    def test_valid_semver(self):
        from fraisier.versioning import is_valid_semver

        assert is_valid_semver("1.0.0") is True
        assert is_valid_semver("0.1.0") is True
        assert is_valid_semver("10.20.30") is True

    def test_invalid_semver(self):
        from fraisier.versioning import is_valid_semver

        assert is_valid_semver("1.0") is False
        assert is_valid_semver("v1.0.0") is False
        assert is_valid_semver("abc") is False
        assert is_valid_semver("") is False

    def test_parse_semver(self):
        from fraisier.versioning import parse_semver

        major, minor, patch_v = parse_semver("1.2.3")
        assert major == 1
        assert minor == 2
        assert patch_v == 3

    def test_parse_semver_invalid_raises(self):
        from fraisier.versioning import parse_semver

        with pytest.raises(ValueError, match="Invalid semver"):
            parse_semver("bad")


class TestBumpVersion:
    """Test atomic version bumping."""

    def test_bump_patch(self, tmp_path):
        from fraisier.versioning import (
            VersionInfo,
            bump_version,
            read_version,
            write_version,
        )

        path = tmp_path / "version.json"
        write_version(VersionInfo(version="1.2.3"), path)

        result = bump_version(path, "patch")
        assert result.version == "1.2.4"

        loaded = read_version(path)
        assert loaded is not None
        assert loaded.version == "1.2.4"

    def test_bump_minor(self, tmp_path):
        from fraisier.versioning import (
            VersionInfo,
            bump_version,
            write_version,
        )

        path = tmp_path / "version.json"
        write_version(VersionInfo(version="1.2.3"), path)

        result = bump_version(path, "minor")
        assert result.version == "1.3.0"

    def test_bump_major(self, tmp_path):
        from fraisier.versioning import (
            VersionInfo,
            bump_version,
            write_version,
        )

        path = tmp_path / "version.json"
        write_version(VersionInfo(version="1.2.3"), path)

        result = bump_version(path, "major")
        assert result.version == "2.0.0"

    def test_bump_creates_backup(self, tmp_path):
        from fraisier.versioning import (
            VersionInfo,
            bump_version,
            write_version,
        )

        path = tmp_path / "version.json"
        write_version(VersionInfo(version="1.0.0"), path)

        bump_version(path, "patch")
        backup = tmp_path / "version.json.bak"
        assert backup.exists()

        data = json.loads(backup.read_text())
        assert data["version"] == "1.0.0"

    def test_bump_invalid_part_raises(self, tmp_path):
        from fraisier.versioning import (
            VersionInfo,
            bump_version,
            write_version,
        )

        path = tmp_path / "version.json"
        write_version(VersionInfo(version="1.0.0"), path)

        with pytest.raises(ValueError, match="Invalid bump part"):
            bump_version(path, "invalid")

    def test_bump_missing_file_raises(self, tmp_path):
        from fraisier.versioning import bump_version

        path = tmp_path / "nonexistent.json"
        with pytest.raises(FileNotFoundError):
            bump_version(path, "patch")


class TestSyncPyproject:
    """Test pyproject.toml version sync."""

    def test_sync_updates_pyproject(self, tmp_path):
        from fraisier.versioning import sync_pyproject_version

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "myapp"\nversion = "1.0.0"\n')

        sync_pyproject_version("2.0.0", pyproject)

        content = pyproject.read_text()
        assert 'version = "2.0.0"' in content

    def test_sync_preserves_other_content(self, tmp_path):
        from fraisier.versioning import sync_pyproject_version

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\ndescription = "test"\n'
        )

        sync_pyproject_version("2.0.0", pyproject)

        content = pyproject.read_text()
        assert 'name = "myapp"' in content
        assert 'description = "test"' in content
        assert 'version = "2.0.0"' in content

    def test_sync_missing_file_raises(self, tmp_path):
        from fraisier.versioning import sync_pyproject_version

        pyproject = tmp_path / "nonexistent.toml"
        with pytest.raises(FileNotFoundError):
            sync_pyproject_version("1.0.0", pyproject)


class TestUpdateSchemaHash:
    """Test schema hash update in version.json."""

    def test_update_schema_hash(self, tmp_path):
        from fraisier.versioning import (
            VersionInfo,
            read_version,
            update_schema_info,
            write_version,
        )

        version_path = tmp_path / "version.json"
        write_version(VersionInfo(version="1.0.0"), version_path)

        schema_dir = tmp_path / "sql"
        schema_dir.mkdir()
        (schema_dir / "001.sql").write_text("CREATE TABLE x (id int);")

        update_schema_info(version_path, schema_dir)

        v = read_version(version_path)
        assert v is not None
        assert v.schema_hash.startswith("sha256:")
        assert len(v.schema_hash) > 10

    def test_schema_hash_changes_on_migration(self, tmp_path):
        from fraisier.versioning import (
            VersionInfo,
            read_version,
            update_schema_info,
            write_version,
        )

        version_path = tmp_path / "version.json"
        write_version(VersionInfo(version="1.0.0"), version_path)

        schema_dir = tmp_path / "sql"
        schema_dir.mkdir()
        (schema_dir / "001.sql").write_text("CREATE TABLE x (id int);")

        update_schema_info(version_path, schema_dir)
        v1 = read_version(version_path)

        (schema_dir / "002.sql").write_text("ALTER TABLE x ADD y text;")
        update_schema_info(version_path, schema_dir)
        v2 = read_version(version_path)

        assert v1 is not None
        assert v2 is not None
        assert v1.schema_hash != v2.schema_hash


class TestDatabaseVersion:
    """Test database version derivation."""

    def test_derive_database_version(self):
        from fraisier.versioning import derive_database_version

        # Should produce YYYY.MM.DD.NNN format
        dbv = derive_database_version(sequence=1)
        assert dbv.count(".") == 3
        parts = dbv.split(".")
        assert len(parts[0]) == 4  # year
        assert len(parts[1]) == 2  # month
        assert len(parts[2]) == 2  # day
        assert int(parts[3]) == 1

    def test_derive_database_version_sequence(self):
        from fraisier.versioning import derive_database_version

        dbv = derive_database_version(sequence=42)
        assert dbv.endswith(".042")

    def test_update_schema_info_sets_database_version(self, tmp_path):
        from fraisier.versioning import (
            VersionInfo,
            read_version,
            update_schema_info,
            write_version,
        )

        version_path = tmp_path / "version.json"
        write_version(VersionInfo(version="1.0.0"), version_path)

        schema_dir = tmp_path / "sql"
        schema_dir.mkdir()
        (schema_dir / "001.sql").write_text("CREATE TABLE x;")
        (schema_dir / "002.sql").write_text("ALTER TABLE x;")

        update_schema_info(version_path, schema_dir)

        v = read_version(version_path)
        assert v is not None
        assert v.database_version != ""
        # Sequence should be 2 (number of SQL files)
        assert v.database_version.endswith(".002")


@pytest.fixture
def version_config_file(tmp_path):
    """Create fraises.yaml + version.json for CLI tests."""
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text("""
git:
  provider: github
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/app
        systemd_service: api.service
version:
  track_schema_hash: true
  sync_pyproject: true
""")

    version_file = tmp_path / "version.json"
    version_file.write_text(
        json.dumps(
            {
                "version": "1.2.3",
                "commit": "abc",
                "branch": "main",
            }
        )
    )

    return tmp_path, str(config_file)


class TestVersionShowCommand:
    """Test fraisier version show CLI command."""

    def test_version_show(self, version_config_file):
        from fraisier.cli import main

        tmp_path, config_path = version_config_file
        runner = CliRunner()

        with patch(
            "fraisier.versioning.Path.cwd",
            return_value=tmp_path,
        ):
            result = runner.invoke(
                main,
                [
                    "-c",
                    config_path,
                    "version",
                    "show",
                    "--version-file",
                    str(tmp_path / "version.json"),
                ],
            )

        assert result.exit_code == 0, result.output
        assert "1.2.3" in result.output

    def test_version_show_missing_file(self, version_config_file):
        from fraisier.cli import main

        _, config_path = version_config_file
        runner = CliRunner()

        result = runner.invoke(
            main,
            [
                "-c",
                config_path,
                "version",
                "show",
                "--version-file",
                "/nonexistent/version.json",
            ],
        )

        assert result.exit_code != 0 or "not found" in result.output.lower()


class TestVersionBumpCommand:
    """Test fraisier version bump CLI command."""

    def test_version_bump_patch(self, version_config_file):
        from fraisier.cli import main

        tmp_path, config_path = version_config_file
        runner = CliRunner()

        result = runner.invoke(
            main,
            [
                "-c",
                config_path,
                "version",
                "bump",
                "patch",
                "--version-file",
                str(tmp_path / "version.json"),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "1.2.4" in result.output

        # Verify file was actually updated
        data = json.loads((tmp_path / "version.json").read_text())
        assert data["version"] == "1.2.4"

    def test_version_bump_dry_run(self, version_config_file):
        from fraisier.cli import main

        tmp_path, config_path = version_config_file
        runner = CliRunner()

        result = runner.invoke(
            main,
            [
                "-c",
                config_path,
                "version",
                "bump",
                "minor",
                "--version-file",
                str(tmp_path / "version.json"),
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "1.3.0" in result.output

        # Verify file was NOT updated
        data = json.loads((tmp_path / "version.json").read_text())
        assert data["version"] == "1.2.3"


class TestVersionGating:
    """Test version comparison for deploy gating."""

    def test_version_changed(self):
        from fraisier.versioning import has_version_changed

        assert has_version_changed("1.0.0", "1.0.1") is True
        assert has_version_changed("1.0.0", "2.0.0") is True

    def test_version_unchanged(self):
        from fraisier.versioning import has_version_changed

        assert has_version_changed("1.0.0", "1.0.0") is False

    def test_version_changed_none_old(self):
        from fraisier.versioning import has_version_changed

        assert has_version_changed(None, "1.0.0") is True


class TestCliVersionFlag:
    """Test fraisier --version flag."""

    def test_version_flag_outputs_version(self):
        from fraisier.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["--version"])

        assert result.exit_code == 0
        assert "fraisier" in result.output
        # Should contain a version-like string (e.g. "0.3.4")
        import re

        assert re.search(r"\d+\.\d+\.\d+", result.output)

    def test_version_full_flag_outputs_system_info(self):
        from fraisier.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["version", "--full"])

        assert result.exit_code == 0
        assert "Fraisier v" in result.output
        # Should contain system information
        assert (
            "Python" in result.output
            or "systemd" in result.output
            or "platform" in result.output
        )


class TestDeployTemplateVersionGating:
    """Test that deploy.yml template includes version gating."""

    def test_deploy_yml_contains_version_check(self, tmp_path):
        from fraisier.config import FraisierConfig
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
git:
  provider: github
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/app
        systemd_service: api.service
scaffold:
  output_dir: """
            + str(tmp_path / "output")
        )

        config = FraisierConfig(str(config_file))
        renderer = ScaffoldRenderer(config)
        files = renderer.render()

        assert "deploy.yml" in files

        deploy_yml = (tmp_path / "output" / "deploy.yml").read_text()
        assert "version.json" in deploy_yml

    def test_deploy_yml_has_force_deploy_option(self, tmp_path):
        from fraisier.config import FraisierConfig
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
git:
  provider: github
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/app
        systemd_service: api.service
scaffold:
  output_dir: """
            + str(tmp_path / "output")
        )

        config = FraisierConfig(str(config_file))
        renderer = ScaffoldRenderer(config)
        renderer.render()

        deploy_yml = (tmp_path / "output" / "deploy.yml").read_text()
        assert "force" in deploy_yml.lower() or "workflow_dispatch" in deploy_yml
