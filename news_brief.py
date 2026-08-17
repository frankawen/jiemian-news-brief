#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
界面新闻·快报 每日简报抓取与推送

- 抓取「快报」模块下三个子栏目：今日热点 / 公司头条 / 时事追踪
- 解析服务端直出的快讯条目（标题、链接、摘要、时间）
- 按北京时间过滤「今日」快讯，生成简报
- 通过微信推送（支持 pushplus / Server酱 / 企业微信 三种方式）

用法：
  python news_brief.py                 # 正常抓取并推送
  DRY_RUN=1 python news_brief.py       # 只打印简报，不推送
  python news_brief.py --help          # 查看参数

环境变量（详见 README.md）：
  PUSH_METHOD       pushplus | serverchan | wecom   (默认 pushplus)
  PUSHPLUS_TOKEN    pushplus 的 token
  SERVERCHAN_KEY    Server酱(SCT) 的 sendkey
  WECOM_KEY         企业微信机器人 webhook key
  MAX_ITEMS         每个栏目最多条数 (默认 15)
  TODAY_ONLY        是否仅取今日快讯 1/0 (默认 1)
  TIMEZONE_OFFSET   时区偏移小时，默认 8 (北京时间)
  DRY_RUN           1 时只打印不推送
"""

import os
import sys
import argparse
import datetime
import html
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 三个子栏目 -> 界面快报分类直出页（category id 稳定，作为兜底）
CATEGORY_URLS: Dict[str, str] = {
    "今日热点": "https://www.jiemian.com/lists/1324kb.html",
    "公司头条": "https://www.jiemian.com/lists/1322kb.html",
    "时事追踪": "https://www.jiemian.com/lists/1325kb.html",
}

# 快报聚合页，用于动态解析分类链接（若兜底 URL 失效可自动纠正）
CATEGORY_INDEX = "https://www.jiemian.com/lists/4.html"

ITEM_CLASS = "columns-right-center__newsflash-item"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class NewsItem:
    def __init__(self, title: str, url: str, summary: str, time_text: str, ts: int):
        self.title = title
        self.url = url
        self.summary = summary
        self.time_text = time_text  # HH:MM
        self.ts = ts

    def __repr__(self):
        return f"<NewsItem {self.time_text} {self.title}>"


# ---------------------------------------------------------------------------
# 抓取与解析
# ---------------------------------------------------------------------------

def resolve_category_urls() -> Dict[str, str]:
    """从快报聚合页动态解析各分类的直出页 URL，失败时回退到硬编码映射。"""
    try:
        resp = requests.get(CATEGORY_INDEX, headers={"User-Agent": UA}, timeout=15)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        mapping: Dict[str, str] = {}
        for a in soup.find_all("a", attrs={"data-url": True}):
            name = a.get_text(strip=True)
            if name in CATEGORY_URLS:
                mapping[name] = a["data-url"]
        if len(mapping) == len(CATEGORY_URLS):
            return mapping
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 动态解析分类链接失败，使用兜底映射：{e}", file=sys.stderr)
    return dict(CATEGORY_URLS)


def fetch_category(name: str, url: str) -> List[NewsItem]:
    """抓取单个分类页，解析快讯条目。"""
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        resp.encoding = "utf-8"
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 抓取「{name}」失败：{e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    items: List[NewsItem] = []
    seen = set()

    for div in soup.select(f"div.{ITEM_CLASS}"):
        ts_raw = div.get("data-time")
        try:
            ts = int(ts_raw) if ts_raw else 0
        except ValueError:
            ts = 0

        date_node = div.select_one(".columns-right-center__newsflash-date-node")
        time_text = date_node.get_text(strip=True) if date_node else ""

        title_a = div.select_one("h4 a") or div.select_one("a")
        if not title_a:
            continue
        title = title_a.get_text(strip=True)
        link = title_a.get("href", "")
        if link and not link.startswith("http"):
            link = "https://www.jiemian.com" + link

        summary_node = div.select_one(".columns-right-center__newsflash-content__summary")
        summary = summary_node.get_text(strip=True) if summary_node else ""

        if not title or link in seen:
            continue
        seen.add(link)
        items.append(NewsItem(title, link, summary, time_text, ts))

    return items


def filter_today(items: List[NewsItem], tz: datetime.timezone, today_only: bool) -> List[NewsItem]:
    """按北京时间过滤今日快讯。"""
    if not today_only:
        return items
    today = datetime.datetime.now(tz).date()
    out = []
    for it in items:
        if it.ts:
            dt = datetime.datetime.fromtimestamp(it.ts, tz)
            if dt.date() == today:
                out.append(it)
        else:
            out.append(it)  # 无时间戳的一律保留
    return out


# ---------------------------------------------------------------------------
# 简报生成
# ---------------------------------------------------------------------------

def build_brief(categories: Dict[str, List[NewsItem]], tz: datetime.timezone) -> (str, str):
    """返回 (html, text) 两种格式的简报。"""
    now = datetime.datetime.now(tz)
    date_str = now.strftime("%Y年%m月%d日 %H:%M")
    weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]

    total = sum(len(v) for v in categories.values())

    # ---- HTML ----
    html_parts = [
        f"<h1>📰 界面新闻 · 每日快报</h1>",
        f"<p style='color:#888;font-size:13px;'>"
        f"{date_str} {weekday} ｜ 共 {total} 条（今日热点 / 公司头条 / 时事追踪）</p>",
        "<hr/>",
    ]
    for name, items in categories.items():
        html_parts.append(f"<h2>🔸 {name}（{len(items)} 条）</h2>")
        if not items:
            html_parts.append("<p style='color:#999;'>今日暂无更新</p>")
            continue
        html_parts.append("<ol>")
        for it in items:
            t = it.time_text or ""
            summary = it.summary or ""
            html_parts.append(
                f"<li>"
                f"<b><a href='{html.escape(it.url)}'>{html.escape(it.title)}</a></b> "
                f"<span style='color:#aaa;font-size:12px;'>{html.escape(t)}</span>"
                f"<br/><span style='color:#555;font-size:14px;'>{html.escape(summary)}</span>"
                f"</li>"
            )
        html_parts.append("</ol>")
    html_doc = "\n".join(html_parts)

    # ---- 纯文本 ----
    text_parts = [f"📰 界面新闻 · 每日快报", f"{date_str} {weekday} ｜ 共 {total} 条", ""]
    for name, items in categories.items():
        text_parts.append(f"【{name}】（{len(items)} 条）")
        if not items:
            text_parts.append("  今日暂无更新")
            continue
        for i, it in enumerate(items, 1):
            t = f"[{it.time_text}] " if it.time_text else ""
            text_parts.append(f"{i}. {t}{it.title}")
            if it.summary:
                text_parts.append(f"   {it.summary}")
            text_parts.append(f"   {it.url}")
        text_parts.append("")
    text_doc = "\n".join(text_parts)

    return html_doc, text_doc


# ---------------------------------------------------------------------------
# 微信推送
# ---------------------------------------------------------------------------

def push(html_content: str, text_content: str) -> bool:
    """根据 PUSH_METHOD 推送。返回是否成功。"""
    method = (os.getenv("PUSH_METHOD") or "pushplus").lower()
    title = "界面新闻 · 每日快报"

    if method == "pushplus":
        token = os.getenv("PUSHPLUS_TOKEN")
        if not token:
            print("[error] 未配置 PUSHPLUS_TOKEN", file=sys.stderr)
            return False
        resp = requests.post(
            "https://www.pushplus.plus/send",
            json={"token": token, "title": title, "content": html_content, "template": "html"},
            timeout=15,
        )
        return _check(resp, "pushplus")

    if method == "serverchan":
        key = os.getenv("SERVERCHAN_KEY")
        if not key:
            print("[error] 未配置 SERVERCHAN_KEY", file=sys.stderr)
            return False
        # Server酱 Turbo 接口；普通版请改成 https://sc.ftqq.com/{key}.send
        resp = requests.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": title, "desp": text_content},
            timeout=15,
        )
        return _check(resp, "serverchan")

    if method == "wecom":
        key = os.getenv("WECOM_KEY")
        if not key:
            print("[error] 未配置 WECOM_KEY", file=sys.stderr)
            return False
        resp = requests.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}",
            json={"msgtype": "markdown", "markdown": {"content": text_content}},
            timeout=15,
        )
        return _check(resp, "wecom")

    print(f"[error] 未知 PUSH_METHOD：{method}", file=sys.stderr)
    return False


def _check(resp, name: str) -> bool:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        print(f"[error] {name} 推送返回非 JSON：{resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return False
    ok = resp.status_code == 200 and (data.get("code") in (200, 0, "0") or data.get("errcode") in (0, None))
    if ok:
        print(f"[ok] {name} 推送成功：{data}")
    else:
        print(f"[error] {name} 推送失败：{data}", file=sys.stderr)
    return ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="界面新闻快报每日简报")
    parser.add_argument("--dry-run", action="store_true", help="只打印简报，不推送")
    args = parser.parse_args()

    dry_run = args.dry_run or os.getenv("DRY_RUN") == "1"

    tz_offset = int(os.getenv("TIMEZONE_OFFSET", "8"))
    tz = datetime.timezone(datetime.timedelta(hours=tz_offset))
    max_items = int(os.getenv("MAX_ITEMS", "15"))
    today_only = os.getenv("TODAY_ONLY", "1") != "0"

    print(f"[info] 解析分类链接 ...", file=sys.stderr)
    urls = resolve_category_urls()

    categories: Dict[str, List[NewsItem]] = {}
    for name, url in urls.items():
        print(f"[info] 抓取「{name}」：{url}", file=sys.stderr)
        items = fetch_category(name, url)
        items = filter_today(items, tz, today_only)
        items = items[:max_items]
        categories[name] = items
        print(f"[info] 「{name}」拿到 {len(items)} 条", file=sys.stderr)

    html_doc, text_doc = build_brief(categories, tz)

    if dry_run:
        print("\n" + "=" * 60)
        print(text_doc)
        print("=" * 60)
        print("\n[info] DRY_RUN 模式，未推送。", file=sys.stderr)
        return

    ok = push(html_doc, text_doc)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
