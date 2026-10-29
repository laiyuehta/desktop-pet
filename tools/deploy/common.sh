# desktop-pet 部署共用函式（給各 install.sh / start.sh 用 `. tools/deploy/common.sh` 載入）。
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

# render_template <模板檔>：把佔位換成環境變數值輸出到 stdout；還有沒換掉的 @XXX@ 就失敗
render_template() {
    out="$(sed -e "s#@USER@#$PET_USER#g" -e "s#@HOME@#$PET_HOME#g" -e "s#@BACKEND_URL@#$PET_BACKEND_URL#g" "$1")" || return 1
    if printf '%s\n' "$out" | grep -q '@[A-Z_]*@'; then
        echo "$1 還有沒設定的佔位：$(printf '%s\n' "$out" | grep -o '@[A-Z_]*@' | sort -u | tr '\n' ' ')" >&2
        return 1
    fi
    printf '%s\n' "$out"
}
