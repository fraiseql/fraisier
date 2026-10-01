"""A throwaway "production + staging" host in Docker, for the pgBackRest source (#424).

Production is a cluster with WAL archiving into a pgBackRest repository; staging
is a second, dedicated cluster that is refreshed from it.  Both are Debian-style
clusters (``pg_createcluster``), which is what the helper's ``18/staging`` names.

Nothing here is the code under test.  ``Lab.run`` is the one seam: it is the
helper's command runner, so the helper's real argv construction, parsing and
guards run — and each command executes inside the container instead of on this
machine.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).parent
PGBACKREST_CONF = """\
[global]
repo1-path=/var/lib/pgbackrest
repo1-retention-full=3
start-fast=y
log-level-console=info

[main]
pg1-path=/var/lib/postgresql/18/prod
pg1-port=5432
"""

SETUP = """\
set -e
mkdir -p /var/lib/pgbackrest /var/log/pgbackrest /etc/pgbackrest
chown postgres:postgres /var/lib/pgbackrest /var/log/pgbackrest
cat > /etc/pgbackrest/pgbackrest.conf <<'CONF'
{pgbackrest_conf}CONF
chown postgres /etc/pgbackrest/pgbackrest.conf
pg_createcluster 18 prod -p 5432 -- --auth-local=trust >/dev/null 2>&1
pg_createcluster 18 staging -p 5433 -- --auth-local=trust >/dev/null 2>&1
cat > /etc/postgresql/18/prod/conf.d/lab.conf <<'CONF'
archive_mode = on
archive_command = 'pgbackrest --stanza=main archive-push %p'
wal_level = replica
CONF
# staging is reachable from the host through the published port
cat > /etc/postgresql/18/staging/conf.d/lab.conf <<'CONF'
listen_addresses = '*'
CONF
echo 'host all all 0.0.0.0/0 trust' >> /etc/postgresql/18/staging/pg_hba.conf
if [ -f /usr/lib/postgresql/18/lib/pg_tviews.so ] || [ -f /usr/lib/postgresql/18/lib/libpg_tviews.so ]; then
  for c in prod staging; do
    echo "shared_preload_libraries = 'pg_tviews'" >> /etc/postgresql/18/$c/conf.d/lab.conf
  done
fi
pg_ctlcluster 18 prod start
su postgres -c 'pgbackrest --stanza=main stanza-create' >/dev/null
"""


def docker_available() -> str | None:
    """``None`` when Docker works, else the reason it does not."""
    if shutil.which("docker") is None:
        return "docker is not installed"
    probe = subprocess.run(["docker", "info"], capture_output=True, check=False)
    return None if probe.returncode == 0 else "the docker daemon is not reachable"


def build_image(base: str) -> str:
    """The harness image for *base*, built once and then reused."""
    tag = "fraisier-pgbackrest-it:" + base.replace(":", "-").replace("/", "-")
    present = subprocess.run(
        ["docker", "image", "inspect", tag], capture_output=True, check=False
    )
    if present.returncode != 0:
        subprocess.run(
            [
                "docker",
                "build",
                "-q",
                "--build-arg",
                f"BASE={base}",
                "-t",
                tag,
                str(HERE),
            ],
            check=True,
            capture_output=True,
        )
    return tag


@dataclass(frozen=True)
class Lab:
    """A running container holding the ``prod`` and ``staging`` clusters."""

    container: str
    staging_port: int  # published on 127.0.0.1

    def exec(
        self,
        *argv: str,
        user: str | None = None,
        check: bool = True,
        timeout: int = 600,
    ) -> subprocess.CompletedProcess[str]:
        user_flag = ["-u", user] if user else []
        return subprocess.run(
            ["docker", "exec", *user_flag, self.container, *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )

    def sh(
        self, script: str, *, user: str | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.exec("bash", "-c", script, user=user, check=check)

    def psql(self, port: int, db: str, sql: str) -> str:
        """Run *sql* as postgres on a cluster's own socket; returns trimmed output."""
        result = self.exec(
            "psql", "-p", str(port), "-qAt", "-d", db, "-c", sql, user="postgres"
        )
        return result.stdout.strip()

    def cluster_status(self, name: str) -> str:
        listing = self.exec("pg_lsclusters", "--no-header").stdout
        for line in listing.splitlines():
            fields = line.split()
            if len(fields) > 3 and fields[1] == name:
                return fields[3]
        return "missing"

    def run(
        self, argv: list[str], *, user: str | None, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        """The helper's runner: its commands, executed in the container."""
        return self.exec(*argv, user=user, check=False, timeout=timeout)

    def admin_url(self) -> str:
        return f"postgresql://postgres@127.0.0.1:{self.staging_port}/postgres"

    def has_pg_tviews(self) -> bool:
        result = self.exec(
            "psql",
            "-p",
            "5432",
            "-qAt",
            "-d",
            "postgres",
            "-c",
            "select 1 from pg_available_extensions where name = 'pg_tviews'",
            user="postgres",
            check=False,
        )
        return result.stdout.strip() == "1"


def start_lab(image: str, staging_port: int) -> Lab:
    name = f"fraisier-pgbr-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{staging_port}:5433",
            image,
        ],
        check=True,
        capture_output=True,
    )
    lab = Lab(name, staging_port)
    lab.sh(SETUP.format(pgbackrest_conf=PGBACKREST_CONF))
    return lab


def stop_lab(lab: Lab) -> None:
    with contextlib.suppress(Exception):
        subprocess.run(
            ["docker", "rm", "-f", lab.container], capture_output=True, check=False
        )
