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

HUPU_URL_RE = re.compile(r"https?://bbs\.hupu\.com/(\d+)\.html")
HUPU_HOT_RE = re.compile(r"(\d+)\s*亮\s*(\d+)\s*回复")
ZIBO8_URL_RE = re.compile(r"https?://news\.zhibo8\.com/[\w-]+/[\w-]+/[\w-]+\.htm")
ZIBO8_NATIVE_RE = re.compile(r"native\.htm$")


@dataclass
class NewsItem:
    source: str
    section: str
    title: str
    url: str
    replies: int = 0
    likes: int = 0
    fetched_at: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "section": self.section,
            "title": self.title,
            "url": self.url,
            "replies": self.replies,
            "likes": self.likes,
            "fetched_at": self.fetched_at,
        }


def _now_iso() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def _get(url: str, timeout: int = 10) -> str | None:
    """带一次重试的 GET。"""
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
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


def fetch_zhibo8(section_code: str, section_name: str = "") -> List[NewsItem]:
    """抓取直播吧新闻列表页。

    解析策略:
    - 找所有 href 匹配 news.zhibo8.com/.../native.htm 的 <a>
    - 只取标题 + URL(直播吧列表页无热度指标)
    """
    url = f"https://news.zhibo8.com/{section_code}/"
    html = _get(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    items: List[NewsItem] = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://news.zhibo8.com" + href
        if not ZIBO8_URL_RE.match(href):
            continue
        if not ZIBO8_NATIVE_RE.search(href):
            continue
        if href in seen_urls:
            continue

        title = a.get_text(strip=True)
        if not title or len(title) < 4:
            continue

        seen_urls.add(href)
        items.append(
            NewsItem(
                source="zhibo8",
                section=section_name or section_code,
                title=title,
                url=href,
                replies=0,
                likes=0,
                fetched_at=_now_iso(),
            )
        )

    # Sanity check
    if len(items) < 3:
        print(f"[scrape] WARN: zhibo8/{section_code} 仅解析到 {len(items)} 条,可能 HTML 结构变更")
        return []
    return items


def fetch_all(config: dict) -> List[NewsItem]:
    """根据 config 抓取所有启用的源。"""
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
        for s in zibo8_cfg.get("sections", []):
            try:
                items.extend(fetch_zhibo8(s["code"], s.get("name", s["code"])))
            except Exception as e:
                print(f"[scrape] zhibo8/{s['code']} 异常: {e}")

    return items
