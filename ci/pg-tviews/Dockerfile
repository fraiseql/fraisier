# PostgreSQL 18 carrying pg_tviews, for the `tviews` leg of quality-gate.yml.
#
# pg_tviews is in no distribution, so it is built here from a pinned ref with the
# pgrx version it pins (Cargo.toml: pgrx = "=0.17.0") against PGDG's PostgreSQL 18,
# and the tree `cargo pgrx package` writes is copied onto the official image of the
# same Debian release. This is confiture's ci/pg-tviews build. The ref is the
# commit of the signed v0.1.0-beta.32 tag (`git rev-parse v0.1.0-beta.32^{commit}`),
# the newest release; fraisier and confiture 1.39 require beta.30 or later. It
# moves in the same change that moves the TVIEW tests to another release. Fetched by SHA so a build reproduces and the
# layer cache never serves another tree under the same ref.
FROM rust:1.91.1-slim-bookworm AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends wget gnupg ca-certificates lsb-release git \
 && wget -qO- https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor > /usr/share/keyrings/pgdg.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-18 postgresql-server-dev-18 build-essential pkg-config clang libclang-dev \
 && rm -rf /var/lib/apt/lists/*
RUN cargo install --locked cargo-pgrx --version 0.17.0
RUN cargo pgrx init --pg18 /usr/lib/postgresql/18/bin/pg_config
ARG PG_TVIEWS_REF=778346fb5df06a38f470901974d609d2b9dc26a6
WORKDIR /src
RUN git init -q \
 && git fetch -q --depth 1 https://github.com/fraiseql/pg_tviews "$PG_TVIEWS_REF" \
 && git checkout -q FETCH_HEAD
RUN cargo pgrx package --no-default-features --features pg18 \
      --pg-config /usr/lib/postgresql/18/bin/pg_config --out-dir /out

FROM postgres:18-bookworm
COPY --from=build /out/ /
# Preloaded, as pg_tviews requires: a session that has not loaded the library
# turns `CREATE TABLE tv_x AS …` into a plain table and registers nothing.
CMD ["postgres", "-c", "shared_preload_libraries=pg_tviews"]
