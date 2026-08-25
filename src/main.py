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

from scrapers import NewsItem, fetch_all, fetch_hupu_detail, fetch_zhibo8_detail  # noqa: E402
from filter import filter_and_rank, normalize_title  # noqa: E402
from llm import llm_judge  # noqa: E402
from push import push_to_serverchan, push_to_serverchan_batch, push_test  # noqa: E402
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

    # 3. 模式判断(必须先于过滤,决定是否跳过新鲜度窗口等逻辑)
    rules = config.get("rules", {})
    quiet_start = int(rules.get("quiet_start_hour", 23))
    quiet_end = int(rules.get("quiet_end_hour", 7))
    morning_cap = int(rules.get("morning_queue_cap", 50))
    mode = state.get_run_mode(quiet_start, quiet_end)
    now_beijing = datetime.now(BEIJING_TZ)
    print(f"[main] 运行模式: {mode} (北京 {now_beijing.strftime('%H:%M')})")

    # 4. 准备过滤输入(MORNING 模式下把 night_queue 先转成 NewsItem 合并到 items 再过滤,
    #    这样跨源标题相似度去重、关键词屏蔽等会同时作用于夜间新闻和新抓的新闻)
    filter_input: list[NewsItem] = list(items)
    if mode == "MORNING" and state.night_queue:
        existing = {it.url for it in filter_input}
        for d in state.night_queue:
            if d.get("url") in existing:
                continue
            filter_input.append(NewsItem(
                source=d.get("source", ""),
                section=d.get("section", ""),
                title=d.get("title", ""),
                url=d.get("url", ""),
                replies=int(d.get("replies", 0) or 0),
                likes=int(d.get("likes", 0) or 0),
                fetched_at=d.get("fetched_at", ""),
                content="",
            ))
            existing.add(d.get("url", ""))
        print(f"[main] 晨间汇总:夜间队列 {len(state.night_queue)} 条 + 本次抓取 {len(items)} 条 → 合并 {len(filter_input)} 条再过滤")

    # 5. 过滤
    #   - QUIET:  跳过新鲜度 + 阈值 ×0.5(放宽,夜间回帖慢,让新事件能入队)
    #   - MORNING:跳过新鲜度(夜间新闻已经在队列里挂了 N 小时,不能用 2h 窗口拦)
    #   - NORMAL: 正常新鲜度 + 正常阈值
    skip_fresh = mode in ("QUIET", "MORNING")
    thr_mul = 0.5 if mode == "QUIET" else 1.0
    candidates = filter_and_rank(
        filter_input,
        state,
        rules,
        config.get("keyword_boost", []),
        config.get("keyword_block", []),
        skip_freshness=skip_fresh,
        threshold_multiplier=thr_mul,
    )
    print(f"[main] 过滤后候选 {len(candidates)} 条 (阈值倍率={thr_mul}, skip_freshness={skip_fresh})")

    # === MORNING 模式:只保留"静音时段内首次出现"或"昨晚 QUIET 已入队"的新闻
    #    防止 7 点首页里带进来的"昨天白天旧热帖"占了早报版面(它们应走白天 NORMAL)
    if mode == "MORNING":
        # 计算本周期静音起点/终点(北京时间)
        # 规则: 今天的静音期是 [昨晚 quiet_start 点, 今早 quiet_end 点)
        morning_now = now_beijing
        q_start = morning_now.replace(
            hour=quiet_start % 24, minute=0, second=0, microsecond=0
        )
        if q_start > morning_now:
            q_start -= timedelta(days=1)
        q_end = morning_now.replace(
            hour=quiet_end % 24, minute=0, second=0, microsecond=0
        )
        if q_end <= q_start:
            q_end += timedelta(days=1)
        # 夜间队列 URL 白名单(昨晚已判候选的)
        night_queue_urls = {d.get("url") for d in state.night_queue if d.get("url")}
        kept: list[NewsItem] = []
        from_night_url = 0
        from_first_seen = 0
        from_items = len(candidates)
        for c in candidates:
            from_queue = c.url in night_queue_urls
            first_seen = state.get_first_seen(c.url)
            within_quiet = first_seen is not None and q_start <= first_seen <= q_end
            # 首次见到的 URL(今次 7 点 run 才第一次进入 hotness_history)
            #   → 虽然在窗口外,但它是 7 点刚出的新闻,应该被早报收录(否则要等 8 点 NORMAL)
            brand_new = first_seen is None
            if from_queue:
                from_night_url += 1
                kept.append(c)
            elif within_quiet:
                from_first_seen += 1
                kept.append(c)
            elif brand_new:
                kept.append(c)
                from_first_seen += 1  # 当作"今早刚发生的"算入夜间时段
            # else:首次出现时间在昨天白天 → 丢弃,走 NORMAL
        print(
            f"[main] 晨间汇总候选过滤:过滤前 {from_items} 条,"
            f" 属于夜间队列 {from_night_url} 条,"
            f" 静音窗口首次出现/今早刚出 {from_first_seen} 条,"
            f" 最终保留 {len(kept)} 条(丢弃 {from_items - len(kept)} 条属于昨天白天的旧热帖)"
        )
        candidates = kept

    if dry_run:
        for c in candidates:
            print(
                f"[候选] {c.source}/{c.section} | replies={c.replies} likes={c.likes} "
                f"| {c.title[:50]} | {c.url}"
            )
        print(f"[dry-run] night_queue 当前 {len(state.night_queue)} 条")
        return 0

    # === 模式 1: QUIET 静音(23:00 ~ 06:59) ===
    if mode == "QUIET":
        if candidates:
            state.enqueue_night_candidates(candidates, morning_cap)
            print(f"[main] 静音模式:入队 {len(candidates)} 条,队列当前 {len(state.night_queue)} 条(阈值减半放宽)")
        else:
            print("[main] 静音模式:无新候选")
        return 0

    if not candidates:
        print("[main] 无候选,跳过 LLM 与推送")
        return 0

    # 6. LLM 总结(抓详情页 + 调 LLM)
    llm_limit = rules.get("llm_daily_limit", 150)
    for c in candidates:
        try:
            if c.source == "hupu":
                c.content = fetch_hupu_detail(c.url)
            elif c.source == "zhibo8":
                c.content = fetch_zhibo8_detail(c.url)
            if c.content:
                pass
            else:
                print(f"[main] 详情页正文为空,LLM 仅基于标题判断: {c.url}")
            time.sleep(0.5)  # 礼貌延迟
        except Exception as e:
            print(f"[main] 抓详情页失败: {type(e).__name__}: {e}")
    summaries = [llm_judge(c, state, llm_limit) for c in candidates]

    # 补全 content / fetched_at
    for c, s in zip(candidates, summaries):
        s.setdefault("content", c.content or "")
        s.setdefault("fetched_at", c.fetched_at or "")
        s.setdefault("url", c.url)
        s.setdefault("source", c.source)
        s.setdefault("section", c.section)
        s.setdefault("replies", c.replies)
        s.setdefault("likes", c.likes)

    # 6. 按 important 收集
    daily_limit = rules.get("daily_push_limit", 5)
    batch: list[dict] = []
    for s in summaries:
        if not s.get("important"):
            print(f"[main] LLM 判定不重要,跳过: {s.get('headline')}")
            continue
        if state.daily_pushed_count() >= daily_limit and not batch:
            print("[main] 已达 Server酱 日限,停止推送")
            break
        s["_normalized"] = normalize_title(s.get("headline", "") + s.get("summary", ""))
        batch.append(s)

    # MORNING 模式下:不受 max_push_per_run 限制,全部打包发
    if mode != "MORNING":
        max_push = rules.get("max_push_per_run", 10)
        if len(batch) > max_push:
            print(f"[main] 候选 {len(batch)} 条超过 max_push_per_run={max_push},截断取前 {max_push}")
            batch = batch[:max_push]

    if not batch:
        print("[main] 无需要推送的重要新闻")
    else:
        # 7. 推送
        if mode == "MORNING":
            window_str = state.quiet_window_string(quiet_start, quiet_end)
            title_prefix = str(rules.get("morning_title_prefix", "🌞 早报")).strip()
            morning_header = str(rules.get("morning_header", "🌞 早报")).strip()
            header_note = (
                f"**{morning_header}**\n\n"
                f"📅 覆盖时段: {window_str}\n"
                f"📋 共 {len(batch)} 条,已按热度排序"
            )
            print(f"[main] 晨间汇总推送 {len(batch)} 条 → 合并成 1 条消息(覆盖: {window_str})")
        else:
            header_note = None
            title_prefix = None
            print(f"[main] 批量推送 {len(batch)} 条 → 合并成 1 条消息")

        status = push_to_serverchan_batch(
            batch, state,
            daily_limit=daily_limit,
            title_prefix=title_prefix,
            header_note=header_note,
        )
        if mode == "MORNING":
            if status == "pushed":
                # 只有真正推送成功时才标记"今日晨间汇总已发"并清空队列
                state.mark_morning_digest_sent()
                state.clear_night_queue()
                print(f"[main] 晨间汇总已成功发送,队列清空,今日不再二次发送晨间汇总")
            else:
                # 失败(网络/配额/无key等):保留队列 + 不写日期标记,下次 run 仍当 MORNING 重试
                print(f"[main] 晨间汇总推送失败(status={status}),保留队列等待下次重试,不清空")
        if status == "quota_exhausted":
            print("[main] Server酱 配额耗尽,后续批次不再推送")

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
