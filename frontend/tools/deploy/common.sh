# desktop-pet 部署共用函式（給各 install.sh / start.sh 用 `. frontend/tools/deploy/common.sh` 載入）。
# 部署值優先順序（PROPOSAL.md 16.2）：
#   1. 已設定的 shell 環境變數
#   2. PET_DEPLOY_ENV 指定的檔案
#   3. /etc/desktop-pet/env（可用 PET_SYS_ENV 覆寫路徑，測試用）
#   4. 舊版 $PET_REPO_ROOT/deploy.env（過渡期相容，讀到時印警告到 stderr）
# 模板裡的佔位：@USER@ 執行使用者、@HOME@ 其家目錄。install 腳本只讀不寫環境檔。
# 呼叫端要先設 PET_REPO_ROOT（repo 根目錄的絕對路徑）；沒設就用目前目錄。
PET_REPO_ROOT="${PET_REPO_ROOT:-$(pwd)}"

_load_env_file_defaults() {
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line="${_line%$(printf '\r')}"
        case "$_line" in
            ""|\#*|\ *\#*) continue ;;
            *=*)
                _k="${_line%%=*}"
                _v="${_line#*=}"
                # 值留空視為未設；呼叫端已設的非空環境變數優先
                if [ -n "$_k" ] && [ -n "$_v" ]; then
                    eval "_cur=\${$_k:-}"
                    if [ -z "$_cur" ]; then
                        eval "$_k=\"\$_v\""
                    fi
                fi
                ;;
        esac
    done < "$1"
}

load_deploy_env() {
    _sys_env="${PET_SYS_ENV:-/etc/desktop-pet/env}"
    if [ -n "${PET_DEPLOY_ENV:-}" ] && [ -f "$PET_DEPLOY_ENV" ]; then
        _load_env_file_defaults "$PET_DEPLOY_ENV"
    elif [ -f "$_sys_env" ]; then
        _load_env_file_defaults "$_sys_env"
    elif [ -f "$PET_REPO_ROOT/deploy.env" ]; then
        echo "警告：從舊版 $PET_REPO_ROOT/deploy.env 讀取設定；請改放到 /etc/desktop-pet/env（見 PROPOSAL.md 16.2）" >&2
        _load_env_file_defaults "$PET_REPO_ROOT/deploy.env"
    fi
    PET_USER="${PET_USER:-${SUDO_USER:-$(id -un)}}"
    if [ -z "${PET_HOME:-}" ]; then
        PET_HOME="$(getent passwd "$PET_USER" | cut -d: -f6)"
    fi
    [ -n "$PET_HOME" ] || { echo "找不到使用者 $PET_USER 的家目錄（在 /etc/desktop-pet/env 設 PET_HOME）" >&2; return 1; }
    PET_BACKEND_URL="${PET_BACKEND_URL:-}"
}

# require_sys_env [--warn]：檢查環境檔是否存在；若完全沒有環境檔則中止（--warn 時僅印警告且回傳 0），若僅有舊版 deploy.env 則印警告相容
require_sys_env() {
    _warn_only=0
    if [ "${1:-}" = "--warn" ] || [ "${1:-}" = "warn" ]; then
        _warn_only=1
    fi
    _sys_env="${PET_SYS_ENV:-/etc/desktop-pet/env}"
    if [ -n "${PET_DEPLOY_ENV:-}" ]; then
        if [ -f "$PET_DEPLOY_ENV" ]; then
            return 0
        fi
        if [ "$_warn_only" -eq 1 ]; then
            if [ -f "$_sys_env" ]; then
                echo "警告：PET_DEPLOY_ENV 指定的環境檔不存在：$PET_DEPLOY_ENV（已由 $_sys_env 載入設定）。" >&2
                return 0
            elif [ -f "$PET_REPO_ROOT/deploy.env" ]; then
                echo "警告：PET_DEPLOY_ENV 指定的環境檔不存在：$PET_DEPLOY_ENV（目前暫由舊版 $PET_REPO_ROOT/deploy.env 提供設定）。" >&2
                return 0
            else
                echo "警告：PET_DEPLOY_ENV 指定的環境檔不存在：$PET_DEPLOY_ENV（且無其他環境檔），回退後請盡快補齊設定。" >&2
                return 0
            fi
        fi
        echo "錯誤：PET_DEPLOY_ENV 指定的環境檔不存在：$PET_DEPLOY_ENV" >&2
        return 1
    fi
    if [ -f "$_sys_env" ]; then
        return 0
    fi
    u="${SUDO_USER:-$PET_USER}"
    [ "$u" = "root" ] && u="<使用者>"
    if [ -f "$PET_REPO_ROOT/deploy.env" ]; then
        echo "警告：找不到環境檔 $_sys_env，目前暫由舊版 $PET_REPO_ROOT/deploy.env 提供設定。" >&2
        echo "為確保自動更新（從 release 目錄啟動）上線後能正常運作，請盡快建立 $_sys_env（權限 root:$u 0640）：" >&2
        echo "  sudo mkdir -p \"\$(dirname \"$_sys_env\")\"" >&2
        echo "  sudo cp \"$PET_REPO_ROOT/deploy.env\" \"$_sys_env\"" >&2
        echo "  sudo chown root:\"$u\" \"$_sys_env\"" >&2
        echo "  sudo chmod 640 \"$_sys_env\"" >&2
        return 0
    fi
    if [ "$_warn_only" -eq 1 ]; then
        echo "警告：找不到環境檔 $_sys_env（且無舊版 $PET_REPO_ROOT/deploy.env），回退後請盡快補齊設定。" >&2
        return 0
    fi
    echo "錯誤：找不到環境檔 $_sys_env（且無舊版 $PET_REPO_ROOT/deploy.env）。" >&2
    echo "systemd 服務透過 $_sys_env 載入設定（如 PET_BACKEND_URL），自動更新亦依賴此檔案。" >&2
    echo "請先建立 $_sys_env（權限 root:$u 0640，見 deploy.env.example）：" >&2
    echo "  sudo mkdir -p \"\$(dirname \"$_sys_env\")\"" >&2
    echo "  sudo cp \"$PET_REPO_ROOT/deploy.env.example\" \"$_sys_env\"" >&2
    echo "  sudo chown root:\"$u\" \"$_sys_env\"" >&2
    echo "  sudo chmod 640 \"$_sys_env\"" >&2
    return 1
}

