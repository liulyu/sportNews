"""Server酱 推送(单条 + 批量合并)。"""
from __future__ import annotations

import os
from typing import Any

import requests

from state import State

SERVERCHAN_API = "https://sctapi.ftqq.com/{key}.send"
TIMEOUT = 15

# Server酱 title 上限 32 字,desp 上限约 32KB(保守按 25000 字切)
TITLE_MAX = 32
DESP_MAX_CHARS = 25000


def _ensure_summary(item: dict) -> str:
    """对单条 item:保证 summary 非空(LLM失败/无摘要兜底)。"""
    headline = item.get("headline", "")
    section = item.get("section", "")
    replies = item.get("replies", 0)
    likes = item.get("likes", 0)
    summary = (item.get("summary") or "").strip()
    if summary:
        return summary
    content = (item.get("content") or "").strip()
    if content:
        compact = " ".join(content.split())
        return compact[:150] + ("…" if len(compact) > 150 else "")
    heat_parts = []
    if replies:
        heat_parts.append(f"{replies}回复")
    if likes:
        heat_parts.append(f"{likes}亮")
    heat_str = "、".join(heat_parts) or "热度未知"
    return f"{headline}。({heat_str}，{section})"[:150]


def _heat_str(item: dict) -> str:
    source = item.get("source", "")
    replies = item.get("replies", 0)
    likes = item.get("likes", 0)
    if source == "hupu":
        return f"虎扑 {replies}回复 {likes}亮"
    if source == "zhibo8":
        if replies:
            return f"直播吧 {replies}评论"
        return "直播吧"
    return f"{replies}回复 {likes}亮"


def _build_desp(item: dict) -> str:
    """单条完整 desp(用于单条推送 / batch 模式只有 1 条时)。"""
    headline = item.get("headline", "")
    url = item.get("url", "")
    fetched_at = item.get("fetched_at", "")
    source = item.get("source", "")
    section = item.get("section", "")
    summary = _ensure_summary(item)
    return (
        f"## {headline}\n\n{summary}\n\n"
        f"📊 热度:{_heat_str(item)}\n"
        f"🔗 [查看原文]({url})\n\n"
        f"来源:{source} · {section}"
        + (f" · {fetched_at}" if fetched_at else "")
    )


def _build_batch_desp(items: list[dict], header_note: str | None = None) -> str:
    """多条拼接成一条 desp:按编号 ### 分节。顶部可加 header_note 说明段(晨间汇总用)。"""
    if len(items) == 1:
        body = _build_desp(items[0])
        if header_note:
            return header_note.rstrip() + "\n\n---\n\n" + body
        return body

    blocks: list[str] = []
    if header_note:
        blocks.append(header_note.rstrip() + "\n\n")
    blocks.append(f"# 体育大新闻汇总 · {len(items)} 条\n")
    for idx, it in enumerate(items, start=1):
        headline = it.get("headline", "")
        url = it.get("url", "")
        source = it.get("source", "")
        section = it.get("section", "")
        fetched_at = it.get("fetched_at", "")
        summary = _ensure_summary(it)
        block = (
            f"\n---\n\n### {idx}. {headline}\n\n"
            f"{summary}\n\n"
            f"📊 {_heat_str(it)}　｜　"
            f"来源:{source} · {section}"
            + (f" · {fetched_at}" if fetched_at else "")
            + f"\n\n🔗 [查看原文]({url})\n"
        )
        blocks.append(block)
    desp = "".join(blocks)
    if len(desp) > DESP_MAX_CHARS:
        desp = desp[:DESP_MAX_CHARS] + "\n\n⚠️ 内容过长,已截断。"
    return desp


def _build_batch_title(items: list[dict], title_prefix: str | None = None) -> str:
    """批量消息 title:N=1 用单条标题;否则默认'体育大新闻汇总·N条'。
    title_prefix 非空时用它代替"体育大新闻汇总"默认文案(晨间汇总场景)。"""
    if len(items) == 1:
        title = items[0].get("headline", "") or items[0].get("title", "")
        return title[:TITLE_MAX]
    if title_prefix:
        title = f"{title_prefix} · {len(items)} 条"
    else:
        title = f"体育大新闻汇总 · {len(items)} 条"
    first = items[0].get("headline", "") if items else ""
    if first and len(title) < TITLE_MAX:
        hint = "｜" + first[: TITLE_MAX - len(title) - 2]
        title = (title + hint)[:TITLE_MAX]
    return title


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


def _mark_batch_results(items: list[dict], state: State, status_str: str) -> None:
    """批量把 items 标 pushed/attempted。"""
    for it in items:
        normalized = it.get("_normalized", "")
        state.mark_pushed(it.get("url", ""), normalized, status_str)


def push_to_serverchan_batch(
    items: list[dict],
    state: State,
    sendkey: str | None = None,
    daily_limit: int = 5,
    title_prefix: str | None = None,
    header_note: str | None = None,
) -> str:
    """多条合并成一条 Server酱 消息推送。

    title_prefix: 晨间汇总用(如 "🌅 晨间汇总"),默认"体育大新闻汇总"
    header_note: desp 顶部附加说明(如覆盖时段)

    返回状态同 push_to_serverchan:
    - "pushed": 成功
    - "attempted": 失败但已记录
    - "quota_exhausted": 配额耗尽
    - "no_key": 未配置 sendkey
    - "empty": items 为空,未推送
    """
    if not items:
        return "empty"

    key = (sendkey or os.getenv("SERVERCHAN_KEY", "")).strip()
    if not key:
        print("[push] 未配置 SERVERCHAN_KEY,跳过推送")
        return "no_key"

    # 调用前检查日限:批量条数整体占用 1 条日限(因为 1 次 Server酱 API)
    if state.daily_pushed_count() >= daily_limit:
        print(f"[push] 已达 Server酱 日限 ({daily_limit}),跳过(本批 {len(items)} 条)")
        return "quota_exhausted"

    title = _build_batch_title(items, title_prefix=title_prefix)
    desp = _build_batch_desp(items, header_note=header_note)

    status, body = _do_post(key, title, desp)
    if 500 <= status < 600:
        print(f"[push] HTTP {status},重试 1 次")
        status, body = _do_post(key, title, desp)

    if status == 200 and body and body.get("code") == 0:
        # 成功:所有 items 一次性 mark pushed(只占 daily_pushed 1 个日限配额)
        _mark_batch_results(items, state, "pushed")
        if len(items) == 1:
            print(f"[push] 推送成功(单条合并): {title}")
        else:
            titles = "、".join(it.get("headline", "")[:12] for it in items[:3])
            if len(items) > 3:
                titles += "…"
            print(f"[push] 推送成功(批量 {len(items)} 条合并): {titles}")
        return "pushed"

    if status == 200 and body and body.get("code") != 0:
        print(f"[push] Server酱 返回 code={body.get('code')} msg={body.get('message', '')}")
        _mark_batch_results(items, state, "attempted")
        return "quota_exhausted"

    if status == -1 or 400 <= status < 500:
        print(f"[push] HTTP {status} 或网络异常,批量标 attempted")
        _mark_batch_results(items, state, "attempted")
        return "attempted"

    if 500 <= status < 600:
        print(f"[push] HTTP {status} 重试后仍失败,批量标 attempted")
        _mark_batch_results(items, state, "attempted")
        return "attempted"

    _mark_batch_results(items, state, "attempted")
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
