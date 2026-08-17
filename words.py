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


def fetch_one(cat):
    try:
        r = requests.get(HITOKOTO, params={"c": cat, "encode": "json"}, timeout=15)
        if r.status_code != 200:
            return None
        d = r.json()
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
    except Exception as e:
        print("hitokoto err:", e)
        return None


def main():
    words, seen = [], set()
    for cat in CATS:
        for _ in range(2):
            w = fetch_one(cat)
            if w and w["text"] not in seen:
                seen.add(w["text"])
                words.append(w)
            time.sleep(0.3)
    data = {
        "updated_at": now_beijing().isoformat(timespec="seconds"),
        "source": "hitokoto(一言) 公开接口",
        "words": words,
    }
    with open("words.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("words.json written: %d" % len(words))


if __name__ == "__main__":
    main()
