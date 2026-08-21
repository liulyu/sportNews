"""Server酱 推送(区分 pushed/attempted)。"""
from __future__ import annotations

import os
from typing import Any

import requests

from state import State

SERVERCHAN_API = "https://sctapi.ftqq.com/{key}.send"
TIMEOUT = 15


def _build_desp(item: dict) -> str:
    source = item.get("source", "")
    section = item.get("section", "")
    replies = item.get("replies", 0)
    likes = item.get("likes", 0)
    headline = item.get("headline", "")
    summary = (item.get("summary") or "").strip()
    url = item.get("url", "")
    fetched_at = item.get("fetched_at", "")
    content = (item.get("content") or "").strip()

    heat_line = "📊 热度:"
    if source == "hupu":
        heat_line += f"虎扑 {replies}回复 {likes}亮"
    elif source == "zhibo8":
        heat_line += "直播吧"
        if replies:
            heat_line += f" {replies}评论"
    else:
        heat_line += f"{replies}回复 {likes}亮"

    # 第二层兜底:如果 summary 空,用 content 截 150 字;再空就用标题+板块说明
    if not summary:
        if content:
            compact = " ".join(content.split())
            summary = compact[:150] + ("…" if len(compact) > 150 else "")
        else:
            heat_parts = []
            if replies:
                heat_parts.append(f"{replies}回复")
            if likes:
                heat_parts.append(f"{likes}亮")
            heat_str = "、".join(heat_parts) or "热度未知"
            summary = f"{headline}。({heat_str}，{section})"

    summary_block = f"\n\n{summary}\n"
    return (
        f"## {headline}\n"
        f"{summary_block}"
        f"\n{heat_line}\n"
        f"🔗 [查看原文]({url})\n"
        f"\n来源:{source} · {section}"
        + (f" · {fetched_at}" if fetched_at else "")
    )


def _do_post(sendkey: str, title: str, desp: str) -> tuple[int, dict | None]:
    """返回 (http_status, json_body 或 None)。"""
    try:
        r = requests.post(
            SERVERCHAN_API.format(key=sendkey),
            data={"title": title, "desp": desp},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"[push] 网络异常: {type(e).__name__}: {e}")
        return -1, None

    body = None
    try:
        body = r.json()
    except ValueError:
        pass
    return r.status_code, body


def push_to_serverchan(
    item: dict,
    state: State,
    normalized: str,
    sendkey: str | None = None,
    daily_limit: int = 5,
) -> str:
    """推送单条消息。

    返回状态:
    - "pushed": 已确认成功
    - "attempted": 尝试过但状态未知(超时/4xx/5xx 重试后仍失败)
    - "quota_exhausted": Server酱 配额耗尽,本次运行应停止
    - "no_key": 未配置 sendkey(本地 dry-run 用)

    daily_limit: Server酱 日推送上限(免费版 5,Turbo 5000),由 main.py 从 config 注入
    """
    key = (sendkey or os.getenv("SERVERCHAN_KEY", "")).strip()
    if not key:
        print("[push] 未配置 SERVERCHAN_KEY,跳过推送")
        return "no_key"

    # 调用前检查日限(由 main.py 从 config.yaml 的 daily_push_limit 注入)
    if state.daily_pushed_count() >= daily_limit:
        print(f"[push] 已达 Server酱 日限 ({daily_limit}),跳过")
        return "quota_exhausted"

    title = (item.get("headline") or item.get("title", ""))[:32]
    desp = _build_desp(item)

    status, body = _do_post(key, title, desp)

    # 5xx 重试 1 次
    if 500 <= status < 600:
        print(f"[push] HTTP {status},重试 1 次")
        status, body = _do_post(key, title, desp)

    if status == 200 and body and body.get("code") == 0:
        state.mark_pushed(item.get("url", ""), normalized, "pushed")
        print(f"[push] 推送成功: {title}")
        return "pushed"

    if status == 200 and body and body.get("code") != 0:
        # 配额耗尽等业务错误
        print(f"[push] Server酱 返回 code={body.get('code')} msg={body.get('message', '')}")
        state.mark_pushed(item.get("url", ""), normalized, "attempted")
        return "quota_exhausted"

    if status == -1 or 400 <= status < 500:
        # 超时 / 4xx:不重试,标 attempted
        print(f"[push] HTTP {status} 或网络异常,标 attempted")
        state.mark_pushed(item.get("url", ""), normalized, "attempted")
        return "attempted"

    if 500 <= status < 600:
        # 重试后仍失败
        print(f"[push] HTTP {status} 重试后仍失败,标 attempted")
        state.mark_pushed(item.get("url", ""), normalized, "attempted")
        return "attempted"

    # 其他未知情况保守标 attempted
    state.mark_pushed(item.get("url", ""), normalized, "attempted")
    return "attempted"


def push_test(sendkey: str | None = None) -> bool:
    """发送一条测试消息,验证 Server酱 配置。"""
    item = {
        "headline": "测试推送",
        "summary": "如果你看到此消息,Server酱 配置成功。",
        "url": "https://github.com/",
        "source": "system",
        "section": "test",
        "replies": 0,
        "likes": 0,
        "fetched_at": "",
    }
    key = (sendkey or os.getenv("SERVERCHAN_KEY", "")).strip()
    if not key:
        print("[push] 未配置 SERVERCHAN_KEY,无法测试")
        return False
    status, body = _do_post(key, item["headline"], _build_desp(item))
    ok = status == 200 and body and body.get("code") == 0
    print(f"[push] 测试推送: {'成功' if ok else '失败'} (HTTP {status})")
    return ok
