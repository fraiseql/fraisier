"""Project init templates for ``fraisier init``.

Each template returns a YAML string for a fraises.yaml scaffold
tailored to a specific framework.
"""


def generic_template() -> str:
    """Generic fraises.yaml template."""
    return """\
# Fraisier Configuration
# See https://github.com/your-org/fraisier for full documentation

fraises:
  my_app:
    type: api
    description: My application
    environments:
      development:
        name: my-app-dev
        branch: dev
        app_path: /var/www/my-app-dev
        systemd_service: my-app-dev.service
        health_check:
          url: http://localhost:8000/health
          timeout: 30

      production:
        name: my-app
        branch: main
        app_path: /var/www/my-app
        systemd_service: my-app.service
        health_check:
          url: http://localhost:8000/health
          timeout: 30

environments:
  development:
    server: dev.example.com
  production:
    server: prod.example.com
"""


def django_template() -> str:
    """Django + PostgreSQL + gunicorn template."""
    return """\
# Fraisier Configuration — Django + PostgreSQL
# Migrations via confiture (wraps manage.py migrate)

fraises:
  django_app:
    type: api
    description: Django application
    environments:
      development:
        name: django-app-dev
        branch: dev
        app_path: /var/www/django-app-dev
        systemd_service: gunicorn-django-dev.service
        database:
          name: django_dev
          strategy: rebuild
          # Migration command: confiture migrate up
          # (or manage.py migrate if not using confiture)
        health_check:
          url: http://localhost:8000/health
          timeout: 30

      production:
        name: django-app
        branch: main
        app_path: /var/www/django-app
        systemd_service: gunicorn-django.service
        database:
          name: django_production
          strategy: migrate
          backup_before_deploy: true
        health_check:
          url: http://localhost:8000/health
          timeout: 30

environments:
  development:
    server: dev.example.com
  production:
    server: prod.example.com
"""


def rails_template() -> str:
    """Rails + PostgreSQL + puma template."""
    return """\
# Fraisier Configuration — Rails + PostgreSQL
# Migrations via rails db:migrate

fraises:
  rails_app:
    type: api
    description: Rails application
    environments:
      development:
        name: rails-app-dev
        branch: dev
        app_path: /var/www/rails-app-dev
        systemd_service: puma-rails-dev.service
        database:
          name: rails_dev
          strategy: rebuild
          # Migration: rails db:migrate / rake db:migrate
        health_check:
          url: http://localhost:3000/health
          timeout: 30

      production:
        name: rails-app
        branch: main
        app_path: /var/www/rails-app
        systemd_service: puma-rails.service
        database:
          name: rails_production
          strategy: migrate
          backup_before_deploy: true
        health_check:
          url: http://localhost:3000/health
          timeout: 30

environments:
  development:
    server: dev.example.com
  production:
    server: prod.example.com
"""


def node_template() -> str:
    """Node.js application template (no database by default)."""
    return """\
# Fraisier Configuration — Node.js
# No database section — add one if your app uses PostgreSQL

fraises:
  node_app:
    type: api
    description: Node.js application
    environments:
      development:
        name: node-app-dev
        branch: dev
        app_path: /var/www/node-app-dev
        systemd_service: node-app-dev.service
        health_check:
          url: http://localhost:3000/health
          timeout: 30

      production:
        name: node-app
        branch: main
        app_path: /var/www/node-app
        systemd_service: node-app.service
        health_check:
          url: http://localhost:3000/health
          timeout: 30

environments:
  development:
    server: dev.example.com
  production:
    server: prod.example.com
"""


TEMPLATES: dict[str, callable] = {
    "generic": generic_template,
    "django": django_template,
    "rails": rails_template,
    "node": node_template,
}
