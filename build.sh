#!/usr/bin/env bash
set -euo pipefail
OUT=dist
rm -rf "$OUT" && mkdir -p "$OUT"

build_one() {
  local os=$1 arch=$2
  local ext=""
  [[ "$os" == windows ]] && ext=".exe"
  local suffix=$os
  [[ "$os" == darwin ]] && suffix="${os}-${arch}"
  GOOS=$os GOARCH=$arch CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o "$OUT/tunnel-server-${suffix}${ext}" server.go common.go
  GOOS=$os GOARCH=$arch CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o "$OUT/tunnel-client-${suffix}${ext}" client.go common.go
}

build_one linux amd64
build_one windows amd64
build_one darwin amd64
build_one darwin arm64
