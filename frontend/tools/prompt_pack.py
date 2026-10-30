#!/usr/bin/env python3
"""動畫提示詞組合包工具（prompts/anim/）：組提示詞 → 叫本機 LLM 抽 N 個變體 → 逐一驗證／烘焙預覽。

只用標準函式庫 + 既有相依（Pillow，烘焙時才用）。Windows Python 3.12 可跑。

    python tools/prompt_pack.py build  --task new_emotion_level --var STATE=happy_4 ... -o prompt.md
    python tools/prompt_pack.py gen    --task tweak_expression --var STATE=sad_2 --var IDEA=... -n 8 -o out/
    python tools/prompt_pack.py validate out/v01.json [--bake] [--out-dir out/v01_pack]
    python tools/prompt_pack.py validate-all out/ --bake

LLM 輸出格式（patch JSON）見 prompts/anim/system.md。驗證只寫到暫存／指定資料夾，**不會改 repo 的設定檔**。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
import urllib.request
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # frontend/
REPO = os.path.dirname(ROOT)
PACK_DIR = os.path.join(REPO, "prompts", "anim")
SKELETON = os.path.join(ROOT, "assets", "skeletons", "seed.json")
STATES = os.path.join(ROOT, "config", "states.json")
BAKE = os.path.join(ROOT, "config", "bake.json")

CHANNELS = ("x", "y", "rot", "sx", "sy")
EASES = ("linear", "ease", "step")
EYES = ("normal", "happy", "bored", "sleep", "squint", "surprised", "sleepy", "sad")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# 合理範圍（避免飛出畫面 800x480、零件翻面）
LIMITS = {"x": (-160, 160), "y": (-140, 40), "rot": (-400, 400), "sx": (0.6, 1.4), "sy": (0.6, 1.4)}
PATCH_KEYS = {"animations", "states", "bake_states", "transitions", "notes"}


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- 組提示詞
def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def fill(template: str, var: Dict[str, str]) -> Tuple[str, List[str]]:
    """把 {{KEY}} 換成值，回傳 (文字, 沒填的 KEY)。"""
    out = template
    for k, v in var.items():
        out = out.replace("{{" + k + "}}", v)
    return out, sorted(set(re.findall(r"\{\{([A-Z_0-9]+)\}\}", out)))


def build_prompt(task: str, var: Dict[str, str], pack_dir: str = PACK_DIR) -> Tuple[str, str, List[str]]:
    """回傳 (system, user, 沒填的變數)。task 可以是 tasks/ 裡的名稱或檔案路徑。"""
    tpath = task if os.path.isfile(task) else os.path.join(pack_dir, "tasks", task + ".md")
    system = read_text(os.path.join(pack_dir, "system.md"))
    context = read_text(os.path.join(pack_dir, "context.md"))
    body, missing = fill(read_text(tpath), var)
    return system, context + "\n\n---\n\n" + body, missing


# --------------------------------------------------------------------------- 呼叫 LLM
def chat(base_url: str, model: str, api_key: str, system: str, user: str,
         temperature: float = 0.9, seed: Optional[int] = None, timeout: float = 600) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {"model": model, "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if seed is not None:
        body["seed"] = seed
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + (api_key or "none")})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def extract_json(text: str) -> dict:
    """從 LLM 回覆取出 patch JSON：去掉 <think>…</think>、```json 區塊，取最外層 { }。"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.S)
    if m:
        text = m.group(1)
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j < i:
        raise ValueError("回覆裡找不到 JSON 物件")
    return json.loads(text[i:j + 1])


# --------------------------------------------------------------------------- 驗證
def _close(a: float, b: float, eps: float = 1e-6) -> bool:
    return abs(a - b) <= eps


def check_animation(name: str, a: dict, bones: List[str]) -> List[str]:
    err = []
    if not isinstance(a, dict):
        return [f"動畫 {name} 不是物件"]
    dur = a.get("duration")
    if not isinstance(dur, (int, float)) or not (0.5 <= dur <= 12):
        return [f"動畫 {name}: duration 要是 0.5~12 秒的數字"]
    if not _close(round(dur / 0.05) * 0.05, dur):
        err.append(f"動畫 {name}: duration {dur} 不是 0.05 的倍數")
    loop = a.get("loop", True)
    for k in ("fade_in", "fade_out"):
        if k in a and not (isinstance(a[k], (int, float)) and 0 <= a[k] <= 2):
            err.append(f"動畫 {name}: {k} 要是 0~2 秒")
    tracks = a.get("tracks")
    if not isinstance(tracks, dict) or not tracks:
        return err + [f"動畫 {name}: 沒有 tracks"]
    for bone, chans in tracks.items():
        if bone not in bones:
            err.append(f"動畫 {name}: 骨頭 {bone} 不存在（只能用 {', '.join(bones)}）")
            continue
        for ch, keys in (chans or {}).items():
            where = f"動畫 {name}.{bone}.{ch}"
            if ch not in CHANNELS:
                err.append(f"{where}: 未知通道（只能用 {', '.join(CHANNELS)}）")
                continue
            if not isinstance(keys, list) or len(keys) < 2:
                err.append(f"{where}: 至少要 2 個關鍵幀")
                continue
            prev = -1.0
            lo, hi = LIMITS[ch]
            for k in keys:
                if (not isinstance(k, list) or len(k) not in (2, 3)
                        or not all(isinstance(x, (int, float)) for x in k[:2])):
                    err.append(f"{where}: 關鍵幀格式要是 [秒, 數值] 或 [秒, 數值, 緩動]：{k}")
                    break
                t, v = k[0], k[1]
                if len(k) == 3 and k[2] not in EASES:
                    err.append(f"{where}: 緩動 {k[2]} 只能是 {'/'.join(EASES)}")
                if t <= prev:
                    err.append(f"{where}: 時間要嚴格遞增（{t}）")
                if not _close(round(t / 0.05) * 0.05, t):
                    err.append(f"{where}: 時間 {t} 不是 0.05 的倍數")
                if not (lo <= v <= hi):
                    err.append(f"{where}: 數值 {v} 超出範圍 {lo}~{hi}")
                prev = t
            else:
                if not _close(keys[0][0], 0):
                    err.append(f"{where}: 第一格時間要是 0")
                if not _close(keys[-1][0], dur):
                    err.append(f"{where}: 最後一格時間要等於 duration {dur}")
                if loop:
                    a0, a1 = keys[0][1], keys[-1][1]
                    ok = _close(a0, a1, 1e-3) or (ch == "rot" and _close((a1 - a0) % 360, 0, 1e-3))
                    if not ok:
                        err.append(f"{where}: 循環動畫頭尾值要相同（{a0} vs {a1}；rot 可差 360 的倍數）")
    return err


def check_patch(patch: dict, sk: dict, sc: dict, bc: dict) -> List[str]:
    """檢查 patch 本身與合併後的一致性，回傳錯誤清單（空 = 通過）。"""
    if not isinstance(patch, dict):
        return ["patch 不是 JSON 物件"]
    err = [f"未知的頂層欄位 {k}（只能用 {', '.join(sorted(PATCH_KEYS))}）" for k in patch if k not in PATCH_KEYS]
    bones = [b["name"] for b in sk["bones"]]
    anims = patch.get("animations", {}) or {}
    states = patch.get("states", {}) or {}
    bstates = patch.get("bake_states", {}) or {}
    fps = float(bc.get("fps", 20))
    for name, a in anims.items():
        if not NAME_RE.match(name):
            err.append(f"動畫名稱 {name} 只能用小寫英數與底線")
        err += check_animation(name, a, bones)
    all_anims = dict(sk["animations"], **anims)
    all_states = dict(sc["states"], **states)
    for name, st in states.items():
        if not NAME_RE.match(name):
            err.append(f"狀態名稱 {name} 只能用小寫英數與底線")
        if not isinstance(st, dict):
            err.append(f"狀態 {name} 不是物件")
            continue
        an = st.get("animation", name)
        if an not in all_anims:
            err.append(f"狀態 {name}: 動畫 {an} 不存在")
        if st.get("eyes", "normal") not in EYES:
            err.append(f"狀態 {name}: eyes 只能是 {', '.join(EYES)}")
        for e in st.get("effects", []):
            if e != "zzz":
                err.append(f"狀態 {name}: 特效只有 zzz")
        if not isinstance(st.get("label", ""), str) or not st.get("label"):
            err.append(f"狀態 {name}: 要有中文 label")
        for tm in st.get("timers", []):
            if not isinstance(tm.get("after"), (int, float)) or not isinstance(tm.get("event"), str):
                err.append(f"狀態 {name}: timer 要有 after（秒）與 event")
            elif tm["event"] == f"{name}_done" and an in all_anims:
                dur = float(all_anims[an]["duration"])
                r = tm["after"] % dur
                if not (dur - 0.2 < r < dur):
                    err.append(f"狀態 {name}: 計時 after 要是 動畫長度×輪數−0.1（目前 {tm['after']}，長度 {dur}）")
    for name, cfg in bstates.items():
        if name not in all_states:
            err.append(f"bake_states.{name}: 沒有這個狀態")
            continue
        an = all_states[name].get("animation", name)
        if an not in all_anims:
            continue
        dur = float(all_anims[an]["duration"])
        n = int(round(dur * fps))
        ce = cfg.get("cut_every")
        if ce is not None and (not isinstance(ce, int) or ce < 1 or ce > n):
            err.append(f"bake_states.{name}: cut_every 要是 1~{n} 的整數")
        vs = cfg.get("variants", [])
        if not isinstance(vs, list) or not vs:
            err.append(f"bake_states.{name}: 至少一個 variant")
            continue
        seen = set()
        for v in vs:
            vn = v.get("name") if isinstance(v, dict) else None
            if not vn or not NAME_RE.match(vn) or vn in seen:
                err.append(f"bake_states.{name}: variant 名稱缺少、重複或格式錯 {vn}")
                continue
            seen.add(vn)
            if not isinstance(v.get("weight", 1), int) or v.get("weight", 1) < 1:
                err.append(f"bake_states.{name}.{vn}: weight 要是 ≥1 的整數")
            for t in v.get("blinks", []):
                if not (isinstance(t, (int, float)) and 0 <= t < dur):
                    err.append(f"bake_states.{name}.{vn}: blink 時間 {t} 要在 0~{dur}")
            for lk in v.get("looks", []):
                if (not isinstance(lk, list) or len(lk) != 3 or not (0 <= lk[0] < dur)
                        or not all(-1 <= x <= 1 for x in lk[1:])):
                    err.append(f"bake_states.{name}.{vn}: look 要是 [秒(<{dur}), x(-1~1), y(-1~1)]")
        if not any(isinstance(v, dict) and v.get("name") == "base" for v in vs):
            err.append(f"bake_states.{name}: 要有一個 name=base 的 variant")
    for tr in patch.get("transitions", []) or []:
        if not isinstance(tr, dict) or set(tr) - {"from", "event", "to"} or not all(k in tr for k in ("from", "event", "to")):
            err.append(f"transition 格式要是 {{from, event, to}}：{tr}")
            continue
        if tr["from"] != "*" and tr["from"] not in all_states:
            err.append(f"transition from {tr['from']} 不存在")
        if tr["to"] not in all_states:
            err.append(f"transition to {tr['to']} 不存在")
    for name in states:
        if name not in sc["states"] and not any(t.get("from") == name for t in sc["transitions"] + (patch.get("transitions") or [])):
            err.append(f"新狀態 {name} 沒有任何離開的 transition（通常要 {name} --{name}_done--> idle）")
    return err


def merge(patch: dict, sk: dict, sc: dict, bc: dict) -> Tuple[dict, dict, dict]:
    sk, sc, bc = copy.deepcopy(sk), copy.deepcopy(sc), copy.deepcopy(bc)
    sk["animations"].update(patch.get("animations", {}) or {})
    sc["states"].update(patch.get("states", {}) or {})
    bc.setdefault("states", {}).update(patch.get("bake_states", {}) or {})
    for tr in patch.get("transitions", []) or []:
        sc["transitions"] = [t for t in sc["transitions"] if not (t["from"] == tr["from"] and t["event"] == tr["event"])]
        sc["transitions"].append(tr)
    return sk, sc, bc


def touched_states(patch: dict, sc: dict) -> List[str]:
    names = set(patch.get("states", {}) or {}) | set(patch.get("bake_states", {}) or {})
    anims = set(patch.get("animations", {}) or {})
    for n, st in dict(sc["states"], **(patch.get("states") or {})).items():
        if st.get("animation", n) in anims:
            names.add(n)
    for tr in patch.get("transitions", []) or []:
        names |= {tr.get("from"), tr.get("to")} - {"*", None}
    return sorted(names)


def write_merged(patch: dict, work: str) -> Tuple[str, str, str]:
    """把合併後的骨架／狀態／烘焙設定寫到 work/（零件圖改成絕對路徑），回傳三個路徑。"""
    sk, sc, bc = merge(patch, _load(SKELETON), _load(STATES), _load(BAKE))
    skdir = os.path.dirname(SKELETON)
    for b in sk["bones"]:
        if b.get("part"):
            b["part"]["image"] = os.path.normpath(os.path.join(skdir, b["part"]["image"]))
    os.makedirs(work, exist_ok=True)
    paths = [os.path.join(work, n) for n in ("skeleton.json", "states.json", "bake.json")]
    bc["skeleton"] = os.path.abspath(paths[0])
    for p, d in zip(paths, (sk, sc, bc)):
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
    return paths[0], paths[1], paths[2]


def validate(patch: dict, out_dir: Optional[str] = None, do_bake: bool = False, fps: float = 10,
             log=print) -> List[str]:
    err = check_patch(patch, _load(SKELETON), _load(STATES), _load(BAKE))
    if err:
        return err
    work = out_dir or tempfile.mkdtemp(prefix="petvariant_")
    _, st_p, bk_p = write_merged(patch, os.path.join(work, "config"))
    # 用合併後設定跑一次狀態機 + 骨架載入（不需要螢幕）
    sys.path.insert(0, ROOT)
    from pet.skeleton import Skeleton
    from pet.state_machine import StateMachine
    try:
        sk = Skeleton.load(os.path.join(work, "config", "skeleton.json"), load_images=False)
        sc = _load(st_p)
        for name in touched_states(patch, sc):
            fsm = StateMachine(sc)
            fsm.force(name)
            sk.animations[sc["states"][name].get("animation", name)].sample(0.0)
    except Exception as e:  # noqa: BLE001
        return [f"載入／狀態機失敗：{e!r}"]
    if do_bake:
        from pet.baker import bake
        only = sorted(set(["idle"] + touched_states(patch, sc)))
        try:
            bake(os.path.join(work, "pack"), bake_cfg=bk_p, states_cfg=st_p, only_states=only, fps=fps,
                 transitions=False, expressions=[], preview=True, log=lambda *_: None)
        except Exception as e:  # noqa: BLE001
            return [f"烘焙失敗：{e!r}"]
        log(f"  預覽：{os.path.join(work, 'pack', 'preview')}")
    return []


# --------------------------------------------------------------------------- CLI
def _vars(items: List[str]) -> Dict[str, str]:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        if v.startswith("@anim:"):
            names = [n for n in v[6:].split(",") if n]
            anims = _load(SKELETON)["animations"]
            v = json.dumps({n: anims[n] for n in names}, ensure_ascii=False, separators=(",", ":"))
        elif v.startswith("@") and os.path.isfile(v[1:]):
            v = read_text(v[1:])
        out[k.strip()] = v
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="桌寵動畫提示詞組合包")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("build", "gen"):
        p = sub.add_parser(c)
        p.add_argument("--task", required=True, help="tasks/ 裡的名稱（不含 .md）或檔案路徑")
        p.add_argument("--var", action="append", default=[], help="KEY=值，或 KEY=@檔案；可重複")
        p.add_argument("-o", "--out", required=True, help="build：輸出提示詞檔；gen：輸出資料夾")
    g = sub.choices["gen"]
    g.add_argument("-n", type=int, default=4, help="抽幾個變體")
    g.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:1234/v1"))
    g.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "local-model"))
    g.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    g.add_argument("--temperature", type=float, default=0.9)
    g.add_argument("--bake", action="store_true", help="每個變體都烘焙預覽 GIF")
    for c in ("validate", "validate-all"):
        p = sub.add_parser(c)
        p.add_argument("path", help="patch JSON（validate）或資料夾（validate-all）")
        p.add_argument("--bake", action="store_true", help="烘焙小動畫包並產生 preview/*.gif")
        p.add_argument("--out-dir", default=None, help="validate：烘焙輸出資料夾（預設暫存）")
    a = ap.parse_args(argv)

    if a.cmd in ("build", "gen"):
        system, user, missing = build_prompt(a.task, _vars(a.var))
        if a.cmd == "gen":
            missing = [m for m in missing if m != "SEED_HINT"]   # gen 會每次自動填
        if missing:
            print(f"!! 還沒填的變數：{', '.join(missing)}（用 --var KEY=值）", file=sys.stderr)
            return 2
        if a.cmd == "build":
            with open(a.out, "w", encoding="utf-8") as fh:
                fh.write("# SYSTEM\n\n" + system + "\n\n# USER\n\n" + user)
            print(f"已寫入 {a.out}（約 {len(system + user) // 3} tokens 以內）")
            return 0
        os.makedirs(a.out, exist_ok=True)
        ok = 0
        for i in range(1, a.n + 1):
            tag = f"v{i:02d}"
            try:
                u = user.replace("{{SEED_HINT}}", f"#{i}")
                raw = chat(a.base_url, a.model, a.api_key, system, u, a.temperature, seed=i)
            except Exception as e:  # noqa: BLE001
                print(f"{tag}: 呼叫失敗 {e!r}")
                continue
            with open(os.path.join(a.out, tag + ".raw.txt"), "w", encoding="utf-8") as fh:
                fh.write(raw)
            try:
                patch = extract_json(raw)
            except Exception as e:  # noqa: BLE001
                print(f"{tag}: JSON 解析失敗 {e}")
                continue
            with open(os.path.join(a.out, tag + ".json"), "w", encoding="utf-8") as fh:
                json.dump(patch, fh, ensure_ascii=False, indent=1)
            err = validate(patch, os.path.join(a.out, tag) if a.bake else None, a.bake)
            print(f"{tag}: " + ("OK" if not err else "不通過\n    " + "\n    ".join(err)))
            ok += not err
        print(f"完成：{ok}/{a.n} 個變體通過 → {a.out}")
        return 0 if ok else 1

    files = ([a.path] if a.cmd == "validate" else
             sorted(os.path.join(a.path, f) for f in os.listdir(a.path) if f.endswith(".json")))
    bad = 0
    for f in files:
        try:
            patch = _load(f)
        except Exception as e:  # noqa: BLE001
            print(f"{f}: 讀不到 JSON {e}")
            bad += 1
            continue
        out = a.out_dir if a.cmd == "validate" else (os.path.splitext(f)[0] if a.bake else None)
        err = validate(patch, out, a.bake)
        print(f"{f}: " + ("OK" if not err else "不通過\n    " + "\n    ".join(err)))
        bad += bool(err)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
