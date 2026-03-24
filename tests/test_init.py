"""Tests for fraisier init command."""

import yaml
from click.testing import CliRunner

from fraisier.cli import main


class TestInitCommand:
    """Test fraisier init scaffolds a valid fraises.yaml."""

    def test_init_creates_fraises_yaml(self, tmp_path):
        """Test init creates a fraises.yaml file."""
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--output", str(tmp_path)])
        assert result.exit_code == 0

        config_file = tmp_path / "fraises.yaml"
        assert config_file.exists()

    def test_init_output_is_valid_yaml(self, tmp_path):
        """Test generated file is valid YAML."""
        runner = CliRunner()
        runner.invoke(main, ["init", "--output", str(tmp_path)])

        config_file = tmp_path / "fraises.yaml"
        data = yaml.safe_load(config_file.read_text())
        assert isinstance(data, dict)

    def test_init_has_required_sections(self, tmp_path):
        """Test generated config has fraises and environments sections."""
        runner = CliRunner()
        runner.invoke(main, ["init", "--output", str(tmp_path)])

        data = yaml.safe_load((tmp_path / "fraises.yaml").read_text())
        assert "fraises" in data
        assert "environments" in data

    def test_init_refuses_to_overwrite(self, tmp_path):
        """Test init refuses to overwrite existing fraises.yaml."""
        (tmp_path / "fraises.yaml").write_text("existing: true")

        runner = CliRunner()
        result = runner.invoke(main, ["init", "--output", str(tmp_path)])
        assert result.exit_code != 0
        assert "already exists" in result.output.lower()

    def test_init_force_overwrites(self, tmp_path):
        """Test init --force overwrites existing file."""
        (tmp_path / "fraises.yaml").write_text("existing: true")

        runner = CliRunner()
        result = runner.invoke(main, ["init", "--output", str(tmp_path), "--force"])
        assert result.exit_code == 0

        data = yaml.safe_load((tmp_path / "fraises.yaml").read_text())
        assert "fraises" in data


class TestInitDjangoTemplate:
    """Test fraisier init --template django."""

    def test_django_template_has_migration_command(self, tmp_path):
        """Test Django template includes manage.py migrate."""
        runner = CliRunner()
        runner.invoke(main, ["init", "--output", str(tmp_path), "--template", "django"])

        yaml_text = (tmp_path / "fraises.yaml").read_text()
        assert "confiture" in yaml_text or "manage.py" in yaml_text

    def test_django_template_has_gunicorn_service(self, tmp_path):
        """Test Django template references gunicorn systemd service."""
        runner = CliRunner()
        runner.invoke(main, ["init", "--output", str(tmp_path), "--template", "django"])

        yaml_text = (tmp_path / "fraises.yaml").read_text()
        assert "gunicorn" in yaml_text

    def test_django_template_has_database_section(self, tmp_path):
        """Test Django template includes PostgreSQL database config."""
        runner = CliRunner()
        runner.invoke(main, ["init", "--output", str(tmp_path), "--template", "django"])

        data = yaml.safe_load((tmp_path / "fraises.yaml").read_text())
        fraises = data.get("fraises", {})
        # Find the first fraise and check it has database config
        first_fraise = next(iter(fraises.values()))
        envs = first_fraise.get("environments", {})
        prod = envs.get("production", {})
        assert "database" in prod

    def test_django_template_has_health_check(self, tmp_path):
        """Test Django template includes health check URL."""
        runner = CliRunner()
        runner.invoke(main, ["init", "--output", str(tmp_path), "--template", "django"])

        data = yaml.safe_load((tmp_path / "fraises.yaml").read_text())
        fraises = data.get("fraises", {})
        first_fraise = next(iter(fraises.values()))
        envs = first_fraise.get("environments", {})
        prod = envs.get("production", {})
        assert "health_check" in prod


class TestInitRailsTemplate:
    """Test fraisier init --template rails."""

    def test_rails_template_has_puma(self, tmp_path):
        """Test Rails template references puma service."""
        runner = CliRunner()
        runner.invoke(main, ["init", "--output", str(tmp_path), "--template", "rails"])

        yaml_text = (tmp_path / "fraises.yaml").read_text()
        assert "puma" in yaml_text

    def test_rails_template_has_database(self, tmp_path):
        """Test Rails template includes database config."""
        runner = CliRunner()
        runner.invoke(main, ["init", "--output", str(tmp_path), "--template", "rails"])

        data = yaml.safe_load((tmp_path / "fraises.yaml").read_text())
        fraises = data.get("fraises", {})
        first_fraise = next(iter(fraises.values()))
        envs = first_fraise.get("environments", {})
        prod = envs.get("production", {})
        assert "database" in prod

    def test_rails_template_has_migration_command(self, tmp_path):
        """Test Rails template references rails db:migrate."""
        runner = CliRunner()
        runner.invoke(main, ["init", "--output", str(tmp_path), "--template", "rails"])

        yaml_text = (tmp_path / "fraises.yaml").read_text()
        assert "rails" in yaml_text or "rake" in yaml_text


class TestInitNodeTemplate:
    """Test fraisier init --template node."""

    def test_node_template_has_service(self, tmp_path):
        """Test Node template references a node service."""
        runner = CliRunner()
        runner.invoke(main, ["init", "--output", str(tmp_path), "--template", "node"])

        yaml_text = (tmp_path / "fraises.yaml").read_text()
        assert "node" in yaml_text.lower()

    def test_node_template_has_health_check(self, tmp_path):
        """Test Node template includes health check."""
        runner = CliRunner()
        runner.invoke(main, ["init", "--output", str(tmp_path), "--template", "node"])

        data = yaml.safe_load((tmp_path / "fraises.yaml").read_text())
        fraises = data.get("fraises", {})
        first_fraise = next(iter(fraises.values()))
        envs = first_fraise.get("environments", {})
        prod = envs.get("production", {})
        assert "health_check" in prod

    def test_node_template_no_database_by_default(self, tmp_path):
        """Test Node template does not include database by default."""
        runner = CliRunner()
        runner.invoke(main, ["init", "--output", str(tmp_path), "--template", "node"])

        data = yaml.safe_load((tmp_path / "fraises.yaml").read_text())
        fraises = data.get("fraises", {})
        first_fraise = next(iter(fraises.values()))
        envs = first_fraise.get("environments", {})
        prod = envs.get("production", {})
        assert "database" not in prod
