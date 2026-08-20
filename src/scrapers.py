"""虎扑 + 直播吧 抓取层。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List

import requests
from bs4 import BeautifulSoup

BEIJING_TZ = timezone(timedelta(hours=8))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# 移动版 UA(直播吧 m.zhibo8.com 列表页按 UA 返回不同结构)
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)
HEADERS_MOBILE = {
    "User-Agent": UA_MOBILE,
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://m.zhibo8.com/news.htm",
}

HUPU_URL_RE = re.compile(r"https?://bbs\.hupu\.com/(\d+)\.html")
HUPU_HOT_RE = re.compile(r"(\d+)\s*亮\s*(\d+)\s*回复")
# 直播吧移动版新闻 URL:/news/web/{type}/{date}/{id}native.htm
ZIBO8_MOBILE_URL_RE = re.compile(r"/news/web/([\w-]+)/[\w-]+/[\w-]+native\.htm")
ZIBO8_NATIVE_RE = re.compile(r"native\.htm$")
# 直播吧评论数 AJAX 接口(从抓包确认):
# https://cache.zhibo8.cc/json/{YYYY_MM_DD}/news/{type}/{id}native_count.htm
ZIBO8_COUNT_RE = re.compile(
    r"/news/web/([\w-]+)/(\d{4}-\d{2}-\d{2})/([\w-]+)native\.htm"
)
ZIBO8_COUNT_URL_TMPL = (
    "https://cache.zhibo8.cc/json/{date_us}/news/{cat}/{id}native_count.htm"
)


@dataclass
class NewsItem:
    source: str
    section: str
    title: str
    url: str
    replies: int = 0
    likes: int = 0
    fetched_at: str = ""
    content: str = ""  # 详情页正文摘要,LLM 调用前由 main.py 填充

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "section": self.section,
            "title": self.title,
            "url": self.url,
            "replies": self.replies,
            "likes": self.likes,
            "fetched_at": self.fetched_at,
            "content": self.content,
        }


# 详情页正文最多截断到 800 字(约 400 token),平衡 token 成本与 LLM 信息量
DETAIL_MAX_CHARS = 800


def fetch_hupu_detail(url: str, timeout: int = 10) -> str:
    """抓虎扑帖子主楼正文前 800 字。

    选择器优先级:
    1. div.thread-content-detail    最干净(纯主楼正文)
    2. div[class*="main-post-info"] 备选(带部分元信息)
    """
    html = _get(url, timeout)
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")

    # 优先:thread-content-detail(纯正文)
    node = soup.select_one("div.thread-content-detail")
    # 备选:main-post-info(带元信息但更鲁棒)
    if not node:
        node = soup.select_one("div[class*='main-post-info']")

    if not node:
        print(f"[scrape] hupu 详情页未找到正文容器: {url}")
        return ""
    text = node.get_text("\n", strip=True)
    return text[:DETAIL_MAX_CHARS]


def fetch_zhibo8_detail(url: str, timeout: int = 10) -> str:
    """抓直播吧新闻正文前 800 字。

    选择器:div.content(直播吧 PC 版与移动版详情页共用正文容器)
    """
    # 移动版 URL 需用移动 UA 请求,避免被重定向到 PC 版
    headers = HEADERS_MOBILE if "m.zhibo8.com" in url else HEADERS
    html = _get(url, timeout, headers=headers)
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")

    # 直播吧新闻正文标准容器
    node = soup.select_one("div.content")
    if not node:
        # 备选:常见 article 容器
        node = soup.select_one("div.article-content, div.news-content, article")

    if not node:
        print(f"[scrape] zhibo8 详情页未找到正文容器: {url}")
        return ""
    text = node.get_text("\n", strip=True)
    return text[:DETAIL_MAX_CHARS]


def fetch_zhibo8_comment_count(url: str, timeout: int = 5) -> int:
    """查询直播吧新闻评论数。

    通过 AJAX 接口 cache.zhibo8.cc/json/{date}/news/{cat}/{id}native_count.htm
    返回 JSON,取 all_num 字段(总评论数)。
    失败返回 0(不影响主流程)。
    """
    m = ZIBO8_COUNT_RE.search(url)
    if not m:
        return 0
    cat = m.group(1)
    date_dash = m.group(2)
    id_part = m.group(3)
    date_us = date_dash.replace("-", "_")
    api_url = ZIBO8_COUNT_URL_TMPL.format(date_us=date_us, cat=cat, id=id_part)
    headers = {
        "User-Agent": UA_MOBILE,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://m.zhibo8.com/news.htm",
    }
    try:
        r = requests.get(api_url, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return 0
        data = r.json()
        return int(data.get("all_num", 0))
    except (requests.RequestException, ValueError, TypeError) as e:
        print(f"[scrape] 评论数查询失败 {url}: {type(e).__name__}: {e}")
        return 0


def _now_iso() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def _get(url: str, timeout: int = 10, headers: dict | None = None) -> str | None:
    """带一次重试的 GET。headers 不传时用默认 PC UA。"""
    h = headers or HEADERS
    for attempt in range(2):
        try:
            r = requests.get(url, headers=h, timeout=timeout)
            r.encoding = r.apparent_encoding or "utf-8"
            if r.status_code == 200:
                return r.text
            print(f"[scrape] {url} HTTP {r.status_code} (attempt {attempt + 1})")
        except requests.RequestException as e:
            print(f"[scrape] {url} {type(e).__name__}: {e} (attempt {attempt + 1})")
    return None


def fetch_hupu(section_code: str, section_name: str = "") -> List[NewsItem]:
    """抓取虎扑板块热帖列表。

    解析策略(防御性):
    - 找所有 href 匹配 bbs.hupu.com/<id>.html 的 <a>
    - 从 <a> 自身文本取标题
    - 从父元素文本用正则提取 "X亮Y回复"
    """
    url = f"https://bbs.hupu.com/{section_code}"
    html = _get(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    items: List[NewsItem] = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # 补全相对 URL
        if href.startswith("/"):
            href = "https://bbs.hupu.com" + href
        if not HUPU_URL_RE.match(href):
            continue
        if href in seen_urls:
            continue

        title = a.get_text(strip=True)
        if not title or len(title) < 4:
            continue
        # 跳过导航/页码链接
        if title.isdigit():
            continue

        # 从父级文本提取热度
        parent = a.parent
        parent_text = parent.get_text(" ", strip=True) if parent else ""
        m = HUPU_HOT_RE.search(parent_text)
        replies = int(m.group(2)) if m else 0
        likes = int(m.group(1)) if m else 0

        seen_urls.add(href)
        items.append(
            NewsItem(
                source="hupu",
                section=section_name or section_code,
                title=title,
                url=href,
                replies=replies,
                likes=likes,
                fetched_at=_now_iso(),
            )
        )

    # Sanity check
    if len(items) < 5:
        print(f"[scrape] WARN: hupu/{section_code} 仅解析到 {len(items)} 条,可能 HTML 结构变更")
        return []
    return items


def fetch_zhibo8(accept_types: list[str] | None = None) -> List[NewsItem]:
    """抓取直播吧移动版首页 m.zhibo8.com/news.htm。

    移动版首页一次返回全部三类新闻(nba/zuqiu/game),通过 URL 路径段
    /news/web/{type}/... 自动归类。accept_types 控制保留哪些类型
    (默认全部保留)。

    热度指标:列表页 HTML 不含评论数,需对每个 URL 单独调
    fetch_zhibo8_comment_count 补齐(由调用方决定是否批量查)。
    """
    url = "https://m.zhibo8.com/news.htm"
    html = _get(url, headers=HEADERS_MOBILE)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    items: List[NewsItem] = []
    seen_urls = set()
    accept = set(accept_types) if accept_types else None

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # 移动版 URL 形如 /news/web/{type}/{date}/{id}native.htm
        m = ZIBO8_MOBILE_URL_RE.search(href)
        if not m:
            continue
        cat = m.group(1)
        if accept and cat not in accept:
            continue

        # 补全为绝对 URL(移动版域名)
        if href.startswith("/"):
            href = "https://m.zhibo8.com" + href
        if href in seen_urls:
            continue

        title = a.get_text(strip=True)
        if not title or len(title) < 4:
            continue

        seen_urls.add(href)
        items.append(
            NewsItem(
                source="zhibo8",
                section=cat,           # nba/zuqiu/game
                title=title,
                url=href,
                replies=0,             # 由调用方批量查评论数后填充
                likes=0,
                fetched_at=_now_iso(),
            )
        )

    # Sanity check
    if len(items) < 10:
        print(f"[scrape] WARN: zhibo8 移动版仅解析到 {len(items)} 条,可能 HTML 结构变更")
        return []
    return items


def fetch_all(config: dict) -> List[NewsItem]:
    """根据 config 抓取所有启用的源。

    直播吧移动版抓取后,会对每条新闻调评论数 AJAX 接口补齐 replies 字段。
    为控制请求数和延迟,只对前 N 条(默认 30)查评论数。
    """
    items: List[NewsItem] = []
    src_cfg = config.get("sources", {})

    hupu_cfg = src_cfg.get("hupu", {})
    if hupu_cfg.get("enabled", True):
        for s in hupu_cfg.get("sections", []):
            try:
                items.extend(fetch_hupu(s["code"], s.get("name", s["code"])))
            except Exception as e:
                print(f"[scrape] hupu/{s['code']} 异常: {e}")

    zibo8_cfg = src_cfg.get("zhibo8", {})
    if zibo8_cfg.get("enabled", True):
        try:
            accept_types = zibo8_cfg.get("accept_types")  # None 表示全部保留
            zb_items = fetch_zhibo8(accept_types)
            # 批量查评论数(只查前 N 条,控制请求数)
            count_limit = int(zibo8_cfg.get("comment_count_limit", 30))
            for i, it in enumerate(zb_items):
                if i >= count_limit:
                    break
                it.replies = fetch_zhibo8_comment_count(it.url)
                if it.replies > 0:
                    print(f"[scrape] zhibo8 评论数={it.replies}  {it.title[:40]}")
            items.extend(zb_items)
        except Exception as e:
            print(f"[scrape] zhibo8 异常: {e}")

    return items
