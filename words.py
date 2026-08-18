#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 words.json

用 hitokoto(一言) 等无反爬公开接口每日 enrich 词语库。
每条为 {cat, src, text, tags}，工作台「词语储备」刷新时会去重并入库，不断丰富。
"""
import os
import json
import time
import datetime
import requests

TZ = datetime.timezone(datetime.timedelta(hours=8))
HITOKOTO = "https://v1.hitokoto.cn/"
# 类别：d诗词 i文学 k哲学 j台词 a动漫 l网络 f游戏
CATS = ["d", "i", "k", "j", "a", "l", "f"]
CAT_NAME = {
    "d": "诗词",
    "i": "文学",
    "k": "哲学",
    "j": "台词",
    "a": "动漫",
    "l": "网络",
    "f": "游戏",
}


def now_beijing():
    return datetime.datetime.now(TZ)


def http_get_json(url, params=None):
    """优先用 requests；沙箱里 requests 对部分国内站 TLS 握手会失败，
    退化到 curl 子进程（本地 / GitHub Actions 均可兜底）。"""
    # 1) requests
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200 and r.text.strip():
            return r.json()
    except Exception as e:
        print("requests err:", e)
    # 2) curl 兜底
    try:
        import subprocess
        from urllib.parse import urlencode

        u = url
        if params:
            u = url + ("?" + urlencode(params))
        out = subprocess.run(
            ["curl", "-sS", "-m", "15", "-A", "Mozilla/5.0", u],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout)
    except Exception as e:
        print("curl err:", e)
    return None


def fetch_one(cat):
    d = http_get_json(HITOKOTO, {"c": cat, "encode": "json"})
    if not d:
        return None
    text = (d.get("hitokoto") or "").strip()
    if not text:
        return None
    src = (d.get("from") or "").strip()
    who = (d.get("from_who") or "").strip()
    return {
        "cat": CAT_NAME.get(cat, "金句"),
        "src": ("hitokoto · " + (src or "未知"))[:40],
        "text": text,
        "tags": who,
    }


def main():
    # 读取历史，累积去重，让「词语储备」随天数增长（上限 120 条）
    existing, seen = [], set()
    try:
        with open("words.json", encoding="utf-8") as f:
            existing = json.load(f).get("words", [])
    except Exception:
        pass
    for w in existing:
        seen.add(w.get("text", ""))
    words = list(existing)

    for cat in CATS:
        for _ in range(2):
            w = fetch_one(cat)
            if w and w["text"] not in seen:
                seen.add(w["text"])
                words.append(w)
            time.sleep(0.3)

    words = words[:120]
    data = {
        "updated_at": now_beijing().isoformat(timespec="seconds"),
        "source": "hitokoto(一言) 公开接口（累积去重）",
        "words": words,
    }
    with open("words.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("words.json written: %d (累计 %d)" % (len(words), len(existing)))


if __name__ == "__main__":
    main()
