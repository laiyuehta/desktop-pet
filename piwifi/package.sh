#!/usr/bin/env bash
# piwifi/package.sh
#
# 交叉編譯並打包 piwifi 發行套件：
#   dist/piwifi-<version>-aarch64-linux-musl.tar.gz
#   dist/piwifi-<version>-aarch64-linux-musl.tar.gz.sha256
#
# 產物置於 dist/（已列入 .gitignore，不進 git 版本控制）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET="aarch64-unknown-linux-musl"

cd "${SCRIPT_DIR}"

VERSION="$(awk -F'"' '/^version *= *"/ { print $2; exit }' Cargo.toml)"
if [[ -z "${VERSION}" ]]; then
    echo "錯誤：無法從 Cargo.toml 解析版本號" >&2
    exit 1
fi

PKG_NAME="piwifi-${VERSION}-aarch64-linux-musl"
DIST_DIR="${REPO_ROOT}/dist"
STAGE_DIR="${DIST_DIR}/${PKG_NAME}"
ARCHIVE="${DIST_DIR}/${PKG_NAME}.tar.gz"
SHA_FILE="${ARCHIVE}.sha256"

echo "==> [1/3] 交叉編譯 ${PKG_NAME} (${TARGET}) ..."
cargo build --release --target "${TARGET}"

BIN_SRC="${SCRIPT_DIR}/target/${TARGET}/release/piwifi"
if [[ ! -f "${BIN_SRC}" ]]; then
    echo "錯誤：找不到編譯產物 ${BIN_SRC}" >&2
    exit 1
fi

echo "==> [2/3] 準備打包目錄 ${STAGE_DIR} ..."
rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}"

install -m 0755 "${BIN_SRC}" "${STAGE_DIR}/piwifi"
install -m 0755 "${SCRIPT_DIR}/install.sh" "${STAGE_DIR}/install.sh"
install -m 0644 "${SCRIPT_DIR}/piwifi.service" "${STAGE_DIR}/piwifi.service"
install -m 0644 "${SCRIPT_DIR}/50-piwifi.rules" "${STAGE_DIR}/50-piwifi.rules"
install -m 0640 "${SCRIPT_DIR}/hotspot.conf.example" "${STAGE_DIR}/hotspot.conf.example"
install -m 0644 "${SCRIPT_DIR}/README.md" "${STAGE_DIR}/README.md"
install -m 0644 "${SCRIPT_DIR}/PROTOCOL.md" "${STAGE_DIR}/PROTOCOL.md"
install -m 0644 "${SCRIPT_DIR}/CHANGELOG.md" "${STAGE_DIR}/CHANGELOG.md"

echo "==> [3/3] 產生壓縮檔與 SHA-256 雜湊 ..."
tar -C "${DIST_DIR}" -czf "${ARCHIVE}" "${PKG_NAME}"
rm -rf "${STAGE_DIR}"

(
    cd "${DIST_DIR}"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$(basename "${ARCHIVE}")" > "$(basename "${SHA_FILE}")"
    else
        shasum -a 256 "$(basename "${ARCHIVE}")" > "$(basename "${SHA_FILE}")"
    fi
)

echo "==> 打包完成："
echo "    ${ARCHIVE}"
echo "    ${SHA_FILE}"
