"""状态持久化(JSON 文件 + git commit)。"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Any

BEIJING_TZ = timezone(timedelta(hours=8))

DEFAULT_PATH = "data/state.json"

# 保留策略
PUSHED_TTL_HOURS = 24
HOTNESS_KEEP_SNAPSHOTS = 12
DAILY_KEEP_DAYS = 7


def _now_iso() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def _today_key() -> str:
    """北京时间日期 YYYY-MM-DD,作为 daily_* 字典 key。"""
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        # Python 3.11+ fromisoformat 支持时区
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class State:
    def __init__(self) -> None:
        self.last_run: str = ""
        self.health: dict = {
            "last_success": "",
            "consecutive_scrape_failures": 0,
            "scrape_warnings": [],
        }
        self.daily_pushed: dict[str, int] = {}
        self.daily_instant: dict[str, int] = {}
        self.daily_llm_calls: dict[str, int] = {}
        # url -> {"ts": iso, "status": "pushed"|"attempted", "normalized": str}
        self.pushed: dict[str, dict] = {}
        self.pushed_normalized: list[str] = []
        # url -> [{"t": iso, "replies": int, "likes": int}, ...]
        self.hotness_history: dict[str, list] = {}
        # --- 夜间静音 + 晨间汇总 ---
        # 静音期候选队列: [{"title","url","source","section","replies","likes","fetched_at"}, ...]
        self.night_queue: list[dict] = []
        # 今天是否已发晨间汇总(如 "2026-08-22", 空表示未发)
        self.last_morning_digest_date: str = ""

    # ---- 加载/保存 ----
    @classmethod
    def load(cls, path: str = DEFAULT_PATH) -> "State":
        st = cls()
        if not os.path.exists(path):
            return st
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            st.last_run = data.get("last_run", "")
            st.health = data.get("health", st.health)
            st.daily_pushed = data.get("daily_pushed", {})
            st.daily_instant = data.get("daily_instant", {})
            st.daily_llm_calls = data.get("daily_llm_calls", {})
            st.pushed = data.get("pushed", {})
            st.pushed_normalized = data.get("pushed_normalized", [])
            st.hotness_history = data.get("hotness_history", {})
            st.night_queue = data.get("night_queue", [])
            st.last_morning_digest_date = data.get("last_morning_digest_date", "")
        except (OSError, json.JSONDecodeError) as e:
            print(f"[state] 加载 {path} 失败,从空状态开始: {e}")
        return st

    def to_dict(self) -> dict:
        return {
            "last_run": self.last_run,
            "health": self.health,
            "daily_pushed": self.daily_pushed,
            "daily_instant": self.daily_instant,
            "daily_llm_calls": self.daily_llm_calls,
            "pushed": self.pushed,
            "pushed_normalized": self.pushed_normalized,
            "hotness_history": self.hotness_history,
            "night_queue": self.night_queue,
            "last_morning_digest_date": self.last_morning_digest_date,
        }

    def save(self, path: str = DEFAULT_PATH) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    # ---- 状态查询 ----
    def is_cold_start(self) -> bool:
        return not self.pushed

    def daily_pushed_count(self) -> int:
        return self.daily_pushed.get(_today_key(), 0)

    def daily_instant_count(self) -> int:
        return self.daily_instant.get(_today_key(), 0)

    def daily_llm_count(self) -> int:
        return self.daily_llm_calls.get(_today_key(), 0)

    def is_url_pushed_recently(self, url: str, window_hours: int) -> bool:
        info = self.pushed.get(url)
        if not info:
            return False
        ts = _parse_iso(info.get("ts", ""))
        if not ts:
            return False
        age = (datetime.now(BEIJING_TZ) - ts).total_seconds() / 3600
        return age < window_hours

    def is_normalized_pushed(self, normalized: str, threshold: float, jaccard_fn) -> bool:
        """与已推送 normalized 比对,Jaccard ≥ threshold 视为重复。jaccard_fn(a, b) -> float。"""
        for prev in self.pushed_normalized:
            try:
                if jaccard_fn(normalized, prev) >= threshold:
                    return True
            except Exception:
                continue
        return False

    # ---- 状态写入 ----
    def mark_pushed(self, url: str, normalized: str, status: str) -> None:
        ts = _now_iso()
        self.pushed[url] = {"ts": ts, "status": status, "normalized": normalized}
        if status == "pushed":
            if normalized and normalized not in self.pushed_normalized:
                self.pushed_normalized.append(normalized)
            self.daily_pushed[_today_key()] = self.daily_pushed.get(_today_key(), 0) + 1
        elif status == "attempted":
            # 不计入 daily_pushed(不占配额),只记 attempted 标记
            pass

    def mark_instant(self) -> None:
        self.daily_instant[_today_key()] = self.daily_instant.get(_today_key(), 0) + 1

    def mark_llm_call(self) -> None:
        self.daily_llm_calls[_today_key()] = self.daily_llm_calls.get(_today_key(), 0) + 1

    def update_hotness(self, url: str, replies: int, likes: int) -> None:
        self.hotness_history.setdefault(url, []).append(
            {"t": _now_iso(), "replies": replies, "likes": likes}
        )

    def get_last_hotness(self, url: str) -> dict | None:
        hist = self.hotness_history.get(url, [])
        return hist[-1] if hist else None

    def get_velocity(self, url: str) -> tuple[float, float] | None:
        """返回 (replies_per_min, likes_per_min) 或 None(无 2 个快照)。"""
        hist = self.hotness_history.get(url, [])
        if len(hist) < 2:
            return None
        prev = hist[-2]
        cur = hist[-1]
        prev_ts = _parse_iso(prev.get("t", ""))
        cur_ts = _parse_iso(cur.get("t", ""))
        if not prev_ts or not cur_ts:
            return None
        dt_min = (cur_ts - prev_ts).total_seconds() / 60
        if dt_min <= 0:
            return None
        v_r = (cur.get("replies", 0) - prev.get("replies", 0)) / dt_min
        v_l = (cur.get("likes", 0) - prev.get("likes", 0)) / dt_min
        return v_r, v_l

    def get_first_seen(self, url: str) -> datetime | None:
        """该 URL 首次被抓取快照的时间(北京时间)。无记录返回 None。"""
        hist = self.hotness_history.get(url, [])
        if not hist:
            return None
        return _parse_iso(hist[0].get("t", ""))

    def get_age_hours(self, url: str) -> float | None:
        """首次出现距今多少小时。无记录返回 None。"""
        first = self.get_first_seen(url)
        if not first:
            return None
        return (datetime.now(BEIJING_TZ) - first).total_seconds() / 3600

    # ---- 运行模式判断 (静音 / 晨间汇总 / 正常) ----
    @staticmethod
    def _quiet_hours(start: int, end: int) -> set[int]:
        """把 [start, end) 转成小时集合(支持跨午夜)。"""
        hours = set()
        h = start % 24
        end_mod = end % 24
        while True:
            hours.add(h)
            h = (h + 1) % 24
            if h == end_mod:
                break
        return hours

    def get_run_mode(self, quiet_start_hour: int, quiet_end_hour: int) -> str:
        """返回 "QUIET" / "MORNING" / "NORMAL"。"""
        now = datetime.now(BEIJING_TZ)
        today = now.strftime("%Y-%m-%d")
        quiet = self._quiet_hours(quiet_start_hour, quiet_end_hour)
        if now.hour in quiet:
            return "QUIET"
        if self.last_morning_digest_date != today:
            # 非静音时段且今天晨间汇总还没发
            return "MORNING"
        return "NORMAL"

    def quiet_window_string(self, quiet_start_hour: int, quiet_end_hour: int) -> str:
        """返回类似 "2026-08-21 23:00 ~ 2026-08-22 07:08" 的覆盖时段字符串(北京时间)。

        跨午夜时 end_candidate 会自动 +1 天,保证 end >= start。
        """
        now = datetime.now(BEIJING_TZ)
        start_candidate = now.replace(
            hour=quiet_start_hour % 24, minute=0, second=0, microsecond=0
        )
        if start_candidate > now:
            # 起点在未来 → 应该往前推一天(静音期是从昨天这个点开始的)
            start_candidate -= timedelta(days=1)
        end_candidate = now.replace(
            hour=quiet_end_hour % 24, minute=0, second=0, microsecond=0
        )
        # 跨午夜修正:end <= start 说明 end 应该落在下一天
        if end_candidate <= start_candidate:
            end_candidate += timedelta(days=1)
        end = min(now, end_candidate)
        return (
            f"{start_candidate.strftime('%Y-%m-%d %H:%M')}"
            f" ~ {end.strftime('%Y-%m-%d %H:%M')}"
        )

    # ---- 夜间静音队列 ----
    def enqueue_night_candidates(self, items, cap: int) -> int:
        """把候选 NewsItem 列表按 URL 去重并入队。超过 cap 按热度取前 cap。

        返回实际新增了多少条。
        """
        existing_urls = {it.get("url") for it in self.night_queue}
        for it in items:
            if it.url in existing_urls:
                continue
            self.night_queue.append({
                "title": it.title,
                "url": it.url,
                "source": it.source,
                "section": it.section,
                "replies": it.replies,
                "likes": it.likes,
                "fetched_at": it.fetched_at or "",
            })
            existing_urls.add(it.url)
        # 超过 cap 时按热度排序,保留前 cap
        if len(self.night_queue) > cap:
            self.night_queue.sort(
                key=lambda d: d.get("replies", 0) + d.get("likes", 0) * 5, reverse=True
            )
            self.night_queue = self.night_queue[:cap]
        return len(items)

    def clear_night_queue(self) -> None:
        self.night_queue = []

    def mark_morning_digest_sent(self, today: str | None = None) -> None:
        self.last_morning_digest_date = today or _today_key()

    # ---- 健康状态 ----
    def mark_run_started(self) -> None:
        self.last_run = _now_iso()

    def mark_scrape_success(self) -> None:
        self.health["last_success"] = _now_iso()
        self.health["consecutive_scrape_failures"] = 0

    def mark_scrape_failure(self, msg: str = "") -> None:
        self.health["consecutive_scrape_failures"] = (
            self.health.get("consecutive_scrape_failures", 0) + 1
        )
        if msg:
            warns = self.health.setdefault("scrape_warnings", [])
            warns.append(f"{_now_iso()} {msg}")
            self.health["scrape_warnings"] = warns[-10:]

    # ---- 清理 ----
    def cleanup(self) -> None:
        now = datetime.now(BEIJING_TZ)

        # pushed: 超过 PUSHED_TTL_HOURS 删除
        new_pushed = {}
        for url, info in self.pushed.items():
            ts = _parse_iso(info.get("ts", ""))
            if ts and (now - ts).total_seconds() / 3600 < PUSHED_TTL_HOURS:
                new_pushed[url] = info
        self.pushed = new_pushed

        # pushed_normalized: 只保留仍对应 pushed 中的 normalized
        kept_norm = {info.get("normalized") for info in self.pushed.values() if info.get("normalized")}
        self.pushed_normalized = [n for n in self.pushed_normalized if n in kept_norm]

        # hotness_history: 每个 URL 保留最近 N 个快照
        for url in list(self.hotness_history.keys()):
            self.hotness_history[url] = self.hotness_history[url][-HOTNESS_KEEP_SNAPSHOTS:]
            if not self.hotness_history[url]:
                del self.hotness_history[url]

        # daily_*: 保留最近 N 天
        cutoff = (now - timedelta(days=DAILY_KEEP_DAYS)).strftime("%Y-%m-%d")
        for d in (self.daily_pushed, self.daily_instant, self.daily_llm_calls):
            for k in list(d.keys()):
                if k < cutoff:
                    del d[k]


def save_and_commit(state: State, path: str = DEFAULT_PATH) -> None:
    """写文件 + git add + commit + push。失败时 log 不抛异常。"""
    state.save(path)

    # 仅当 git 仓库存在时尝试 commit
    if not os.path.isdir(".git"):
        return

    cmds = [
        ["git", "config", "user.name", "github-actions[bot]"],
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        ["git", "add", path],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=False, capture_output=True, timeout=15)
        except Exception as e:
            print(f"[state] git cmd {cmd[1]} 异常: {e}")
            return

    # 检查是否有变更
    diff = subprocess.run(
        ["git", "diff", "--staged", "--quiet"],
        capture_output=True,
        timeout=15,
    )
    if diff.returncode == 0:
        # 无变更
        return

    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        subprocess.run(
            ["git", "commit", "-m", f"state: {ts}"],
            check=False, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "push"],
            check=False, capture_output=True, timeout=60,
        )
    except Exception as e:
        print(f"[state] git push 失败(state 未上云,下次重试): {e}")
