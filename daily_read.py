#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 daily_read.json

两大来源：
  1) 人民日报 05版「评论」——每天自动抓取（按日期回溯，取最近一个有文章的日期）。
  2) 学习强国「每日一读」——通过 search.xuexi.cn/api/search 抓取（PC 端 jQuery ajax
     完整协议：size=15 / client_version=PC:0.0.10 / program_id=1 / product=wenhui_search，
     字段位于 cardSchema.renderData.{title,summary,url,publish_time} + ext_infos[0].title）。
     沙箱/无登录态时仅能拿到「习近平文汇」系列，「每日一读」频道需要登录态浏览器才能搜到。
     降级策略：API 抓不到「每日一读」时，读仓库 xuexi_seed.json（悟空手工维护的 5 篇）。
"""
import os
import json
import re
import datetime
import urllib.parse
import urllib.request
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


def fetch_xuexi_search():
    """调 learning强国 wenhui 搜索 API 拿「每日一读」前 5 条。

    完整 PC 端 jQuery ajax 协议（来自 static.xuexi.cn/search/online 的 JS bundle）：
      endpoint: https://search.xuexi.cn/api/search
      query (必填): query, page=1, size=15, hid=<32位>, client_version=PC:0.0.10,
                    search_source=2, program_id=1, product=wenhui_search,
                    product_params=<URL-encoded JSON {"time_filter":"all", ...}>,
                    _t=<毫秒时间戳>
    响应字段路径: data.list[i].cardSchema.renderData.{title, summary, url, publish_time}
                  + data.list[i].cardSchema.renderData.ext_infos[0].title (频道标签)

    Returns: list[dict] 每条 {title, summary, url, date, channel}，至多 5 条；失败返回 []
    """
    import time as _t
    import random as _r
    import string as _s

    hid = "".join(_r.choices(_s.ascii_letters + _s.digits, k=32))
    pp = json.dumps(
        {
            "time_filter": "all",
            "type_filter": "all",
            "sort_method": "integrated",
            "wenhui_sort_method": "near_far",
            "search_method": "all",
        },
        ensure_ascii=False,
    )
    pp_enc = urllib.parse.quote(pp)
    q = urllib.parse.quote("每日一读")

    base = {
        "query": q,
        "page": "1",
        "size": "15",
        "hid": hid,
        "client_version": "PC:0.0.10",
        "search_source": "2",
        "program_id": "1",
        "product": "wenhui_search",
        "product_params": pp_enc,
        "_t": str(int(_t.time() * 1000)),
    }
    url = "https://search.xuexi.cn/api/search?" + urllib.parse.urlencode(base)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Referer": "https://www.xuexi.cn/",
        "Origin": "https://www.xuexi.cn",
        "Accept": "application/json, text/plain, */*",
    }
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode("utf-8", errors="ignore")
            d = json.loads(body)
            if d.get("status") != 0:
                continue
            lst = (d.get("data") or {}).get("list") or []
            out = []
            for it in lst:
                cs = (it.get("cardSchema") or {})
                rd = (cs.get("renderData") or {})
                ext_list = rd.get("ext_infos") or []
                ext = ext_list[0] if ext_list else {}
                title = (rd.get("title") or "").strip()
                summary = (rd.get("summary") or "").strip()
                u = (rd.get("url") or "").strip()
                # url 字段里 &amp; 是 HTML 实体
                u = u.replace("&amp;", "&")
                pub = (rd.get("publish_time") or "").strip()
                channel = (ext.get("title") or "").strip() if ext else ""
                if not title and not summary:
                    # 服务端对未登录只返回 total 不返回内容
                    continue
                out.append(
                    {
                        "title": title,
                        "summary": summary,
                        "url": u,
                        "date": pub,
                        "channel": channel,
                    }
                )
                if len(out) >= 5:
                    break
            return out
        except Exception as e:
            print("xuexi search err (try %d):" % (attempt + 1), e)
            _t.sleep(1.5 * (attempt + 1))
    return []


def load_xuexi_seed():
    """学习强国每日一读：优先调官方 search API，失败则降级到仓库 xuexi_seed.json。

    优先级:
      1) search.xuexi.cn API（PC 端 jQuery ajax 协议）
      2) xuexi_seed.json 的 items 数组（悟空手工维护的 5 篇真实内容）
      3) 已知搜索页 URL（仅保留跳转，无内容）
    """
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xuexi_seed.json")
    seed = None
    if os.path.exists(p):
        try:
            seed = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            print("xuexi seed read err:", e)

    # 1) 调官方 search API
    try:
        items = fetch_xuexi_search()
        if items:
            return {
                "title": "学习强国 · 每日一读",
                "date": now_beijing().strftime("%Y-%m-%d"),
                "url": XUEXI_URL,
                "items": items,
                "source": "learning强国 wenhui 搜索 API（search.xuexi.cn）",
                "note": "通过 PC 端 jQuery ajax 完整参数（size=15 + client_version=PC:0.0.10 + product=wenhui_search）实时抓取。",
            }
    except Exception as e:
        print("xuexi api err:", e)

    # 2) seed.json 兜底
    if seed and seed.get("items"):
        items = seed["items"]
        return {
            "title": seed.get("title", "学习强国 · 每日一读"),
            "date": seed.get("date") or now_beijing().strftime("%Y-%m-%d"),
            "url": seed.get("url") or XUEXI_URL,
            "items": items,
            "source": "xuexi_seed.json (悟空手工维护)",
            "note": seed.get("note") or "API 暂未抓到「每日一读」频道（无登录态），展示 seed.json 中的真实文章。",
        }
    if seed and (seed.get("url") or seed.get("title")):
        return {
            "title": seed.get("title", "学习强国 · 每日一读"),
            "date": seed.get("date", ""),
            "url": seed.get("url", XUEXI_URL),
            "items": [],
            "source": "xuexi_seed.json (仅链接)",
            "note": seed.get("note")
            or "学习强国搜索结果走签名网关 + JS 渲染，仅保留跳转链接。",
        }

    # 3) 已知链接兜底
    live = fetch_xuexi_link()
    live.setdefault("date", "")
    live["items"] = []
    live["source"] = "已知搜索页 URL"
    live["note"] = "学习强国文章走签名网关，服务端无法自动抓取正文；可点击前往官网阅读。"
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
        "daily_read.json written: rmrb=%d xuexi=%d (source=%s)"
        % (
            len(rmrb.get("items", [])),
            len(xuexi.get("items", [])),
            xuexi.get("source", "?"),
        )
    )


if __name__ == "__main__":
    main()
