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

  # 对话模式（供 OpenClaw / 微信助手随问随答，不推送、不写去重状态）
  python news_brief.py --chat            # 精简版：标题 + 链接（一条尽量装下）
  python news_brief.py --chat --detail   # 详细版：标题 + 时间 + 摘要(截断) + 链接
  python news_brief.py --chat --json     # 以 JSON {"messages":[...]} 输出，便于程序逐条发送

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
import json
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
    """返回 (html, text) 两种格式的简报。

    - html_doc：用于 pushplus（带内联样式）
    - text_doc：用于 serverchan / 企业微信（Markdown 友好，单 \n 不分段落，
      用 \n\n + 标题/分隔条让微信客户端正确分段）
    """
    now = datetime.datetime.now(tz)
    date_str = now.strftime("%Y年%m月%d日 %H:%M")
    weekday = "一二三四五六日"[now.weekday()]
    total = sum(len(v) for v in categories.values())

    # ---------------------------- HTML ----------------------------
    style = (
        "<style>"
        ".ns{margin:18px 0 0 0;}"
        ".ns h2{color:#d83a3a;border-left:4px solid #d83a3a;padding:2px 0 2px 8px;margin:0 0 8px 0;}"
        ".ns h2 small{font-size:12px;color:#888;font-weight:normal;margin-left:6px;}"
        ".n{margin:12px 0;padding:0 0 10px 0;border-bottom:1px dashed #eee;}"
        ".n:last-child{border-bottom:none;}"
        ".t{font-size:15px;color:#222;}"
        ".t a{color:#222;text-decoration:none;}"
        ".tm{color:#999;font-size:12px;margin-left:6px;}"
        ".s{color:#555;font-size:13px;margin:4px 0 6px 0;line-height:1.55;}"
        ".u{font-size:11px;color:#888;word-break:break-all;}"
        ".empty{color:#aaa;font-style:italic;margin:6px 0;}"
        "</style>"
    )
    h = [
        style,
        "<h1 style='margin:0 0 6px 0;color:#222;'>📰 界面新闻 · 每日快报</h1>",
        f"<p style='color:#888;font-size:13px;margin:0 0 6px 0;'>"
        f"🕐 {date_str} 星期{weekday} ｜ 共 {total} 条</p>",
    ]
    for name, items in categories.items():
        h.append("<div class='ns'>")
        h.append(
            f"<h2>🔸 {name}<small>（{len(items)} 条）</small></h2>"
        )
        if not items:
            h.append("<p class='empty'>今日暂无更新</p>")
            h.append("</div>")
            continue
        for it in items:
            t = html.escape(it.time_text or "")
            summary = html.escape(it.summary or "")
            url = html.escape(it.url)
            h.append("<div class='n'>")
            h.append(
                "<div class='t'>"
                f"<a href='{url}'>{html.escape(it.title)}</a>"
                + (f"<span class='tm'>{t}</span>" if t else "")
                + "</div>"
            )
            if summary:
                h.append(f"<div class='s'>{summary}</div>")
            h.append(f"<div class='u'>🔗 <a href='{url}'>{url}</a></div>")
            h.append("</div>")
        h.append("</div>")
    html_doc = "\n".join(h)

    # ---------------------------- Markdown / 纯文本 ----------------------------
    def short(s, n=180):
        s = (s or "").replace("\n", " ").strip()
        return s if len(s) <= n else s[: max(n - 1, 1)] + "…"

    sections = []
    for name, items in categories.items():
        body = [f"### 🔸 {name}（{len(items)} 条）", ""]
        if not items:
            body.append("_今日暂无更新_")
            sections.append("\n".join(body))
            continue
        for i, it in enumerate(items, 1):
            item = [f"**{i}. [{it.title}]({it.url})**"]
            if it.time_text:
                item.append(f"⏰ {it.time_text}")
            if it.summary:
                item.append(f"📝 {short(it.summary)}")
            item.append(f"🔗 [阅读原文]({it.url})")
            body.append("\n".join(item))
            body.append("")  # 条与条之间空行 → 微信段间距
        # 去掉段落末尾多余空行
        while body and body[-1] == "":
            body.pop()
        sections.append("\n".join(body))

    head = (
        f"# 📰 界面新闻 · 每日快报\n\n"
        f"🕐 {date_str} 星期{weekday} ｜ 共 {total} 条"
    )
    text_doc = head + "\n\n---\n\n" + "\n\n---\n\n".join(sections)

    return html_doc, text_doc


