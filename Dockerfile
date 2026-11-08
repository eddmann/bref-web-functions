# syntax=docker/dockerfile:1
ARG RUST_IMAGE=rust:1-bookworm@sha256:e6f2cff35d0f309acf8433b4f05878ed77092602c5065cc81801177da0bb3a9c
ARG NODE_IMAGE=public.ecr.aws/lambda/nodejs:24@sha256:710c806b12813c522a7293199a32e9fbb766c664667dcdec90d3458342c40b3d
ARG CADDY_IMAGE=caddy:2.11.9@sha256:0c2243efcf437ab7f7a978f1027fcf0d4f9be8a75df27b1b3fbc5dbe0ffa8cf0
ARG BREF_IMAGE=bref/php-84:3@sha256:cf1cd6aeeed301781b6a04a741fdecd6d40c1079bb25549b82df10c69633870f

FROM ${RUST_IMAGE} AS rust-build
ARG RUST_TARGET=x86_64-unknown-linux-musl
ARG RUN_RUST_TESTS=0
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src/ ./src/
RUN --mount=type=secret,id=proxy_ca,required=true \
    SSL_CERT_FILE=/run/secrets/proxy_ca \
    CURL_CA_BUNDLE=/run/secrets/proxy_ca \
    rustup target add "${RUST_TARGET}" && \
    if [ "${RUN_RUST_TESTS}" = 1 ]; then \
        CARGO_HTTP_CAINFO=/run/secrets/proxy_ca cargo test --locked; \
    fi && \
    CARGO_HTTP_CAINFO=/run/secrets/proxy_ca \
    RUSTFLAGS="-C linker=rust-lld" \
    cargo build --release --locked --target "${RUST_TARGET}" && \
    mkdir /out && cp "target/${RUST_TARGET}/release/bref-web-gateway" /out/

FROM ${NODE_IMAGE} AS node
FROM ${CADDY_IMAGE} AS caddy

FROM ${BREF_IMAGE} AS common
COPY --from=node /var/lang /var/lang
COPY --from=caddy /usr/bin/caddy /usr/local/bin/caddy
RUN ln -s /var/lang/bin/node /usr/local/bin/node
WORKDIR /var/task
COPY --chown=1000:1000 runtime/php/ ./compat/
COPY --chown=1000:1000 runtime/launcher/ ./runtime/launcher/
COPY --chown=1000:1000 reference/caddy/ ./reference/caddy/
COPY --chown=1000:1000 tests/fixtures/public/ ./public/
RUN chmod +x /var/task/compat/entrypoint.sh
ENV GOMAXPROCS=2 \
    GOMEMLIMIT=128MiB \
    NODE_OPTIONS=--max-old-space-size=64 \
    BREF_WEB_LAUNCHER=/var/task/runtime/launcher/index.js \
    BREF_WEB_GATEWAY=rust \
    AWS_LAMBDA_HTTP_ENDPOINT=0.0.0.0:8080 \
    BREF_WEB_FPM_ADDR=127.0.0.1:9000 \
    BREF_WEB_MAX_IN_FLIGHT=4 \
    BREF_WEB_QUEUE_CAPACITY=4 \
    BREF_WEB_REQUEST_TIMEOUT_MS=30000 \
    BREF_WEB_MAX_BODY_BYTES=6291456
USER 1000:1000
EXPOSE 8080
ENTRYPOINT ["/var/task/compat/entrypoint.sh"]
CMD []

FROM common AS baseline-prototype
ENV BREF_WEB_GATEWAY=caddy

FROM scratch AS rust-export
COPY --from=rust-build /out/bref-web-gateway /bref-web-gateway

FROM common AS prototype
COPY --from=rust-build /out/bref-web-gateway /usr/local/bin/bref-web-gateway
