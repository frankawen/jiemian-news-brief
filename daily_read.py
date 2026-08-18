#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 daily_read.json

两大来源：
  1) 人民日报 05版「评论」——每天自动抓取（按日期回溯，取最近一个有文章的日期）。
  2) 学习强国「每日一读」——搜索结果走签名网关 + JS 渲染（iframe 动态加载），
     服务端（沙箱 / GitHub Actions）均无法直接抓取标题与简介。因此保留「链接跳转」
     卡片，链接指向用户提供的「每日一读」搜索页；若要展示真实标题+简介，需每日将
     内容粘贴进 xuexi_seed.json 的 paragraphs（或工作台模块内直接粘贴）。
"""
import os
import json
import re
import datetime
import requests
from bs4 import BeautifulSoup

TZ = datetime.timezone(datetime.timedelta(hours=8))
BASE = "https://paper.people.com.cn/rmrb/pc/layout"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://paper.people.com.cn/",
}

# 学习强国「每日一读」搜索页（签名网关 + JS 渲染，服务端无法抓正文/简介，
# 仅保留链接供跳转）。链接为用户提供的「每日一读」搜索结果页。
XUEXI_URL = (
    "https://www.xuexi.cn/dc12897105c8c496d783c5e4d3b680a2/"
    "9a75e290b9cf8cb8fb529a6e503db78d.html"
    "?query=%E6%AF%8F%E6%97%A5%E4%B8%80%E8%AF%BB&page=1&search_source=1"
    "&program_id=0&product_params=%7B%22time_filter%22%3A%22all%22%2C%22type_filter"
    "%22%3A%22all%22%2C%22sort_method%22%3A%22integrated%22%2C%22wenhui_sort_method"
    "%22%3A%22near_far%22%2C%22search_method%22%3A%22all%22%7D&_t=1787019679134"
)


def now_beijing():
    return datetime.datetime.now(TZ)


def fetch_rmrb_comment():
    """回溯最近 8 天，取第一个有文章的 05 版评论。"""
    for back in range(0, 8):
        d = now_beijing() - datetime.timedelta(days=back)
        ym = d.strftime("%Y%m")
        dd = d.strftime("%d")
        url = "%s/%s/%s/node_05.html" % (BASE, ym, dd)
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                continue
            r.encoding = "utf-8"
            soup = BeautifulSoup(r.text, "lxml")
            items = []
            for a in soup.find_all("a", href=re.compile(r"content_\d+\.html")):
                title = a.get_text(strip=True)
                if not title or len(title) < 4:
                    continue
                if title.startswith("本版责编") or title == "图片报道":
                    continue
                href = a.get("href", "")
                m = re.search(r"(content/\d{6}/\d{2}/content_\d+\.html)", href)
                full = ("https://paper.people.com.cn/rmrb/pc/" + m.group(1)) if m else url
                items.append({"title": title, "url": full})
            seen, uniq = set(), []
            for it in items:
                if it["title"] not in seen:
                    seen.add(it["title"])
                    uniq.append(it)
            if uniq:
                return {
                    "title": "人民日报 · 05版 评论",
                    "date": d.strftime("%Y-%m-%d"),
                    "items": uniq,
                }
        except Exception as e:
            print("rmrb fetch err:", e)
            continue
    return {"title": "人民日报 · 05版 评论", "date": "", "items": []}


def fetch_xuexi_link():
    """尝试从学习强国落地页提取标题与链接；失败则回退到已知链接。"""
    try:
        r = requests.get(XUEXI_URL, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            r.encoding = "utf-8"
            soup = BeautifulSoup(r.text, "lxml")
            title = soup.title.get_text(strip=True) if soup.title else ""
            title = re.sub(r"[_\-—|｜]?\s*学习强国.*$", "", title).strip()
            url = XUEXI_URL
            canon = soup.find("link", attrs={"rel": "canonical"})
            if canon and canon.get("href"):
                url = canon.get("href")
            og = soup.find("meta", attrs={"property": "og:url"})
            if og and og.get("content"):
                url = og.get("content")
            if not title:
                title = "学习强国 · 每日一读"
            return {"title": title, "url": url}
    except Exception as e:
        print("xuexi link fetch err:", e)
    return {"title": "学习强国 · 每日一读", "url": XUEXI_URL}


def load_xuexi_seed():
    """学习强国每日一读：服务端无法自动抓正文，至少提取文章链接。

    优先级：仓库 xuexi_seed.json（用户/助手补充）> 在线落地页提取 > 已知链接兜底。
    无论是否有正文，返回的段都带 url，保证模块可点击跳转。
    """
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xuexi_seed.json")
    seed = None
    if os.path.exists(p):
        try:
            seed = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            print("xuexi seed read err:", e)
    live = fetch_xuexi_link()
    if seed and (seed.get("paragraphs") or seed.get("url") or seed.get("title")):
        out = dict(seed)
        out["url"] = seed.get("url") or live.get("url") or XUEXI_URL
        out.setdefault("title", live.get("title", "学习强国 · 每日一读"))
        out.setdefault("date", "")
        out.setdefault("paragraphs", [])
        out["note"] = seed.get("note") or "学习强国文章走签名网关，服务端无法自动抓取正文；已提取文章链接，可点击前往官网阅读。"
        return out
    live.setdefault("date", "")
    live.setdefault("paragraphs", [])
    live["note"] = "学习强国文章走签名网关，服务端无法自动抓取正文；已提取文章链接，可点击前往官网阅读。"
    return live


def main():
    rmrb = fetch_rmrb_comment()
    xuexi = load_xuexi_seed()
    data = {
        "updated_at": now_beijing().isoformat(timespec="seconds"),
        "source": "每日一读（人民日报评论 + 学习强国）",
        "sections": {"xuexi": xuexi, "rmrb": rmrb},
    }
    with open("daily_read.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(
        "daily_read.json written: rmrb=%d xuexi=%d"
        % (len(rmrb.get("items", [])), len(xuexi.get("paragraphs", [])))
    )


if __name__ == "__main__":
    main()
