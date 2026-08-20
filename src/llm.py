"""GLM-4-Flash 调用 + 降级 + 软限流。"""
from __future__ import annotations

import json
import os
from typing import Any

import requests

from scrapers import NewsItem
from state import State

GLM_API_BASE = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
GLM_MODEL = "glm-4-flash"
TIMEOUT = 15

PROMPT_TEMPLATE = """你是体育新闻编辑。判断下列新闻是否值得作为"大新闻"推送给体育爱好者。

标题:{title}
来源:{source} - {section}
热度指标:回复 {replies} / 亮 {likes}
增速:回复 {velocity}/分钟

返回 JSON(严格,无其他文字):
{{
  "important": true 或 false,
  "score": 1-10 整数,
  "headline": "20字内精炼标题",
  "summary": "30字内一句话摘要"
}}

判断标准:球员交易/签约/伤病/退役、教练变动、重大比赛结果、纪录达成、争议事件 算大新闻;
普通花絮、训练照、情怀回顾、球迷投票 不算。"""


def _fallback(item: NewsItem, reason: str) -> dict:
    """LLM 不可用时的降级:important=True,headline=原标题前 32 字。"""
    return {
        "important": True,
        "score": 5,
        "headline": item.title[:32],
        "summary": "",
        "url": item.url,
        "source": item.source,
        "section": item.section,
        "replies": item.replies,
        "likes": item.likes,
        "_fallback": reason,
    }


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
    )

    state.mark_llm_call()

    try:
        r = requests.post(
            GLM_API_BASE,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": GLM_MODEL,
                "messages": [
                    {"role": "system", "content": "You are a strict JSON producer."},
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

        parsed = json.loads(content)
        return {
            "important": bool(parsed.get("important", True)),
            "score": int(parsed.get("score", 5)),
            "headline": str(parsed.get("headline", item.title[:32]))[:32],
            "summary": str(parsed.get("summary", ""))[:60],
            "url": item.url,
            "source": item.source,
            "section": item.section,
            "replies": item.replies,
            "likes": item.likes,
        }
    except (requests.RequestException, json.JSONDecodeError, ValueError, KeyError, IndexError) as e:
        print(f"[llm] {type(e).__name__}: {e}")
        return _fallback(item, type(e).__name__)
