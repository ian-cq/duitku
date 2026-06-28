# Multi-stage Dockerfile for duitku.
#
# Stage 1: build a static linux/amd64 binary with CGO disabled. We use
# modernc.org/sqlite (pure-Go) so CGO is not required even for the
# dedup store.
#
# Stage 2: distroless static base. No shell, no apt, no busybox; just
# the binary plus CA roots.

FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod go.sum* ./
RUN go mod download
COPY . .
ARG VERSION=dev
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build \
      -trimpath \
      -ldflags="-s -w -X main.version=${VERSION}" \
      -o /out/duitku \
      ./cmd/duitku

FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /
COPY --from=build /out/duitku /duitku
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/duitku"]
