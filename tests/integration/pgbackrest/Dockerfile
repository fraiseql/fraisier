# A host with two PostgreSQL clusters and pgBackRest, for the #424 integration test.
#
# The base is a stock PostgreSQL image, which is Debian with postgresql-common —
# the same `pg_ctlcluster` / `pg_lsclusters` tooling the helper speaks. Pass
# `--build-arg BASE=<image with pg_tviews>` to exercise the TVIEW rebuild too.
ARG BASE=postgres:18
FROM ${BASE}
USER root
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends pgbackrest postgresql-common procps \
 && rm -rf /var/lib/apt/lists/* \
 && test -x /usr/bin/pgbackrest -a -x /usr/bin/pg_ctlcluster -a -x /usr/bin/pg_lsclusters
ENTRYPOINT ["sleep", "infinity"]
