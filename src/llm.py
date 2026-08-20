"""LLM 调用 + 降级 + 软限流。

支持任意 OpenAI 兼容的 LLM 服务,通过环境变量切换:
- LLM_API_BASE:API endpoint(默认 DeepSeek)
- LLM_MODEL:模型名(默认 deepseek-chat,即 DeepSeek-V3)
- LLM_API_KEY:API Key

兼容 provider:DeepSeek / 智谱 GLM / 阿里云通义 / 字节豆包 / 月之暗面 等
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import requests

from scrapers import NewsItem
from state import State

# 默认 DeepSeek-V3(便宜快,中文质量好);通过环境变量可切换其他 provider
LLM_API_BASE = os.getenv(
    "LLM_API_BASE",
    "https://api.deepseek.com/v1/chat/completions",
)
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
TIMEOUT = 30  # DeepSeek 偶尔比 GLM 慢,放宽到 30s


def _parse_llm_json(content: str) -> dict:
    """从 LLM 响应中提取 JSON。

    兼容三种返回格式:
    1. 纯 JSON:{"important": true, ...}
    2. markdown 代码块:```json\\n{...}\\n```
    3. 含前后多余文字:好的,这是结果:\\n{...}\\n
    """
    content = content.strip()
    # 1. 直接尝试
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # 2. 从 ```json ... ``` 或 ``` ... ``` 提取
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 3. 从第一个 { 到最后一个 } 提取
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise json.JSONDecodeError("无法从 LLM 响应中提取 JSON", content, 0)

PROMPT_TEMPLATE = """你是体育新闻编辑。判断下列新闻是否值得作为"大新闻"推送给体育爱好者。

标题:{title}
来源:{source} - {section}
热度指标:回复 {replies} / 亮 {likes}
增速:回复 {velocity}/分钟
正文摘要(最多800字):
{content}

返回 JSON(严格,无其他文字):
{{
  "important": true 或 false,
  "score": 1-10 整数,
  "category": "新闻类型,从以下 8 类中选择一个:",
  "headline": "保留原标题完整信息,只去掉[流言板]/[官方]等站方前缀和记者名,不做压缩",
  "summary": "150字内基于正文的摘要,要包含关键细节(如涉及哪些球员/球队/选秀权/合同金额/纪录/比分)"
}}

category 可选值(只能选一个):
- nba: NBA 美职篮(NBA 球员/球队/选秀/交易)
- cba: 中国篮球(CBA/NBL/中国男篮)
- football_overseas: 海外足球(五大联赛/欧冠/世界杯/美洲杯/海外球员)
- football_china: 中国足球(中超/国足/中国球员/中国足球俱乐部相关)
- esports_lol: 英雄联盟(LPL/LCK/MSI/S赛)
- esports_other: 其他电竞(Dota2/CSGO/王者荣耀等)
- general_sports: 综合体育(赛车/网球/田径/综合赛事)
- other: 其他/花絮(娱乐/八卦/训练照/生活)

判断标准:球员交易/签约/伤病/退役、教练变动、重大比赛结果、纪录达成、争议事件 算大新闻;
普通花絮、训练照、情怀回顾、球迷投票 不算。
注意:若正文为空(抓取失败),仅根据标题判断并生成简短摘要。"""


def _fallback(item: NewsItem, reason: str) -> dict:
    """LLM 不可用时的降级:important=True,headline=原标题,category 按源级映射兜底。"""
    return {
        "important": True,
        "score": 5,
        "category": _infer_category_by_source(item),
        "headline": item.title,
        "summary": "",
        "url": item.url,
        "source": item.source,
        "section": item.section,
        "replies": item.replies,
        "likes": item.likes,
        "_fallback": reason,
    }


def _infer_category_by_source(item: NewsItem) -> str:
    """无 LLM 时按来源+板块粗略推断类型。"""
    if item.source == "hupu":
        section_map = {
            "all-nba": "nba",
            "all-cba": "cba",
            "all-csl": "football_china",
            "all-gambia": "general_sports",
        }
        return section_map.get(item.section, "other")
    if item.source == "zhibo8":
        # 直播吧 section 是 nba/zuqiu/game
        return {
            "nba": "nba",       # 直播吧 nba 混了 CBA,默认 nba
            "zuqiu": "football_overseas",  # 直播吧足球默认海外
            "game": "esports_other",
        }.get(item.section, "other")
    return "other"


def llm_judge(item: NewsItem, state: State, llm_daily_limit: int = 50) -> dict:
    """对单条候选调 LLM 判断。返回标准化 dict。"""
    api_key = os.getenv("LLM_API_KEY", "").strip()

    # 软限流
    if state.daily_llm_count() >= llm_daily_limit:
        return _fallback(item, "llm_daily_limit_reached")

    # 无 API key
    if not api_key:
        return _fallback(item, "no_api_key")

    velocity = state.get_velocity(item.url)
    v_str = f"{velocity[0]:.1f}" if velocity else "N/A"

    prompt = PROMPT_TEMPLATE.format(
        title=item.title,
        source=item.source,
        section=item.section,
        replies=item.replies,
        likes=item.likes,
        velocity=v_str,
        content=item.content or "(无正文)",
    )

    state.mark_llm_call()

    try:
        r = requests.post(
            LLM_API_BASE,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": "You are a strict JSON producer. Only output JSON, no markdown."},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.3,
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[llm] HTTP {r.status_code}: {r.text[:200]}")
            return _fallback(item, f"http_{r.status_code}")

        data = r.json()
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if not content:
            return _fallback(item, "empty_response")

        parsed = _parse_llm_json(content)
        # category 校验:不在白名单内则按源级映射兜底
        valid_categories = {
            "nba", "cba", "football_overseas", "football_china",
            "esports_lol", "esports_other", "general_sports", "other",
        }
        category = str(parsed.get("category", "")).strip().lower()
        if category not in valid_categories:
            category = _infer_category_by_source(item)
        return {
            "important": bool(parsed.get("important", True)),
            "score": int(parsed.get("score", 5)),
            "category": category,
            "headline": str(parsed.get("headline", item.title))[:100],
            "summary": str(parsed.get("summary", ""))[:200],
            "url": item.url,
            "source": item.source,
            "section": item.section,
            "replies": item.replies,
            "likes": item.likes,
        }
    except (requests.RequestException, json.JSONDecodeError, ValueError, KeyError, IndexError) as e:
        print(f"[llm] {type(e).__name__}: {e}")
        return _fallback(item, type(e).__name__)