# ---------------------------------------------------------------------------
# 对话模式（chat）：供 OpenClaw 等对话助手随问随答使用
# ---------------------------------------------------------------------------

# 多块消息之间的分隔符（程序按此切分多条微信消息）
CHAT_SPLIT = "\n\n§§§SPLIT§§§\n\n"


def _short(s: str, n: int = 180) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: max(n - 1, 1)] + "…"


def write_news_artifact(
    categories: Dict[str, List[NewsItem]], tz: datetime.timezone
) -> None:
    """把当天抓取的新闻写成 news.json，供工作台等页面通过 GitHub 采集展示。

    写入仓库根目录 news.json（结构化的三栏目新闻），由 workflow 提交回仓库，
    页面侧用 raw.githubusercontent.com 直接 fetch，无需密钥、无跨域限制。
    """

    def _cat(name: str, items: List[NewsItem]):
        return [
            {
                "title": it.title,
                "url": it.url,
                "time": it.time_text or "",
                "summary": _short(it.summary or "", 180),
            }
            for it in items
        ]

    data = {
        "updated_at": datetime.datetime.now(tz).isoformat(timespec="seconds"),
        "source": "界面新闻 · 快报",
        "categories": {name: _cat(name, items) for name, items in categories.items()},
    }
    with open("news.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    total = sum(len(v) for v in data["categories"].values())
    print(f"[info] 已写入 news.json（{total} 条）", file=sys.stderr)


def build_chat_text(
    categories: Dict[str, List[NewsItem]], tz: datetime.timezone, detail: bool = False
) -> str:
    """生成对话助手可直接作为回复的文本。

    - 精简版（detail=False）：标题 + 链接，一条消息尽量装下。
    - 详细版（detail=True）：标题 + 时间 + 摘要(截断180字) + 链接（Markdown 友好）。
    均不推送、不写去重状态（与定时推送链路完全隔离）。
    """
    now = datetime.datetime.now(tz)
    date_str = now.strftime("%Y年%m月%d日 %H:%M")
    weekday = "一二三四五六日"[now.weekday()]
    total = sum(len(v) for v in categories.values())

    head = f"📰 界面新闻·每日快报\n🕐 {date_str} 星期{weekday} ｜ 共 {total} 条"
    blocks = [head]
    for name, items in categories.items():
        lines = [f"\n【{name}】（{len(items)} 条）"]
        if not items:
            lines.append("  今日暂无更新")
            blocks.append("\n".join(lines))
            continue
        for i, it in enumerate(items, 1):
            if detail:
                line = f"**{i}. [{it.title}]({it.url})**"
                if it.time_text:
                    line += f"\n⏰ {it.time_text}"
                if it.summary:
                    line += f"\n📝 {_short(it.summary)}"
                line += f"\n🔗 [阅读原文]({it.url})"
            else:
                line = f"{i}. {it.title}\n   {it.url}"
            lines.append(line)
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def split_chat(text: str, limit: int = 1700) -> List[str]:
    """按行切分，尽量不切断单条新闻，返回多条消息文本。

    limit 为单条微信消息的字数上限（约 2000 中文字，留余量）。
    """
    lines = text.split("\n")
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for ln in lines:
        if cur and cur_len + len(ln) + 1 > limit:
            chunks.append("\n".join(cur))
            cur = []
            cur_len = 0
        cur.append(ln)
        cur_len += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks or [text]


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

def load_state() -> dict:
    """读取已推送状态，用于 17:00 对 8:00 去重。"""
    path = os.getenv("STATE_FILE", ".state/pushed_ids.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    """写回去重状态文件（由 GitHub Actions 提交回仓库，以便跨次运行保留）。"""
    path = os.getenv("STATE_FILE", ".state/pushed_ids.json")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 写入去重状态失败：{e}", file=sys.stderr)


def _getenv(name: str, default: str) -> str:
    """读取环境变量；为空或仅空白时回退默认值。

    解决 GitHub Actions 把"未配置的 secret"渲染成空字符串环境变量、
    导致 os.getenv(name, default) 的默认值不生效的问题。
    """
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v


def main():
    parser = argparse.ArgumentParser(description="界面新闻快报每日简报")
    parser.add_argument("--dry-run", action="store_true", help="只打印简报，不推送")
    parser.add_argument(
        "--chat",
        action="store_true",
        help="对话模式：输出可直接回复的简报文本（不推送、不写去重状态），配合 OpenClaw 等对话助手使用",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="对话模式输出详细版（含摘要）；默认精简版（仅标题+链接）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="对话模式以 JSON 输出 {\"messages\":[...]}，便于程序逐条发送",
    )
    args = parser.parse_args()

    dry_run = args.dry_run or os.getenv("DRY_RUN") == "1"

    tz_offset = int(_getenv("TIMEZONE_OFFSET", "8"))
    tz = datetime.timezone(datetime.timedelta(hours=tz_offset))
    max_items = int(_getenv("MAX_ITEMS", "15"))
    today_only = _getenv("TODAY_ONLY", "1") != "0"

    print(f"[info] 解析分类链接 ...", file=sys.stderr)
    urls = resolve_category_urls()

    categories: Dict[str, List[NewsItem]] = {}
    all_items: List[tuple] = []
    for name, url in urls.items():
        print(f"[info] 抓取「{name}」：{url}", file=sys.stderr)
        items = fetch_category(name, url)
        items = filter_today(items, tz, today_only)
        items = items[:max_items]
        categories[name] = items
        for it in items:
            all_items.append((name, it))
        print(f"[info] 「{name}」拿到 {len(items)} 条", file=sys.stderr)

    # ---- 对话模式（chat）：仅输出文本，不推送、不写去重状态 ----
    if args.chat:
        text = build_chat_text(categories, tz, detail=args.detail)
        chunks = split_chat(text)
        if args.json:
            print(json.dumps({"messages": chunks}, ensure_ascii=False))
        else:
            print(CHAT_SPLIT.join(chunks))
        return

    # ---- 生成 news.json 产物（供工作台页面 GitHub 采集；仅定时/推送模式，不落 dry-run）----
    if not dry_run:
        write_news_artifact(categories, tz)

    # ---- 跨日去重：每天首次运行(8:00)全量；同日后续运行(17:00)剔除已推送 ----
    today_str = datetime.datetime.now(tz).strftime("%Y-%m-%d")
    state = load_state()
    already = set(state.get("ids", [])) if state.get("date") == today_str else set()

    pushed_categories: Dict[str, List[NewsItem]] = {name: [] for name in categories}
    pushed_ids: List[str] = []
    seen_urls: set = set()
    for name, it in all_items:
        if it.url in seen_urls:
            continue
        seen_urls.add(it.url)
        if it.url in already:
            continue
        pushed_categories[name].append(it)
        pushed_ids.append(it.url)

    if not any(pushed_categories.values()):
        print("[info] 本次无新增快讯（17:00 与 8:00 无重复新增），跳过推送。", file=sys.stderr)
        return

    html_doc, text_doc = build_brief(pushed_categories, tz)

    if dry_run:
        print("\n" + "=" * 60)
        print(text_doc)
        print("=" * 60)
        print("\n[info] DRY_RUN 模式，未推送（也未写入去重状态）。", file=sys.stderr)
        return

    ok = push(html_doc, text_doc)
    if ok:
        save_state({"date": today_str, "ids": list(already | set(pushed_ids))})
        print(f"[info] 已记录本次推送 {len(pushed_ids)} 条，供 17:00 去重。", file=sys.stderr)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
