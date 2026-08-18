#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 words.json

用头条搜索(so.toutiao.com)「暖心祝福」关键词每日 enrich 词语库。
每条为 {cat, src, text, tags}，工作台「词语储备」刷新时会去重并入库，不断丰富。

抓取目标（SSR 静态页，无需登录）:
  https://so.toutiao.com/search?dvpf=pc&source=input&keyword=暖心祝福
卡片结构: cs-card-content 内
  标题   -> data-log-click pos=title 后第一个 <a>
  摘要   -> class 含 text-default text-m text-regular 的 <span>
  来源   -> cs-source-content 里第一个 text-ellipsis <span>
"""
import os
import re
import json
import time
import datetime
import hashlib
import html as ihtml

import requests

TZ = datetime.timezone(datetime.timedelta(hours=8))

# 头条搜索：主关键词 + 按星期轮换的变体（保证每日有新内容可累积）
TOUTIAO_SEARCH = "https://so.toutiao.com/search?dvpf=pc&source=input&keyword="
KEYWORD_MAIN = "暖心祝福"
KEYWORD_ROTATE = [
    "暖心祝福",
    "暖心祝福语",
    "早安暖心祝福语",
    "暖心问候祝福语",
    "暖心祝福句子",
    "美好祝福语",
    "温暖祝福问候",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.toutiao.com/",
}

# hitokoto 应急兜底（头条连续失败时保证仍有产出）
HITOKOTO = "https://v1.hitokoto.cn/"
CAT_NAME = {"d": "诗词", "i": "文学", "k": "哲学", "j": "台词", "a": "动漫", "l": "网络", "f": "游戏"}


def now_beijing():
    return datetime.datetime.now(TZ)


def http_get(url, params=None, timeout=20):
    """三级回退：PREFETCH_DIR 本地预抓 -> curl 子进程 -> requests。

    沙箱里 Python TLS 对部分国内站会被掐，PREFETCH_DIR 允许用 Bash 工具
    curl 预抓页面后离线解析；GitHub Actions 上走 curl/requests 直连。
    """
    import subprocess
    from urllib.parse import urlencode, quote

    u = url
    if params:
        u = url + ("?" + urlencode(params, quote_via=quote))

    # 1) 本地预抓文件
    ck = hashlib.md5(u.encode("utf-8")).hexdigest()
    pf_dir = os.environ.get("PREFETCH_DIR")
    if pf_dir:
        pf = os.path.join(pf_dir, ck + ".html")
        if os.path.exists(pf):
            return open(pf, encoding="utf-8", errors="ignore").read()

    # 2) curl 子进程
    try:
        cmd = ["curl", "-sL", "--max-time", str(timeout), "-A", HEADERS["User-Agent"],
               "-H", "Accept-Language: zh-CN,zh;q=0.9"]
        if u.startswith(TOUTIAO_SEARCH):
            cmd += ["-H", "Referer: https://www.toutiao.com/"]
        cmd += [u]
        out = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
        if out.returncode == 0 and out.stdout:
            html = out.stdout.decode("utf-8", errors="ignore")
            if len(html) > 5000 and "cs-card-content" in html:
                if pf_dir:
                    os.makedirs(pf_dir, exist_ok=True)
                    open(os.path.join(pf_dir, ck + ".html"), "w", encoding="utf-8").write(html)
                return html
    except Exception as e:
        print("curl err:", e)

    # 3) requests
    try:
        r = requests.get(u, headers=HEADERS, timeout=timeout)
        r.encoding = "utf-8"
        if r.status_code == 200 and len(r.text) > 5000 and "cs-card-content" in r.text:
            return r.text
    except Exception as e:
        print("requests err:", e)
    return ""


def _strip_tags(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return ihtml.unescape(s).strip()


def parse_toutiao(doc):
    """解析头条搜索 SSR 页 -> list[{cat,src,text,tags}]（只收有正文摘要的卡片）。"""
    out = []
    cards = doc.split("cs-card-content")[1:]
    for c in cards:
        tm = re.search(
            r'pos&quot;:&quot;title&quot;.*?<a[^>]*>(.*?)</a>', c, re.S)
        sm = re.search(
            r'text-default\s+text-m\s+text-regular[^>]*>\s*<span[^>]*>(.*?)</span>',
            c, re.S)
        srcm = re.search(
            r'cs-source-content.*?<span class="text-ellipsis">([^<]*)</span>', c, re.S)
        title = _strip_tags(tm.group(1) if tm else "")
        text = _strip_tags(sm.group(1) if sm else "")
        src = _strip_tags(srcm.group(1) if srcm else "")
        if not text or len(text) < 20:      # 无摘要的是视频/相关搜索卡，跳过
            continue
        # 丢弃半句话：以省略号/逗号/分号等截断的，截到最后一个完整句
        if not re.search(r"[。！？!?…]$", text):
            m = re.search(r"^(.*[。！？!?…])", text)
            text = (m.group(1) if m else "").strip()
            if len(text) < 20:
                continue
        # 来源若抓到的是日期（如"6天前"/"1月29日"），置为通用
        if re.search(r"天前|周前|月|日|次观看|观看", src):
            src = ""
        src = src or "toutiao"
        out.append({
            "cat": "暖心祝福",
            "src": ("头条 · " + src)[:40],
            "text": text[:200],
            "tags": title[:60],
        })
    return out


def fetch_toutiao(keyword):
    doc = http_get(TOUTIAO_SEARCH + keyword)
    if not doc:
        return []
    items = parse_toutiao(doc)
    print("toutiao[%s]: %d 条" % (keyword, len(items)))
    return items


def fetch_hitokoto(cat):
    """应急兜底：头条失败时从 hitokoto 拿一条，保证每日仍有产出。"""
    try:
        r = requests.get(HITOKOTO, params={"c": cat, "encode": "json"}, timeout=15)
        d = r.json()
        text = (d.get("hitokoto") or "").strip()
        if not text:
            return None
        return {
            "cat": CAT_NAME.get(cat, "金句"),
            "src": ("hitokoto · " + (d.get("from") or "未知"))[:40],
            "text": text,
            "tags": (d.get("from_who") or "").strip(),
        }
    except Exception as e:
        print("hitokoto err:", e)
        return None


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

    # 1) 头条搜索「暖心祝福」（主关键词 + 按星期轮换变体）
    weekday = now_beijing().weekday()
    keywords = [KEYWORD_MAIN, KEYWORD_ROTATE[weekday]]
    if KEYWORD_ROTATE[weekday] == KEYWORD_MAIN:
        keywords = [KEYWORD_MAIN]
    added = 0
    for kw in keywords:
        for it in fetch_toutiao(kw):
            if it["text"] not in seen:
                seen.add(it["text"])
                words.append(it)
                added += 1
        time.sleep(0.5)

    # 2) 应急兜底：头条一无所获时用 hitokoto 补 2 条
    if added == 0:
        print("toutiao 无新增，hitokoto 兜底")
        for cat in ["d", "i", "j", "k"]:
            w = fetch_hitokoto(cat)
            if w and w["text"] not in seen:
                seen.add(w["text"])
                words.append(w)
            time.sleep(0.3)

    words = words[:120]
    data = {
        "updated_at": now_beijing().isoformat(timespec="seconds"),
        "source": "头条搜索「暖心祝福」（累积去重）",
        "words": words,
    }
    with open("words.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("words.json written: %d (新增 %d, 累计 %d)"
          % (len(words), added, len(existing)))


if __name__ == "__main__":
    main()
