#!/usr/bin/env bash
# services/piwifi/install.sh
#
# piwifi 守護程式冪等安裝腳本（草案）
# - 建立 piwifi 系統帳號
# - 安裝 /usr/local/bin/piwifi 靜態執行檔
# - 安裝 /etc/polkit-1/rules.d/50-piwifi.rules
# - 初始化 /etc/piwifi/hotspot.conf（root:piwifi 0640，已存在則絕不覆蓋）
# - 安裝 /etc/systemd/system/piwifi.service（將 @USER@ 替換為目標使用者）
#
# 用法：
#   sudo ./install.sh [--user <username>] [--observe] [--bin <path/to/piwifi>]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-}"
OBSERVE_MODE=0
BIN_PATH=""

usage() {
    cat <<'EOF'
用法: sudo ./install.sh [選項]

選項:
  --user <username>   允許讀寫 /run/piwifi/piwifi.sock 的使用者群組名稱（預設取 SUDO_USER）
  --observe           以 --observe 唯讀觀察模式設定 piwifi.service（平行驗證階段使用）
  --bin <path>        指定要安裝的 piwifi 執行檔路徑
  -h, --help          顯示此說明
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)
            TARGET_USER="${2:-}"
            shift 2
            ;;
        --observe)
            OBSERVE_MODE=1
            shift
            ;;
        --bin)
            BIN_PATH="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "錯誤：未知參數 $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "錯誤：請使用 sudo 執行此安裝腳本。" >&2
    exit 1
fi

if [[ -z "${TARGET_USER}" || "${TARGET_USER}" == "root" ]]; then
    echo "錯誤：無法自動偵測一般使用者名稱，請使用 --user <username> 指定。" >&2
    exit 1
fi

if ! id -u "${TARGET_USER}" >/dev/null 2>&1; then
    echo "錯誤：找不到系統使用者 '${TARGET_USER}'。" >&2
    exit 1
fi

# 尋找 piwifi 執行檔
if [[ -z "${BIN_PATH}" ]]; then
    if [[ -f "${SCRIPT_DIR}/piwifi" ]]; then
        BIN_PATH="${SCRIPT_DIR}/piwifi"
    elif [[ -f "${SCRIPT_DIR}/target/aarch64-unknown-linux-musl/release/piwifi" ]]; then
        BIN_PATH="${SCRIPT_DIR}/target/aarch64-unknown-linux-musl/release/piwifi"
    elif [[ -f "${SCRIPT_DIR}/target/release/piwifi" ]]; then
        BIN_PATH="${SCRIPT_DIR}/target/release/piwifi"
    else
        echo "錯誤：找不到 piwifi 執行檔，請先編譯或透過 --bin 指定路徑。" >&2
        exit 1
    fi
fi

echo "==> [1/5] 確認系統帳號 piwifi ..."
if ! id -u piwifi >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin piwifi
    echo "    已建立系統帳號 piwifi"
else
    echo "    系統帳號 piwifi 已存在（略過建立）"
fi

echo "==> [2/5] 安裝執行檔至 /usr/local/bin/piwifi ..."
install -m 0755 -o root -g root "${BIN_PATH}" /usr/local/bin/piwifi

echo "==> [3/5] 安裝 polkit 規則 /etc/polkit-1/rules.d/50-piwifi.rules ..."
install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 -o root -g root "${SCRIPT_DIR}/50-piwifi.rules" /etc/polkit-1/rules.d/50-piwifi.rules

echo "==> [4/5] 檢查 /etc/piwifi/hotspot.conf ..."
install -d -m 0750 -o root -g piwifi /etc/piwifi
if [[ -f /etc/piwifi/hotspot.conf ]]; then
    echo "    /etc/piwifi/hotspot.conf 已存在，保留既有設定不覆蓋"
    chown root:piwifi /etc/piwifi/hotspot.conf
    chmod 0640 /etc/piwifi/hotspot.conf
else
    HOST_CLEAN="$(hostname -s 2>/dev/null | tr -cd 'A-Za-z0-9' || true)"
    if [[ ${#HOST_CLEAN} -ge 4 ]]; then
        SUFFIX="${HOST_CLEAN: -4}"
    elif [[ -n "${HOST_CLEAN}" ]]; then
        SUFFIX="${HOST_CLEAN}"
    else
        SUFFIX="0000"
    fi
    DEFAULT_SSID="DesktopPet-${SUFFIX}"
    RANDOM_PASS="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12)"
    PASSPHRASE="${PIWIFI_HOTSPOT_PASSPHRASE:-${RANDOM_PASS}}"

    umask 0027
    cat > /etc/piwifi/hotspot.conf <<EOF
# /etc/piwifi/hotspot.conf — 由 install.sh 首次建立
SSID=${DEFAULT_SSID}
PASSPHRASE=${PASSPHRASE}
EOF
    chown root:piwifi /etc/piwifi/hotspot.conf
    chmod 0640 /etc/piwifi/hotspot.conf
    echo "    已建立 /etc/piwifi/hotspot.conf（SSID=${DEFAULT_SSID}，權限 root:piwifi 0640）"
fi

echo "==> [5/5] 安裝 systemd 單元 /etc/systemd/system/piwifi.service ..."
EXEC_ARGS="daemon"
if [[ "${OBSERVE_MODE}" -eq 1 ]]; then
    EXEC_ARGS="daemon --observe"
fi

sed -e "s/@USER@/${TARGET_USER}/g" \
    -e "s|ExecStart=/usr/local/bin/piwifi daemon|ExecStart=/usr/local/bin/piwifi ${EXEC_ARGS}|g" \
    "${SCRIPT_DIR}/piwifi.service" > /etc/systemd/system/piwifi.service
chmod 0644 /etc/systemd/system/piwifi.service

systemctl daemon-reload
systemctl enable --now piwifi.service

echo "==> 安裝完成！可使用以下指令檢查狀態："
echo "    systemctl status piwifi.service"
echo "    piwifi status"
