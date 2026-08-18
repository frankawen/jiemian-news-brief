#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 daily_read.json —— 「每日一读」板块多源聚合器

四大功能区（按日期确定性轮转，全部更新一轮后自动从头重复）：
  1) 诗经专栏  gushiwen.cn/gushi/shijing.aspx  —— 305 篇，每天 1 篇 + 原文
     （译文/注释/赏析为 guwendao 反爬动态加载，服务端抓不到，降级为原文页链接）
  2) 名句      gushiwen.cn/mingjus/            —— 50 条，每天 1 条 + 译文/注释/赏析（全静态可抓）
  3) 学习强国·每日一读  —— 通过 search.xuexi.cn API 抓取；无登录态时降级 xuexi_seed.json 的 5 篇《读者》
  4) 每日口才  小红书 65b8f78 主页 —— 小红书需登录态，服务端 API 返回 500（风控），降级为每日主页深链

兼容保留：
  - 人民日报 05 版评论（自动抓取，每天回溯最近有文章的日期）

每天轮转算法：hash(日期字符串) % 条目数  →  同一天所有人看到同一篇，隔天换下一篇，
轮完一轮（诗经305天 / 名句50天）自动循环。
"""
import os
import re
import json
import hashlib
import datetime
import urllib.parse
import urllib.request
import requests
from bs4 import BeautifulSoup

TZ = datetime.timezone(datetime.timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.gushiwen.cn/",
}

# 诗经列表页
SHIJING_INDEX_URL = "https://www.gushiwen.cn/gushi/shijing.aspx"
# 名句列表页
MINGJU_INDEX_URL = "https://www.gushiwen.cn/mingjus/"
# 学习强国「每日一读」搜索页（签名网关 + JS 渲染，服务端无法抓正文/简介，仅保留链接）
XUEXI_URL = (
    "https://www.xuexi.cn/dc12897105c8c496d783c5e4d3b680a2/"
    "9a75e290b9cf8cb8fb529a6e503db78d.html"
    "?query=%E6%AF%8F%E6%97%A5%E4%B8%80%E8%AF%BB&page=1&search_source=1"
    "&program_id=0&product_params=%7B%22time_filter%22%3A%22all%22%2C%22type_filter"
    "%22%3A%22all%22%2C%22sort_method%22%3A%22integrated%22%2C%22wenhui_sort_method"
    "%22%3A%22near_far%22%2C%22search_method%22%3A%22all%22%7D&_t=1787019679134"
)
# 每日口才 · 小红书博主主页（图片分享，需登录态；服务端 API 返回 500，降级为深链）
KOUCAL_URL = (
    "https://www.xiaohongshu.com/user/profile/65b8f780000000000d01ee44"
    "?xsec_token=ABwANG1wqNrs-1dZwP-LM271nvbLBWhkmUyRcR7MzxvrA="
    "&xsec_source=pc_search"
)


def now_beijing():
    return datetime.datetime.now(TZ)


def http_get(url, referer=None, timeout=25):
    """统一 HTTP 抓取。

    策略：sandbox 内 Python 的 TLS 对部分站点被中间人切断（SSL EOF），但 Bash 工具的
    curl 可通。因此：
      1) 若设了环境变量 PREFETCH_DIR 且 prefetch/<md5(url)>.html 存在，直接读本地
         （sandbox 开发：由 Bash 工具 curl 预抓后离线运行）
      2) 否则 curl 子进程联网（GitHub Actions 等环境可通），成功则落盘缓存
      3) 回退 requests
    返回解码后的文本字符串。
    """
    import subprocess

    ck = hashlib.md5(url.encode("utf-8")).hexdigest()
    pf_dir = os.environ.get("PREFETCH_DIR")
    if pf_dir:
        pf = os.path.join(pf_dir, ck + ".html")
        if os.path.exists(pf):
            try:
                return open(pf, encoding="utf-8", errors="ignore").read()
            except Exception:
                pass
    cmd = ["curl", "-sL", "--max-time", str(timeout), "-A", HEADERS["User-Agent"]]
    if referer:
        cmd += ["-H", "Referer: " + referer]
    cmd += [url]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
        if out.returncode == 0 and out.stdout:
            html = out.stdout.decode("utf-8", errors="ignore")
            if pf_dir:
                try:
                    os.makedirs(pf_dir, exist_ok=True)
                    open(os.path.join(pf_dir, ck + ".html"), "w", encoding="utf-8").write(html)
                except Exception:
                    pass
            return html
    except Exception as e:
        print("curl err:", e)
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.encoding = "utf-8"
        return r.text
    except Exception as e:
        print("requests fallback err:", e)
        return ""


# --------------------------------------------------------------------------- #
# 通用：按日期确定性轮转
# --------------------------------------------------------------------------- #
def pick_daily(items, date_str):
    """hash(日期) % 条目数，保证同一天稳定、隔天轮转、循环复用。"""
    if not items:
        return None
    h = int(hashlib.md5(date_str.encode("utf-8")).hexdigest(), 16)
    return items[h % len(items)]


# --------------------------------------------------------------------------- #
# 1) 诗经专栏
# --------------------------------------------------------------------------- #
def fetch_shijing_index():
    """抓诗经列表页，返回 [{path, title, category}] 共 305 篇。缓存 7 天。"""
    cache = os.path.join(HERE, "shijing_index.json")
    if os.path.exists(cache):
        age = now_beijing().timestamp() - os.path.getmtime(cache)
        if age < 7 * 86400:
            try:
                return json.load(open(cache, encoding="utf-8"))
            except Exception:
                pass
    try:
        html = http_get(SHIJING_INDEX_URL, referer="https://www.gushiwen.cn/", timeout=25)
        soup = BeautifulSoup(html, "html.parser")
        items = []
        for tc in soup.find_all("div", class_="typecont"):
            book = tc.find("div", class_="bookMl")
            cat = book.get_text(strip=True) if book else ""
            for span in tc.find_all("span"):
                a = span.find("a")
                if a and a.get("href", "").startswith("/shiwenv_"):
                    items.append(
                        {
                            "path": a["href"],
                            "title": a.get_text(strip=True),
                            "category": cat,
                        }
                    )
        seen, uniq = set(), []
        for it in items:
            if it["path"] not in seen:
                seen.add(it["path"])
                uniq.append(it)
        if uniq:
            json.dump(uniq, open(cache, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            return uniq
    except Exception as e:
        print("shijing index err:", e)
    # 缓存兜底
    if os.path.exists(cache):
        try:
            return json.load(open(cache, encoding="utf-8"))
        except Exception:
            pass
    return []


def fetch_shijing_detail(path):
    """抓诗经某篇详情，取到左侧原文（contson 第一段）。译文/注释/赏析为动态反爬，抓不到。"""
    url = "https://www.gushiwen.cn" + path
    try:
        html = http_get(url, referer="https://www.gushiwen.cn/", timeout=25)
        soup = BeautifulSoup(html, "html.parser")
        sons = soup.find("div", class_="sons")
        original = ""
        if sons:
            cs = sons.find("div", class_="contson")
            if cs:
                original = cs.get_text(strip=True)
        return {"url": url, "original": original}
    except Exception as e:
        print("shijing detail err:", e)
        return {"url": url, "original": ""}


# --------------------------------------------------------------------------- #
# 2) 名句
# --------------------------------------------------------------------------- #
def fetch_mingju_index():
    """抓名句列表页，返回 [{path, title}] 共 50 条。缓存 7 天。"""
    cache = os.path.join(HERE, "mingju_index.json")
    if os.path.exists(cache):
        age = now_beijing().timestamp() - os.path.getmtime(cache)
        if age < 7 * 86400:
            try:
                return json.load(open(cache, encoding="utf-8"))
            except Exception:
                pass
    try:
        html = http_get(MINGJU_INDEX_URL, referer="https://www.gushiwen.cn/", timeout=25)
        soup = BeautifulSoup(html, "html.parser")
        items = []
        for a in soup.find_all("a", href=re.compile(r"^/mingju/juv_")):
            items.append({"path": a["href"], "title": a.get_text(strip=True)})
        seen, uniq = set(), []
        for it in items:
            if it["path"] not in seen:
                seen.add(it["path"])
                uniq.append(it)
        if uniq:
            json.dump(uniq, open(cache, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            return uniq
    except Exception as e:
        print("mingju index err:", e)
    if os.path.exists(cache):
        try:
            return json.load(open(cache, encoding="utf-8"))
        except Exception:
            pass
    return []


def fetch_mingju_detail(path):
    """抓名句详情：译文 / 注释 / 赏析（gushiwen 名句页为全静态，可直接抓）。"""
    url = "https://www.gushiwen.cn" + path
    yiwen = zhushi = shangxi = ""
    try:
        html = http_get(url, referer="https://www.gushiwen.cn/", timeout=25)
        soup = BeautifulSoup(html, "html.parser")
        for p in soup.find_all("p"):
            span = p.find("span", class_="yzsSpan")
            if not span:
                continue
            label = span.get_text(strip=True)
            txt = p.get_text(strip=True).replace(label, "", 1).strip()
            if "译" in label:
                yiwen = txt
            elif "注" in label:
                zhushi = txt
            elif "赏" in label:
                shangxi = txt
    except Exception as e:
        print("mingju detail err:", e)
    return yiwen, zhushi, shangxi


# --------------------------------------------------------------------------- #
# 3) 学习强国 · 每日一读
# --------------------------------------------------------------------------- #
def fetch_xuexi_search():
    """调 learning强国 wenhui 搜索 API 拿「每日一读」前 5 条。

    完整 PC 端 jQuery ajax 协议（来自 static.xuexi.cn/search/online 的 JS bundle）：
      endpoint: https://search.xuexi.cn/api/search
      query (必填): query, page=1, size=15, hid=<32位>, client_version=PC:0.0.10,
                    search_source=2, program_id=1, product=wenhui_search,
                    product_params=<URL-encoded JSON>, _t=<毫秒时间戳>
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
            body = http_get(url, referer="https://www.xuexi.cn/", timeout=20)
            if not body:
                continue
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
                u = (rd.get("url") or "").strip().replace("&amp;", "&")
                pub = (rd.get("publish_time") or "").strip()
                channel = (ext.get("title") or "").strip() if ext else ""
                if not title and not summary:
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
    """学习强国每日一读：优先调官方 search API，失败则降级到仓库 xuexi_seed.json。"""
    p = os.path.join(HERE, "xuexi_seed.json")
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
                "note": "通过 PC 端 jQuery ajax 完整参数实时抓取。",
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
            "note": seed.get("note") or "学习强国搜索结果走签名网关 + JS 渲染，仅保留跳转链接。",
        }

    # 3) 已知链接兜底
    return {
        "title": "学习强国 · 每日一读",
        "date": "",
        "url": XUEXI_URL,
        "items": [],
        "source": "已知搜索页 URL",
        "note": "学习强国文章走签名网关，服务端无法自动抓取正文；可点击前往官网阅读。",
    }


