FROM emscripten/emsdk:latest AS wasm-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends cmake curl make perl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src/client
COPY client /src/client
RUN ./core/twelve_c_wasm/build-wasm.sh

FROM node:22-bookworm-slim AS web-builder
WORKDIR /src/client
COPY client/package.json client/package-lock.json ./
COPY client/transfer ./transfer
COPY client/app ./app
COPY client/web ./web
COPY --from=wasm-builder \
    /src/client/transfer/src/wasm/pkg/twelve_c_cryptography.wasm \
    /src/client/transfer/src/wasm/pkg/twelve_c_cryptography.wasm
COPY --from=wasm-builder \
    /src/client/transfer/src/wasm/pkg/twelve_c_cryptography.js \
    /src/client/transfer/src/wasm/pkg/twelve_c_cryptography.js
COPY deploy/gateway/relay.config.json ./web/public/relay.config.json
RUN npm ci && npm run build -w @stateless-relay/web

FROM nginx:1.27-alpine
COPY deploy/gateway/nginx.conf /etc/nginx/nginx.conf
COPY --from=web-builder /src/client/web/dist /usr/share/nginx/html
EXPOSE 8080
