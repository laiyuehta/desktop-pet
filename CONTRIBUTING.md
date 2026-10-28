# 參與開發

完整規則在 [PROPOSAL.md「15. 開發流程（git 為主）」](PROPOSAL.md#15-開發流程git-為主)。重點：

1. **先有 Issue**：一個任務或 bug 一個 Issue（用範本）。
2. **一個任務一個分支**，從最新的 main 開：
   `git fetch origin && git switch -c feat/<簡短名稱> origin/main`
   前綴：`feat/` 功能、`fix/` 修正、`spec/` 規格、`anim/` 動畫與美術、`chore/` 整理與工具。
3. **commit 訊息**：`<type>(<scope>): <摘要>`，例如 `fix(wifi): 熱點關閉後沒有重連`。
4. **開 PR**（用範本）：寫對應規格、`Closes #n`、測試、部署影響、回退方式。
   CI 要綠燈；由使用者核准並用 **squash merge** 合併。不要直接 push 到 main。
5. **規格**先走 `spec/` PR 給使用者核准，實作 PR 再引用那個規格版本或 PR。
6. **不要 force push、不要改寫歷史**。分支推上去以後，要跟上 main 就 merge main 進來，不要 rebase。
7. **負責範圍**看 `.github/CODEOWNERS`；改到別人負責的路徑要在 PR 標出來，請負責的人確認。
8. **不要放進 repo**：密碼、token、正式網址、區網 IP、Wi‑Fi 名稱、Windows 使用者路徑、照片、`deploy.env`、`backend/config.json`。CI 會掃。

本機測試：`python -m pip install -r requirements.txt pytest && python -m pytest -q`（遙控頁的 script 測試需要 node）。
