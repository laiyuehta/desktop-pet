# desktop-pet 部署共用函式（給各 install.sh / start.sh 用 `. tools/deploy/common.sh` 載入）。
# 部署值放在 repo 根目錄的 deploy.env（不進 repo；範本 deploy.env.example），install 腳本只讀不寫。
# 模板裡的佔位：@USER@ 執行使用者、@HOME@ 其家目錄、@BACKEND_URL@ 後端網址。
# 呼叫端要先設 PET_REPO_ROOT（repo 根目錄的絕對路徑）；沒設就用目前目錄。
PET_REPO_ROOT="${PET_REPO_ROOT:-$(pwd)}"

load_deploy_env() {
    f="${PET_DEPLOY_ENV:-$PET_REPO_ROOT/deploy.env}"
    if [ -f "$f" ]; then
        # shellcheck disable=SC1090
        . "$f"
    fi
    PET_USER="${PET_USER:-${SUDO_USER:-$(id -un)}}"
    if [ -z "${PET_HOME:-}" ]; then
        PET_HOME="$(getent passwd "$PET_USER" | cut -d: -f6)"
    fi
    [ -n "$PET_HOME" ] || { echo "找不到使用者 $PET_USER 的家目錄（在 deploy.env 設 PET_HOME）" >&2; return 1; }
    PET_BACKEND_URL="${PET_BACKEND_URL:-}"
}

# render_template <模板檔>：把佔位換成 deploy.env 的值輸出到 stdout；還有沒換掉的 @XXX@ 就失敗
render_template() {
    out="$(sed -e "s#@USER@#$PET_USER#g" -e "s#@HOME@#$PET_HOME#g" -e "s#@BACKEND_URL@#$PET_BACKEND_URL#g" "$1")" || return 1
    if printf '%s\n' "$out" | grep -q '@[A-Z_]*@'; then
        echo "$1 還有沒設定的佔位：$(printf '%s\n' "$out" | grep -o '@[A-Z_]*@' | sort -u | tr '\n' ' ')" >&2
        return 1
    fi
    printf '%s\n' "$out"
}