# --------------------------------------------------------------------------- #
# 4) 每日口才（小红书）
# --------------------------------------------------------------------------- #
def fetch_koucai():
    """小红书 65b8f78 主页：服务端 API 返回 500（风控/需登录态），降级为每日主页深链。

    若将来在本机带登录态运行（Playwright 执行 JS），可在此处替换为真实笔记列表抓取。
    """
    return {
        "title": "每日口才",
        "url": KOUCAL_URL,
        "source": "小红书 · 每日口才（图片分享）",
        "note": "小红书服务端 API 返回 500（风控/需登录态），每日更新该博主主页深链；"
                "图文请在 APP 内查看。如需自动转文字，请在本机带登录态运行。",
    }


# --------------------------------------------------------------------------- #
# 兼容：人民日报 05 版评论
# --------------------------------------------------------------------------- #
def fetch_rmrb_comment():
    """回溯最近 8 天，取第一个有文章的 05 版评论。"""
    BASE = "https://paper.people.com.cn/rmrb/pc/layout"
    for back in range(0, 8):
        d = now_beijing() - datetime.timedelta(days=back)
        ym = d.strftime("%Y%m")
        dd = d.strftime("%d")
        url = "%s/%s/%s/node_05.html" % (BASE, ym, dd)
        try:
            html = http_get(url, referer="https://paper.people.com.cn/", timeout=20)
            if not html:
                continue
            soup = BeautifulSoup(html, "lxml")
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


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    today = now_beijing().strftime("%Y-%m-%d")

    # 1) 诗经
    shijing_idx = fetch_shijing_index()
    sj = pick_daily(shijing_idx, today)
    shijing = {"title": "", "category": "", "original": "", "url": "", "source": "", "note": ""}
    if sj:
        detail = fetch_shijing_detail(sj["path"])
        shijing = {
            "title": sj["title"],
            "category": sj["category"],
            "original": detail.get("original", ""),
            "url": detail.get("url", ""),
            "source": "gushiwen.cn 诗经",
            "note": "译文/注释/赏析为 guwendao 反爬动态加载，需前往原文页查看。",
        }

    # 2) 名句
    mingju_idx = fetch_mingju_index()
    mj = pick_daily(mingju_idx, today)
    mingju = {"title": "", "yiwen": "", "zhushi": "", "shangxi": "", "url": "", "source": ""}
    if mj:
        yiwen, zhushi, shangxi = fetch_mingju_detail(mj["path"])
        mingju = {
            "title": mj["title"],
            "yiwen": yiwen,
            "zhushi": zhushi,
            "shangxi": shangxi,
            "url": "https://www.gushiwen.cn" + mj["path"],
            "source": "gushiwen.cn 名句",
        }

    # 3) 学习强国
    xuexi = load_xuexi_seed()
    # 4) 口才
    koucai = fetch_koucai()
    # 兼容：人民日报
    rmrb = fetch_rmrb_comment()

    data = {
        "updated_at": now_beijing().isoformat(timespec="seconds"),
        "date": today,
        "sections": {
            "shijing": shijing,
            "mingju": mingju,
            "xuexi": xuexi,
            "koucai": koucai,
            "rmrb": rmrb,
        },
    }
    out = os.path.join(HERE, "daily_read.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(
        "daily_read.json written | 诗经=%s 名句=%s 学强国=%d 口才=%s 人民日报=%d"
        % (
            shijing.get("title") or "(空)",
            mingju.get("title") or "(空)",
            len(xuexi.get("items", [])),
            "链接" if koucai.get("url") else "(空)",
            len(rmrb.get("items", [])),
        )
    )


if __name__ == "__main__":
    main()
