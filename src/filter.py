"""规则过滤 + 跨源去重 + 蹿升速度 + 节流截断。"""
from __future__ import annotations

import re
from typing import List

from scrapers import NewsItem
from state import State

# 标题归一化:去 [流言板]/[官方] 等前缀、记者名、标点
TITLE_PREFIX_RE = re.compile(
    r"^\s*(\[流言板\]|\[官方\]|\[公告\]|\[ rumours \]|官方|美记|名记|快讯|最新|Shams|Haynes|Stein|Fischer|Amick|Siegel|BR|TA|ESPN|Woj|Charania|引述)",
    re.IGNORECASE,
)
PUNCT_RE = re.compile(r"[\s\W_,，。.!！?？:：;；\"'“”‘’()（）\[\]【】\-—·、/\\|]+")


def normalize_title(title: str) -> str:
    """去前缀 + 去标点,返回小写串。"""
    t = title.strip()
    # 多次去前缀(可能套娃)
    for _ in range(3):
        new_t = TITLE_PREFIX_RE.sub("", t)
        if new_t == t:
            break
        t = new_t
    t = PUNCT_RE.sub("", t)
    return t.lower()


def _ngrams(s: str, n: int = 2) -> set:
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def jaccard(a: str, b: str, n: int = 2) -> float:
    sa, sb = _ngrams(a, n), _ngrams(b, n)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _matches_any(text: str, keywords: list[str]) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in keywords)


def _score_item(item: NewsItem) -> int:
    """综合热度得分,用于排序。"""
    return item.replies + item.likes * 5


def filter_and_rank(
    items: List[NewsItem],
    state: State,
    rules: dict,
    instant_keywords: list[str],
    keyword_boost: list[str],
    keyword_block: list[str],
) -> List[NewsItem]:
    """返回通过过滤的候选列表(已排序、已截断)。"""
    if not items:
        return []

    cold = state.is_cold_start()
    cold_mul = float(rules.get("cold_start_multiplier", 2.0))
    min_replies = rules.get("min_replies_hupu", 200)
    min_likes = rules.get("min_likes_hupu", 40)
    v_replies = rules.get("velocity_replies_per_min", 8)
    v_likes = rules.get("velocity_likes_per_min", 2)
    max_push = rules.get("max_push_per_run", 2)
    daily_limit = rules.get("daily_push_limit", 5)
    dedup_hours = rules.get("dedup_window_hours", 6)
    cross_threshold = rules.get("cross_source_dedup_threshold", 0.6)

    # 应用冷启动倍率
    eff_min_replies = min_replies * (cold_mul if cold else 1.0)
    eff_min_likes = min_likes * (cold_mul if cold else 1.0)

    daily_pushed_so_far = state.daily_pushed_count()

    # 候选分类
    instant_candidates: list[tuple[NewsItem, str]] = []  # (item, normalized)
    hot_candidates: list[tuple[NewsItem, str, int]] = []  # + score
    velocity_candidates: list[tuple[NewsItem, str, int]] = []  # + score

    # 本次运行内已选 normalized(防止同源同时报同事件)
    seen_normalized_this_run: list[str] = []

    # 即时通道当日上限 2 条
    instant_used_today = state.daily_instant_count()
    instant_budget = max(0, 2 - instant_used_today)

    for item in items:
        # 黑名单
        if _matches_any(item.title, keyword_block):
            continue

        # URL 去重(包括 attempted 状态的,防止对端已收过)
        if state.is_url_pushed_recently(item.url, dedup_hours):
            continue

        normalized = normalize_title(item.title)

        # 跨源去重:与历史 pushed_normalized 比对
        if state.is_normalized_pushed(normalized, cross_threshold, jaccard):
            continue
        # 与本次运行内已选比对
        if any(jaccard(normalized, n) >= cross_threshold for n in seen_normalized_this_run):
            continue

        # 即时通道
        if _matches_any(item.title, instant_keywords) and instant_budget > 0:
            instant_candidates.append((item, normalized))
            seen_normalized_this_run.append(normalized)
            # 即时通道直接进,不走热度
            continue

        # 虎扑走热度/速度路径
        if item.source == "hupu":
            score = _score_item(item)

            # 加权关键词:阈值 ×0.5
            if _matches_any(item.title, keyword_boost):
                this_min_replies = eff_min_replies * 0.5
                this_min_likes = eff_min_likes * 0.5
            else:
                this_min_replies = eff_min_replies
                this_min_likes = eff_min_likes

            # 绝对热度:首次见到但热度 ≥ 阈值 ×2 → 直推
            # 否则正常阈值
            velocity = state.get_velocity(item.url)
            has_history = bool(state.get_last_hotness(item.url))

            if not has_history:
                # 首次见到:需 ≥ 阈值 ×2 才直接推
                if item.replies >= this_min_replies * 2 or item.likes >= this_min_likes * 2:
                    hot_candidates.append((item, normalized, score))
                    seen_normalized_this_run.append(normalized)
                    continue
                # 否则跳过(下次有历史再判速度)
                continue

            # 有历史:判绝对热度或蹿升速度
            abs_hot = (
                item.replies >= this_min_replies or item.likes >= this_min_likes
            )
            rising = False
            if velocity:
                v_r, v_l = velocity
                if v_r >= v_replies or v_l >= v_likes:
                    rising = True

            if abs_hot:
                hot_candidates.append((item, normalized, score))
                seen_normalized_this_run.append(normalized)
            elif rising:
                velocity_candidates.append((item, normalized, score))
                seen_normalized_this_run.append(normalized)

        # 直播吧无热度数据:只走即时通道(已在上文处理)
        # 此处 item 被丢弃

    # 排序:即时 > 绝对热度 ×2 > 蹿升 > 普通绝对热度
    # 用 (priority_class, score) 排序,priority_class 越小越优先
    ranked: list[tuple[int, int, NewsItem, str]] = []
    for it, n in instant_candidates:
        ranked.append((0, _score_item(it), it, n))
    for it, n, s in hot_candidates:
        ranked.append((1, s, it, n))
    for it, n, s in velocity_candidates:
        ranked.append((2, s, it, n))

    ranked.sort(key=lambda x: (x[0], -x[1]))

    # 节流截断
    result: List[NewsItem] = []
    pushed_in_run = 0
    for _, _, it, _ in ranked:
        if pushed_in_run >= max_push:
            break
        # 当日已推 ≥ daily_limit - 1 时,只允许 1 条且必须是即时通道或热度 ×3
        if daily_pushed_so_far >= daily_limit - 1:
            if pushed_in_run >= 1:
                break
            is_instant = any(it.url == x[0].url for x in instant_candidates)
            if not is_instant and _score_item(it) < eff_min_replies * 3:
                continue
        result.append(it)
        pushed_in_run += 1

    return result
