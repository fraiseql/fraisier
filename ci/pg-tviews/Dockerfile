# PostgreSQL 18 carrying pg_tviews, for the `tviews` leg of quality-gate.yml.
#
# pg_tviews is in no distribution, so it is built here from a pinned ref with the
# pgrx version it pins (Cargo.toml: pgrx = "=0.17.0") against PGDG's PostgreSQL 18,
# and the tree `cargo pgrx package` writes is copied onto the official image of the
# same Debian release. This is confiture's ci/pg-tviews build. The ref is the
# commit of the signed v0.1.0-beta.35 tag (`git rev-parse v0.1.0-beta.35^{commit}`),
# which offers read contract 3; fraisier reads contracts 2 and 3. It moves in the
# same change that moves the TVIEW tests to another release. Fetched by SHA so a
# build reproduces and the layer cache never serves another tree under the same ref.
#
# pg_tviews requires jsonb_delta (`requires = 'jsonb_delta'`), built here at the
# commit of its v0.3.0 tag, the release pg_tviews' own CI runs.
FROM rust:1.91.1-slim-bookworm AS toolchain
RUN apt-get update \
 && apt-get install -y --no-install-recommends wget gnupg ca-certificates lsb-release git \
 && wget -qO- https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor > /usr/share/keyrings/pgdg.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-18 postgresql-server-dev-18 build-essential pkg-config clang libclang-dev \
 && rm -rf /var/lib/apt/lists/*
RUN cargo install --locked cargo-pgrx --version 0.17.0
RUN cargo pgrx init --pg18 /usr/lib/postgresql/18/bin/pg_config

FROM toolchain AS jsonb_delta
ARG JSONB_DELTA_REF=f5c495030a92fcf112be098e9ef15f8f24d4a728
WORKDIR /src
RUN git init -q \
 && git fetch -q --depth 1 https://github.com/evoludigit/jsonb_delta "$JSONB_DELTA_REF" \
 && git checkout -q FETCH_HEAD
RUN cargo pgrx package --no-default-features --features pg18 \
      --pg-config /usr/lib/postgresql/18/bin/pg_config --out-dir /out

FROM toolchain AS pg_tviews
ARG PG_TVIEWS_REF=84157ed337b5b94baa60f3734c04f26a17a132b6
WORKDIR /src
RUN git init -q \
 && git fetch -q --depth 1 https://github.com/fraiseql/pg_tviews "$PG_TVIEWS_REF" \
 && git checkout -q FETCH_HEAD
RUN cargo pgrx package --no-default-features --features pg18 \
      --pg-config /usr/lib/postgresql/18/bin/pg_config --out-dir /out

FROM postgres:18-bookworm
COPY --from=jsonb_delta /out/ /
COPY --from=pg_tviews /out/ /
# Preloaded, as pg_tviews requires: CREATE EXTENSION and every write refuse
# (55000) when the library is not in shared_preload_libraries.
CMD ["postgres", "-c", "shared_preload_libraries=pg_tviews"]
