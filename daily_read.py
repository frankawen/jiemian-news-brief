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
# 学习强国「每日一读」专题（公开 SPA，未登录可点开文章但服务端反爬抓不到列表/正文）
# 整页 tabs（今晚陪你听/特别策划/我说"学习强国"/熊猫什么都知道/倾听/每日一读）
# 中只有「每日一读」是公开可访问的；其他 tab 需要登录。
XUEXI_TOPIC_URL = (
    "https://article.xuexi.cn/news/index.html?source=share&study_style_id=feeds_pure"
    "&reco_id=103ae257e010ac14f3cf000d&share_to=wx_single&study_share_enable=0"
    "&related_id=14122703379888049123&related_type=1&study_comment_disable=0"
    "&ref_read_id=AFB1543C-1E09-4339-9592-52C8DBA40C00#/special-topic/5427951075763236"
)
# 兼容旧引用
XUEXI_URL = XUEXI_TOPIC_URL
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
# 3) 学习强国 · 每日一读（公开 SPA 专题页，只展示 2 篇文章）
# --------------------------------------------------------------------------- #
def fetch_xuexi_dailyread():
    """学习强国「每日一读」tab：服务端 API 全部反爬（search.xuexi.cn 需签名、article.xuexi.cn
    未登录 SSR 返兜底 HTML、lgdata 老接口又 GBK 编码 + 找不到本专题 ID），因此按师父要求改为
    「纯跳转链接」——只给出专题页链接，用户自行点击在浏览器打开阅读。

    Returns: dict {title, date, topic_url, url, items:[], source, note}
    """
    today = now_beijing().strftime("%Y-%m-%d")
    return {
        "title": "学习强国 · 每日一读",
        "date": today,
        "topic_id": "5427951075763236",
        "topic_url": XUEXI_TOPIC_URL,
        "url": XUEXI_TOPIC_URL,
        "items": [],
        "source": "学习强国专题页（纯跳转链接）",
        "note": "「每日一读」服务端反爬抓不到内容，已改为单跳转链接，点按钮在浏览器打开即可阅读。",
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

    # 3) 学习强国 · 每日一读（固定 2 篇文章种子）
    xuexi = fetch_xuexi_dailyread()
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
