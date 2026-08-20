"""主流程入口:抓取 → 过滤 → LLM → 推送 → 持久化。"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import yaml

# 让 `python src/main.py` 可直接运行(添加项目根到 sys.path)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers import fetch_all, fetch_hupu_detail, fetch_zhibo8_detail  # noqa: E402
from filter import filter_and_rank, normalize_title  # noqa: E402
from llm import llm_judge  # noqa: E402
from push import push_to_serverchan, push_test  # noqa: E402
from state import State, save_and_commit, BEIJING_TZ  # noqa: E402

CONFIG_PATH = "config.yaml"


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _run_heartbeat(config: dict, state: State) -> int:
    """心跳作业:检查监控健康,必要时推送告警。"""
    sendkey = os.getenv("SERVERCHAN_KEY", "").strip()
    now = datetime.now(BEIJING_TZ)

    last_success = state.health.get("last_success", "")
    fail_count = state.health.get("consecutive_scrape_failures", 0)

    alerts: list[str] = []
    if not last_success:
        alerts.append("从未成功运行过监控")
    else:
        try:
            ls = datetime.fromisoformat(last_success)
            age_min = (now - ls).total_seconds() / 60
            if age_min > 30:
                alerts.append(f"监控已 {int(age_min)} 分钟未成功运行")
        except ValueError:
            alerts.append("last_success 时间戳解析失败")

    if fail_count >= 3:
        alerts.append(f"抓取解析连续失败 {fail_count} 次,可能 HTML 结构变更")

    today = now.strftime("%Y-%m-%d")
    pushed_today = state.daily_pushed.get(today, 0)

    if alerts:
        msg = "🚨 监控告警\n\n" + "\n".join(alerts)
        item = {
            "headline": "监控告警",
            "summary": msg,
            "url": "https://github.com/",
            "source": "system",
            "section": "heartbeat",
            "replies": 0,
            "likes": 0,
            "fetched_at": now.isoformat(timespec="seconds"),
        }
        if sendkey:
            push_to_serverchan(item, state, "system-heartbeat", sendkey)
        else:
            print("[heartbeat] 未配置 SERVERCHAN_KEY")
            print(msg)
    else:
        msg = f"✅ 监控正常\n今日已推送 {pushed_today} 条"
        item = {
            "headline": "每日报告",
            "summary": msg,
            "url": "https://github.com/",
            "source": "system",
            "section": "heartbeat",
            "replies": 0,
            "likes": 0,
            "fetched_at": now.isoformat(timespec="seconds"),
        }
        if sendkey and pushed_today < 5:
            # 只在还有配额时发送每日报告
            push_to_serverchan(item, state, "system-heartbeat", sendkey)
        else:
            print(f"[heartbeat] {msg}")
    return 0


def run_monitor(config: dict, state: State, dry_run: bool = False) -> int:
    state.mark_run_started()

    # 1. 抓取
    print(f"[main] 开始抓取 ...")
    items = fetch_all(config)
    print(f"[main] 抓取到 {len(items)} 条")

    # 2. Sanity check
    if len(items) < 5:
        state.mark_scrape_failure(f"仅抓到 {len(items)} 条")
    else:
        state.mark_scrape_success()

    # 8. 更新热度快照(对所有抓到的 item,无论是否通过)
    # 这是下次速度计算的基础,必须先于过滤
    # 虎扑有 replies+likes,直播吧移动版有 replies(评论数),likes=0
    for it in items:
        if it.source in ("hupu", "zhibo8"):
            state.update_hotness(it.url, it.replies, it.likes)

    # 3. 过滤
    rules = config.get("rules", {})
    candidates = filter_and_rank(
        items,
        state,
        rules,
        config.get("instant_keywords", []),
        config.get("keyword_boost", []),
        config.get("keyword_block", []),
    )
    print(f"[main] 过滤后候选 {len(candidates)} 条")

    if dry_run:
        for c in candidates:
            print(
                f"[候选] {c.source}/{c.section} | replies={c.replies} likes={c.likes} "
                f"| {c.title[:50]} | {c.url}"
            )
        return 0

    if not candidates:
        print("[main] 无候选,跳过 LLM 与推送")
        return 0

    # 4. LLM 总结(对所有候选调用,精炼标题+生成摘要)
    #    先抓详情页正文,再调 LLM,这样 summary 基于正文而非标题改写
    llm_limit = rules.get("llm_daily_limit", 150)
    top = candidates  # 全部候选都调 LLM
    for c in top:
        try:
            if c.source == "hupu":
                c.content = fetch_hupu_detail(c.url)
            elif c.source == "zhibo8":
                c.content = fetch_zhibo8_detail(c.url)
            if c.content:
                print(f"[main] 详情页正文 {len(c.content)}字: {c.url}")
            else:
                print(f"[main] 详情页正文为空,LLM 仅基于标题判断: {c.url}")
            time.sleep(0.5)  # 礼貌延迟
        except Exception as e:
            print(f"[main] 抓详情页失败: {type(e).__name__}: {e}")
    summaries = [llm_judge(c, state, llm_limit) for c in top]

    # 5. 推送
    daily_limit = rules.get("daily_push_limit", 5)
    for s in summaries:
        if not s.get("important"):
            print(f"[main] LLM 判定不重要,跳过: {s.get('headline')}")
            continue
        if state.daily_pushed_count() >= daily_limit:
            print("[main] 已达 Server酱 日限,停止推送")
            break
        normalized = normalize_title(s.get("headline", "") + s.get("summary", ""))
        status = push_to_serverchan(s, state, normalized, daily_limit=daily_limit)
        if status == "quota_exhausted":
            print("[main] Server酱 配额耗尽,停止本次推送")
            break

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="体育大新闻自动推送")
    parser.add_argument("--test-push", action="store_true", help="发送测试推送,验证 Server酱 配置")
    parser.add_argument("--dry-run", action="store_true", help="只打印候选,不推送")
    parser.add_argument("--heartbeat", action="store_true", help="心跳作业:健康检查 + 每日报告")
    args = parser.parse_args()

    config = load_config(CONFIG_PATH)
    state = State.load("data/state.json")

    if args.test_push:
        ok = push_test()
        return 0 if ok else 1

    if args.heartbeat:
        rc = _run_heartbeat(config, state)
        # 心跳也要保存 state(mark_pushed 的状态需要落盘)
        state.cleanup()
        save_and_commit(state, "data/state.json")
        return rc

    try:
        rc = run_monitor(config, state, dry_run=args.dry_run)
    finally:
        state.cleanup()
        save_and_commit(state, "data/state.json")
    return rc


if __name__ == "__main__":
    sys.exit(main())
