# syntax=docker/dockerfile:1
ARG RUST_IMAGE=rust:1-bookworm@sha256:e6f2cff35d0f309acf8433b4f05878ed77092602c5065cc81801177da0bb3a9c
ARG BREF_IMAGE=bref/php-84:3@sha256:cf1cd6aeeed301781b6a04a741fdecd6d40c1079bb25549b82df10c69633870f

FROM ${RUST_IMAGE} AS rust-build
ARG RUST_TARGET=x86_64-unknown-linux-musl
ARG RUN_RUST_TESTS=0
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src/ ./src/
RUN rustup target add "${RUST_TARGET}" && \
    if [ "${RUN_RUST_TESTS}" = 1 ]; then cargo test --locked; fi && \
    RUSTFLAGS="-C linker=rust-lld" \
    cargo build --release --locked --target "${RUST_TARGET}" && \
    mkdir /out && cp "target/${RUST_TARGET}/release/bref-web-gateway" /out/

# Bref's PHP distribution and the gateway. CI exports /opt and the gateway from
# this image to build deployment archives; runtime/php/tests.sh runs the shared
# initialization in it.
FROM ${BREF_IMAGE} AS runtime
COPY --from=rust-build /out/bref-web-gateway /usr/local/bin/bref-web-gateway
USER 1000:1000
