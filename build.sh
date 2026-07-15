#!/usr/bin/env bash
set -euo pipefail
OUT=dist
rm -rf "$OUT" && mkdir -p "$OUT"

build_one() {
  local os=$1 arch=$2
  local ext=""
  [[ "$os" == windows ]] && ext=".exe"
  GOOS=$os GOARCH=$arch CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o "$OUT/tunnel-server-${os}${ext}" server.go common.go
  GOOS=$os GOARCH=$arch CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o "$OUT/tunnel-client-${os}${ext}" client.go common.go
}

build_one linux amd64
build_one windows amd64