# render_template <模板檔>：把佔位換成環境變數值輸出到 stdout；還有沒換掉的 @XXX@ 就失敗
render_template() {
    out="$(sed -e "s#@USER@#$PET_USER#g" -e "s#@HOME@#$PET_HOME#g" -e "s#@BACKEND_URL@#$PET_BACKEND_URL#g" "$1")" || return 1
    if printf '%s\n' "$out" | grep -q '@[A-Z_]*@'; then
        echo "$1 還有沒設定的佔位：$(printf '%s\n' "$out" | grep -o '@[A-Z_]*@' | sort -u | tr '\n' ' ')" >&2
        return 1
    fi
    printf '%s\n' "$out"
}

# pet_flag_on <值>：1/true/yes/on 視為開（硬體開關 PET_ENABLE_* 用）
pet_flag_on() {
    case "$1" in
        1|true|TRUE|True|yes|YES|Yes|on|ON|On) return 0 ;;
    esac
    return 1
}

# ensure_frontend_venv：建立／檢查 frontend/.venv（systemd unit 的 ExecStart 用這個 venv 的 python）。
# 設計成可離線（Pi 在熱點模式時沒網路）：
#   - venv 用 --system-site-packages：直接用 apt 裝的 python3-pil，不用下載 Pillow
#   - shared/ 用 .pth 檔指到 repo 裡的 shared/（等同 editable，不用 pip、不用下載 setuptools）
#   - 只有 import 檢查失敗時才跑 pip（--no-build-isolation，不另外下載建置工具）；失敗會回傳非 0
ensure_frontend_venv() {
    _fe="$PET_REPO_ROOT/frontend"
    _venv="$_fe/.venv"
    if [ ! -f "$PET_REPO_ROOT/shared/shared/__init__.py" ]; then
        echo "找不到 $PET_REPO_ROOT/shared/（前端需要它；部署時要連同 shared/ 一起放）" >&2
        return 1
    fi
    if [ ! -x "$_venv/bin/python" ]; then
        echo "== 建立 $_venv"
        python3 -m venv --system-site-packages "$_venv" || {
            echo "建立 venv 失敗（Pi 上先 sudo apt install python3-venv）" >&2
            rm -rf "$_venv"
            return 1
        }
    fi
    _site="$("$_venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" || return 1
    printf '%s\n' "$PET_REPO_ROOT/shared" > "$_site/desktop-pet-shared.pth" || return 1
    # requirements.txt 有變（例如新增依賴）就重跑 pip；雜湊記在 venv 裡
    _req_hash="$(sha256sum "$_fe/requirements.txt" | cut -d' ' -f1)"
    _stamp="$_venv/.requirements.sha256"
    if [ "$(cat "$_stamp" 2>/dev/null)" = "$_req_hash" ] \
        && "$_venv/bin/python" -c 'import PIL, shared.apikey, shared.envfile, shared.protocol' 2>/dev/null; then
        echo "== frontend/.venv 已就緒（requirements 沒變；shared 經 .pth 指到 repo）"
        return 0
    fi
    # shared 已經用 .pth 接上，pip 只裝其餘依賴（去掉 -e ../shared，免得離線時要下載 setuptools）；
    # 依賴已由 apt 滿足（python3-pil）時 pip 不需要網路
    _req_tmp="$(mktemp)"
    grep -v -E '^[[:space:]]*-e[[:space:]]+\.\./shared' "$_fe/requirements.txt" > "$_req_tmp"
    echo "== 安裝前端依賴（frontend/requirements.txt）"
    if ! (cd "$_fe" && "$_venv/bin/python" -m pip install --disable-pip-version-check -q --no-build-isolation -r "$_req_tmp"); then
        rm -f "$_req_tmp"
        echo "pip install 失敗（缺的套件需要網路；Pillow 可用 sudo apt install python3-pil 離線滿足）" >&2
        return 1
    fi
    rm -f "$_req_tmp"
    "$_venv/bin/python" -c 'import PIL, shared.apikey, shared.envfile, shared.protocol' || {
        echo "frontend/.venv 無法 import PIL 或 shared" >&2
        return 1
    }
    printf '%s\n' "$_req_hash" > "$_stamp"
}
