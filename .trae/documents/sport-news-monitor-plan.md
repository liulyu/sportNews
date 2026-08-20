# 体育大新闻自动推送系统 - 实施计划(v2,经 5 轮校验修订)

## 一、需求摘要

构建一个自动监控系统,定时抓取 **虎扑**(hupu.com)和 **直播吧**(zhibo8.com)的体育新闻,识别"热度最高"和"蹿升速度最快"的大新闻,经 LLM 智能总结后,通过 **Server酱** 推送到微信,让用户无需打开 app 即可第一时间收到大新闻通知。

**用户决策(已确认):**
- 推送渠道:Server酱(微信)
- 运行环境:GitHub Actions(定时调度)
- AI 角色:规则过滤 + LLM 总结(GLM-4-Flash 免费模型)

---

## 二、五轮校验发现的问题与修正(新增章节)

### 第 1 轮:数据源真实性校验

| # | 原计划假设 | 实际验证 | 修正 |
|---|----------|---------|------|
| 1.1 | 直播吧详情页能抓评论数 | 详情页 HTML 写"评论载入中""已有0条评论",评论数通过 JS 异步加载,静态抓取拿不到 | 直播吧放弃评论数过滤,改为关键词驱动 + 跨源去重 |
| 1.2 | 直播吧列表页有热度指标 | 列表页只有标题+链接+日期,无任何热度指标 | 同上,改用关键词与位置(置顶/最新)作为弱信号 |
| 1.3 | 虎扑列表页有 posted_at | 列表页只有 `X亮Y回复`,无发布时间 | 列表页不取 posted_at;若需时间需抓详情页(成本高,默认不抓) |
| 1.4 | 虎扑详情页可补充 views + posted_at | 已验证详情页有 `1622回复/ 46亮1054096 浏览` + `2026-08-20 07:58:17发布` | 列表页抓取足够过滤;详情页可选,默认不抓 |
| 1.5 | `[流言板]` 是要去掉的前缀 | 实际是虎扑官方资讯帖的强信号(高质量源) | 保留作为 boost 信号,标题中保留展示 |

### 第 2 轮:调度与可靠性校验

| # | 问题 | 修正 |
|---|------|------|
| 2.1 | GitHub Actions cron 不准点,可能延迟 5-15 分钟甚至被跳过(高峰时段尤甚) | 流程对运行间隔不做"假设 10 分钟"硬编码,所有时间戳用 `state.last_run` 真实值计算 |
| 2.2 | 上一次运行未完成时,下一次可能并发执行,导致同时写 state.json 引发 git 冲突 + 重复推送 | workflow 加 `concurrency: { group: monitor, cancel-in-progress: false }` 串行化 |
| 2.3 | Server酱 推送因网络超时重试,可能 API 实际已成功 → 重复推送 | 区分 `pushed`(已确认成功)与 `attempted`(已尝试,状态未知);重试前先查 Server酱 当日已发送记录(若可);保守做法:重试只对明确 5xx 错误,4xx/超时不重试并标 attempted |
| 2.4 | Server酱免费版日限按北京时间计算,代码若按 UTC 计数会错 | `daily_pushed` 字典 key 用北京时间日期 `YYYY-MM-DD(+08:00)`,所有时间戳统一 ISO8601 带时区 |
| 2.5 | `git push` 用 `GITHUB_TOKEN` 不会触发下游 workflow(防环),对项目无害但需知 | 文档注明:state 更新不会自动触发新一轮监控 |

### 第 3 轮:成本与冷启动校验

| # | 问题 | 修正 |
|---|------|------|
| 3.1 | GLM-4-Flash "免费"额度需用户在智谱平台实名验证后才能用,且可能有 QPS 限制 | README 明确告知用户:需注册智谱 + 实名 + 申请 API Key;LLM 失败时降级到纯规则模式 |
| 3.2 | 多候选 × 144 运行/天 可能撞 LLM 配额 | (a) 规则过滤后只把 Top-2 候选送 LLM;(b) 加 LLM 调用计数到 state,每日 ≤ 50 次软上限 |
| 3.3 | 首日 state.json 不存在 → 所有热帖全过"绝对热度"阈值 → 首次运行可能瞬间撞 5 条 Server酱 日限 | 冷启动保护:首日(检测 `state.pushed` 为空)将所有阈值 ×2,且 max_push_per_run 降为 1 |
| 3.4 | workflow 静默失败数日,用户无感知 | 加每日心跳:北京时间 09:00 单独 cron 作业,若过去 24h 无成功运行,推送告警 |

### 第 4 轮:过滤逻辑漏洞校验

| # | 问题 | 修正 |
|---|------|------|
| 4.1 | 跨源同事件去重缺失(虎扑+直播吧同时报沃特森交易 → 用户收 2 次) | 新增 `normalize_title()`:去 `[流言板]`/`官方`/`Shams`/`Haynes` 等记者名前缀、去标点;对 normalized title 做相似度比对(简单:Jaccard 分词重叠 ≥ 0.6 视为同事件) |
| 4.2 | 蹿升速度假设 cron 间隔稳定 → 算错 | 速度计算公式改为 `velocity = (cur - prev) / (cur_ts - prev_ts).total_minutes()`,用真实时间戳 |
| 4.3 | "首次见 URL 跳过蹿升路径" → 错过刚发布的大新闻 | 改为:首次见到时,若绝对热度 ≥ 阈值 ×2 → 直接推送;否则记录快照等下次再判速度 |
| 4.4 | 关键词加权阈值 ×0.5 仍可能错过刚发布"官宣"类(1 分钟内回复还少) | 新增关键词白名单即时通道:标题命中 `官宣/签约/交易/退役/重伤/夺冠/选秀` 等关键词且未在 `pushed` 中 → 立即推送,不走热度阈值(每日上限 2 条,防止刷屏) |
| 4.5 | max_push_per_run=2 与 daily 5 矛盾,突发热点易撞限 | 改为:max_push_per_run=2,但若当日已推 4 条,本次只允许 1 条且必须是关键词即时通道或热度 ×3 的硬阈值 |
| 4.6 | 标题相似度分词需依赖中文分词库(jieba),增加依赖 | 用极简方案:字符串去标点 + 按 2-gram 集合 + Jaccard。零额外依赖 |

### 第 5 轮:运维/UX 校验

| # | 问题 | 修正 |
|---|------|------|
| 5.1 | 无测试推送命令,用户无法验证 Server酱 配置 | 新增 `python src/main.py --test-push` 发送固定测试消息 |
| 5.2 | 无 dry-run 调阈值工具 | 新增 `python src/main.py --dry-run` 打印候选+过滤原因,不推送 |
| 5.3 | 站点 HTML 结构变更静默失败 | 抓取后做 sanity check:若虎扑单板块返回 < 5 条 或 直播吧 < 3 条,记 warning 并写入 state.health;连续 3 次失败触发心跳告警 |
| 5.4 | `.gitignore` 未列具体条目 | 显式忽略:`.env`、`*.local`、`__pycache__/`、`data/state.local.json`(本地测试用) |
| 5.5 | workflow 失败默认邮件用户可能不开 | 不解决(用户开 GitHub 邮件通知即可),但心跳作业提供兜底 |
| 5.6 | 4320 commits/月,git 历史膨胀 | 接受(个人项目可接受);若用户在意可后续用 `data/` 单独分支或 git-restore |
| 5.7 | 虎扑/直播吧 HTML 实际请求需带 `Accept-Encoding: gzip` 才正常返回 | scrapers 中显式设置 headers 含 `Accept-Encoding: gzip, deflate` |
| 5.8 | 虎扑详情页 post_at 仅作为可选项 | 不抓详情页;若用户后续想要"发布时间 < 30 分钟"过滤,再启用详情页抓取 |

---

## 三、现状分析

### 3.1 工作区状态
- 路径 `d:\lb\sportNews` 当前为空目录(全新项目)。
- 无现有代码、配置、依赖,需从零搭建。

### 3.2 数据源可抓取性(经实际验证修正)

**虎扑(验证过):**
- 列表页 `https://bbs.hupu.com/all-nba` 返回静态 HTML,每条热帖含:`[流言板]标题`、链接、`X亮Y回复` 指标。
- `[流言板]` 是虎扑官方资讯帖的强信号,保留作为 boost 标识,展示时可去前缀。
- 详情页可获取 `posted_at` + `views` 但成本高,默认不抓。
- 用 `requests + BeautifulSoup` 即可,无需 JS 渲染。

**直播吧(验证过,与原计划不同):**
- 列表页 `https://news.zhibo8.com/{section}/` 仅返回标题 + 文章 URL,无任何热度指标。
- 详情页 HTML 含 `评论载入中` `已有0条评论` 占位,实际评论数通过 JS 异步加载,静态抓取拿不到。
- **修正策略**:直播吧放弃"评论数过滤",改用:(a) 标题命中事件关键词(`官宣/签约/交易/退役/重伤/夺冠`等)→ 进候选;(b) 与虎扑已推送内容做标题相似度去重(避免同事件二次推送)。

### 3.3 关键约束(更新)
- **Server酱免费版**:5 条/天,200 条/月。日限按北京时间计。
- **GitHub Actions**:公开仓库无分钟限制;cron 不保证准点(延迟 5-15 分钟甚至偶尔跳过)。
- **状态持久化**:Actions 跨运行无状态;用 git commit 写回 `data/state.json`。
- **并发风险**:cron 可能并发 → workflow 必须串行化。
- **GLM-4-Flash**:需用户在智谱平台实名注册获取 API Key;有 QPS 与日额度限制,需软限流。
- **反爬**:每 10 分钟一次低频抓取,两站点通常无强对抗;设真实 UA + `Accept-Encoding: gzip`,超时 10s,失败重试 1 次。

---

## 四、系统架构

```
┌─────────────┐   ┌──────────────┐   ┌────────────┐   ┌──────────┐   ┌────────┐
│ GitHub      │→  │ 抓取层       │→  │ 规则过滤   │→  │ LLM 总结 │→  │ 推送   │
│ Actions cron│   │ scrapers.py  │   │ filter.py  │   │ llm.py   │   │ push.py│
│ (10分钟,串行)│   │ 虎扑+直播吧  │   │ 去重+阈值  │   │ GLM-4-Flash│  │ Server酱│
│ + 心跳作业  │   │ gzip+UA     │   │ +跨源去重  │   │ 失败降级 │   │ 区分attempted│
└─────────────┘   └──────────────┘   └────────────┘   └──────────┘   └────────┘
                         │                                   ↑          ↓
                         ↓                                   │          ↓
                  ┌──────────────┐                          │    ┌────────┐
                  │ state.py     │←─────────────────────────┘    │ 微信   │
                  │ data/state.json (git commit 持久化)         │ 用户   │
                  │ +冷启动保护 +心跳记录                       │        │
                  └──────────────┘                              └────────┘
```

---

## 五、目录结构

```
d:\lb\sportNews\
├── .github/
│   └── workflows/
│       ├── monitor.yml          # 主监控作业(每 10 分钟)
│       └── heartbeat.yml       # 心跳作业(每日 09:00 北京时间)
├── src/
│   ├── __init__.py
│   ├── scrapers.py             # 虎扑 + 直播吧 抓取(含 sanity check)
│   ├── filter.py                # 规则过滤 + 去重 + 蹿升速度 + 跨源相似度
│   ├── llm.py                  # GLM API 调用 + 降级
│   ├── push.py                 # Server酱 推送(区分 attempted/pushed)
│   ├── state.py                # 状态读写 + git commit + 冷启动保护
│   └── main.py                 # 主流程 + --test-push / --dry-run
├── data/
│   └── state.json              # 运行时状态(运行时生成)
├── config.yaml                 # 阈值/板块/关键词
├── requirements.txt
├── .gitignore
└── README.md
```

---

## 六、模块详细设计

### 6.1 `config.yaml`(修订版)

```yaml
# 订阅板块
sources:
  hupu:
    enabled: true
    sections:
      - code: all-nba
        name: NBA
      - code: all-gambia    # 步行街热帖(综合大新闻)
        name: 步行街
      - code: all-cba
        name: CBA
      - code: all-football
        name: 中国足球
  zhibo8:
    enabled: true
    sections:
      - code: nba
        name: NBA
      - code: zhongchao
        name: 中超
      - code: zuqiu
        name: 足球

# 规则阈值(经校验修订)
rules:
  # 虎扑热度阈值(绝对路径,基于列表页 X亮Y回复)
  min_replies_hupu: 200          # 提高门槛,避免推送过多(实际热帖通常 >500 回复)
  min_likes_hupu: 40
  # 蹿升速度(用真实时间戳计算,容忍 cron 不准点)
  velocity_replies_per_min: 8
  velocity_likes_per_min: 2
  # 直播吧无热度指标,只用关键词通道
  # 推送节流
  max_push_per_run: 2
  daily_push_limit: 5             # Server酱 免费版硬上限
  dedup_window_hours: 6
  # 冷启动保护:首日(无 state.pushed)所有阈值 ×2
  cold_start_multiplier: 2.0
  # 跨源标题相似度去重
  cross_source_dedup_threshold: 0.6  # 2-gram Jaccard ≥ 0.6 视为同事件

# 即时通道:命中以下关键词且未推送过 → 立即候选(不走热度阈值)
# 每日上限 2 条,防止刷屏
instant_keywords:
  - 官宣
  - 签约
  - 交易达成
  - 退役
  - 重伤
  - 赛季报销
  - 夺冠
  - 选秀
  - 解雇
  - 下课
  - 世界纪录

# 加权关键词(降低热度阈值至 50%)
keyword_boost:
  - 重磅
  - Shams
  - Haynes
  - Stein
  - 总冠军
  - 世界杯
  - 伤病
  - 复出

# 黑名单(直接跳过)
keyword_block:
  - 招聘
  - 福利
  - 中奖
  - 抽奖

# 软限流
llm_daily_limit: 50              # LLM 每日最多调用次数(防撞智谱配额)
```

### 6.2 `src/scrapers.py`(修订版)

**NewsItem 数据结构(用 dataclass):**
```python
@dataclass
class NewsItem:
    source: str          # "hupu" | "zhibo8"
    section: str         # 板块名
    title: str           # 原始标题(含 [流言板] 等前缀,保留)
    url: str
    replies: int = 0     # 虎扑有;直播吧 0
    likes: int = 0       # 虎扑有;直播吧 0
    fetched_at: str = "" # ISO8601 带时区,抓取时刻
```

**`fetch_hupu(section_code: str) -> List[NewsItem]`:**
- 请求 `https://bbs.hupu.com/{section_code}`
- headers:`User-Agent: Mozilla/5.0 ... Chrome/...`、`Accept-Encoding: gzip, deflate`、`Accept-Language: zh-CN,zh`
- 解析:每个热帖项提取标题、链接、亮数、回复数(从 `X亮Y回复` 文本正则提取)
- **Sanity check**:解析结果 < 5 条 → 记 warning,返回空列表(避免下游因空数据异常)
- 超时 10s,失败重试 1 次,仍失败返回 `[]` 并 log

**`fetch_zhibo8(section_code: str) -> List[NewsItem]`:**
- 请求 `https://news.zhibo8.com/{section_code}/`
- headers:同上
- 解析:列表页是标题 + URL 列表,提取每条 title + url,replies/likes 留空(0)
- **Sanity check**:解析 < 3 条 → warning,返回 `[]`
- 不抓详情页(评论数是 JS 加载,抓不到且成本高)

**关键决策:**
- 直播吧放弃热度指标过滤,改为依赖下游 `filter.py` 的关键词即时通道 + 跨源去重。
- 若用户后续想获取直播吧评论数,需逆向找 AJAX 接口(`https://cache.zhibo8.com/...` 之类),作为后续优化项不在本期实现。

### 6.3 `src/filter.py`(修订版)

**入口:** `filter_and_rank(items: List[NewsItem], state: State, rules: dict) -> List[NewsItem]`

**处理流程:**

1. **跨源去重(新增,针对同事件):**
   - 对每个候选 item 计算 `normalized = normalize_title(item.title)`(去 `[流言板]/官方/Shams/Haynes` 等前缀、去标点空格)
   - 对 normalized 生成 2-gram 集合
   - 与 `state.pushed_normalized` 中已推项逐一比对,Jaccard ≥ `cross_source_dedup_threshold` → 视为同事件,跳过
   - 同时与本次运行内已选候选比对,防止同次运行内重复

2. **URL 去重:** `state.pushed[url]` 在 `dedup_window_hours` 内 → 跳过

3. **即时通道(新增):**
   - 标题命中 `instant_keywords` 任一 且当日 instant 通道已推 < 2 → 直接进候选,跳过热度判断
   - 否则进入热度判断

4. **绝对热度判断:**
   - 虎扑:`replies >= min_replies_hupu OR likes >= min_likes_hupu`
   - 直播吧:由于无热度数据,跳过绝对热度路径(只能走即时通道)
   - 命中 `keyword_boost`:阈值 ×0.5
   - 冷启动:`state.pushed` 为空 → 阈值 ×`cold_start_multiplier`

5. **蹿升速度判断(只对虎扑,且需历史快照):**
   - 取 `state.hotness_history[url]` 最近 2 个快照
   - `dt_min = (cur.fetched_at - prev.fetched_at).total_seconds() / 60`
   - `v_replies = (cur.replies - prev.replies) / dt_min`
   - `v_likes = (cur.likes - prev.likes) / dt_min`
   - `v_replies >= velocity_replies_per_min OR v_likes >= velocity_likes_per_min` → 视为蹿升,进候选
   - **修正首次见到逻辑:** 若无历史快照,但绝对热度 ≥ 阈值 ×2 → 直接通过(覆盖原"首次见跳过")

6. **黑名单过滤:** 命中 `keyword_block` 直接跳过

7. **节流截断:**
   - 当日已推 ≥ `daily_push_limit - 1`(即已推 4 条)→ 本次只允许 1 条且必须 `(instant 通道 OR 热度 ≥ 阈值 ×3)`
   - 按优先级排序:即时通道 > 绝对热度 ×2 > 蹿升速度 > 普通绝对热度
   - 截取前 `max_push_per_run` 条

8. **更新快照:** 对所有抓取到的 item(无论是否通过)更新 `state.hotness_history[url]` → 这是下次速度计算的基础。

### 6.4 `src/llm.py`(修订版)

**调用智谱 GLM-4-Flash(OpenAI 兼容接口):**
- 基址:`https://open.bigmodel.cn/api/paas/v4/chat/completions`
- model: `glm-4-flash`
- 用 `requests` 直接调(避免 `zhipuai` SDK 版本依赖)
- `response_format={"type": "json_object"}` 强制 JSON

**Prompt:**
```
你是体育新闻编辑。判断下列新闻是否值得作为"大新闻"推送给体育爱好者。

标题:{title}
来源:{source} - {section}
热度指标:回复 {replies} / 亮 {likes}
增速:回复 {velocity}/分钟

返回 JSON(严格):
{
  "important": true/false,
  "score": 1-10,
  "headline": "20字内精炼标题",
  "summary": "30字内一句话摘要"
}

判断标准:球员交易/签约/伤病/退役、教练变动、重大比赛结果、纪录达成、争议事件 算大新闻;
普通花絮、训练照、情怀回顾、球迷投票 不算。
```

**容错与降级:**
- 超时 15s,失败 1 次不重试(避免拖长 workflow)
- 返回 JSON 解析失败 → 用原 title 作为 headline,important=true,score=5
- API key 未配置 → 跳过 LLM,候选 item 全部 `important=true` 直推
- 软限流:当日 LLM 调用次数 ≥ `llm_daily_limit` → 跳过 LLM 环节

### 6.5 `src/push.py`(修订版)

**接口:** `POST https://sctapi.ftqq.com/{sendkey}.send`
- 表单字段:`title`(≤ 32)、`desp`(Markdown,≤ 32KB)

**消息格式:**
```markdown
## {headline}

{summary}

📊 热度:虎扑 {replies}回复 {likes}亮 / 直播吧 关键词通道
🔗 [查看原文]({url})

来源:{source} · {section} · {fetched_at}
```

**调用与状态管理(关键修订):**
- 调用前:`daily_pushed_count < daily_push_limit` 才调用
- 调用后:
  - HTTP 200 且返回 JSON `code=0` → 标记 `state.mark_pushed(url, status="pushed")`
  - HTTP 5xx → 重试 1 次,仍失败标 `state.mark_pushed(url, status="attempted")`,下次运行不再推(dedup 仍生效)
  - HTTP 4xx / 超时 → 不重试,标 `attempted`(避免对端实际成功但本地超时导致重复)
  - HTTP 200 但 `code != 0`(配额耗尽等)→ 标 `attempted`,log 错误,停止本次运行所有推送
- `state.attempted` 中的 URL 在 24h 后可重试一次(防止永久错过)

### 6.6 `src/state.py`(修订版)

**`data/state.json` 结构(扩展):**
```json
{
  "last_run": "2026-08-20T14:30:00+08:00",
  "health": {
    "last_success": "2026-08-20T14:30:00+08:00",
    "consecutive_scrape_failures": 0,
    "scrape_warnings": []
  },
  "daily_pushed": {
    "2026-08-20": 3
  },
  "daily_instant": {
    "2026-08-20": 1
  },
  "daily_llm_calls": {
    "2026-08-20": 12
  },
  "pushed": {
    "https://bbs.hupu.com/641958264.html": {
      "ts": "2026-08-20T14:30:00+08:00",
      "status": "pushed"
    }
  },
  "pushed_normalized": [
    "shams-沃特森赴骑士斯特鲁斯加盟快船掘金得到选秀权"
  ],
  "hotness_history": {
    "https://bbs.hupu.com/641958264.html": [
      {"t": "2026-08-20T14:20:00+08:00", "replies": 1500, "likes": 46}
    ]
  }
}
```

**关键方法:**
- `load_state(path) -> State`:文件不存在 → 返回空 State(冷启动)
- `mark_pushed(url, normalized, status)`:同时更新 `pushed`、`pushed_normalized`、`daily_pushed`
- `daily_pushed_count() -> int`:按北京时间取当日计数
- `daily_instant_count() -> int`:同上
- `is_cold_start() -> bool`:`pushed` 为空
- `save_and_commit(path)`:写文件 + git add + commit + push;commit 命令含 `--no-verify` 避免钩子;失败时 log 不抛异常(state 没存下次重试,代价是可能重复推一次)

**清理策略:**
- `pushed`/`pushed_normalized` 中超过 24h 的项删除
- `hotness_history[url]` 保留最近 12 个快照
- `daily_*` 字典保留最近 7 天

### 6.7 `src/main.py`(修订版)

```python
import argparse, sys

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-push", action="store_true", help="发送测试推送")
    parser.add_argument("--dry-run", action="store_true", help="只打印候选,不推送")
    args = parser.parse_args()

    config = load_config("config.yaml")
    state = load_state("data/state.json")

    if args.test_push:
        push_to_serverchan({
            "headline": "测试推送",
            "summary": "如果你看到此消息,Server酱 配置成功。",
            "url": "https://github.com/"
        }, sendkey=os.getenv("SERVERCHAN_KEY"))
        return

    # 1. 抓取
    items = []
    if config["sources"]["hupu"]["enabled"]:
        for s in config["sources"]["hupu"]["sections"]:
            items.extend(fetch_hupu(s["code"]))
    if config["sources"]["zhibo8"]["enabled"]:
        for s in config["sources"]["zhibo8"]["sections"]:
            items.extend(fetch_zhibo8(s["code"]))

    # 2. Sanity check
    if len(items) < 5:
        state.health.consecutive_scrape_failures += 1
        log(f"WARN: 仅抓到 {len(items)} 条,可能解析失效")
    else:
        state.health.consecutive_scrape_failures = 0
    state.health.last_success = now()

    # 3. 过滤
    candidates = filter_and_rank(items, state, config["rules"])

    if args.dry_run:
        for c in candidates:
            print(f"[候选] {c.source} | {c.title} | replies={c.replies}")
        return

    # 4. LLM 总结(只对 Top-2 调用,控制成本)
    top = candidates[:2]
    summaries = [llm_judge(c, os.getenv("LLM_API_KEY")) for c in top]
    # 剩余候选如果 important 不确定,直接以原 title 推送
    for c in candidates[2:]:
        summaries.append({"important": True, "headline": c.title[:32], "summary": "", "url": c.url, ...})

    # 5. 推送
    for s in summaries:
        if not s["important"]:
            continue
        if state.daily_pushed_count() >= config["rules"]["daily_push_limit"]:
            log("已达 Server酱 日限,停止")
            break
        push_to_serverchan(s, sendkey=os.getenv("SERVERCHAN_KEY"))
        # mark_pushed 由 push 内部调用(成功才标 pushed,失败标 attempted)

    # 6. 持久化(无论上游是否成功都尝试,确保 hotness_history 落盘)
    save_and_commit_state(state)

if __name__ == "__main__":
    main()
```

### 6.8 `.github/workflows/monitor.yml`(修订版)

```yaml
name: Sport News Monitor

on:
  schedule:
    - cron: '*/10 * * * *'   # 每 10 分钟(UTC,北京时间 -8)
  workflow_dispatch:

permissions:
  contents: write

# 串行化:防止上一次未完成时下一次并发导致 state 冲突
concurrency:
  group: monitor
  cancel-in-progress: false

jobs:
  monitor:
    runs-on: ubuntu-latest
    timeout-minutes: 5     # 防止单次卡死占用额度
    steps:
      - uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install deps
        run: pip install -r requirements.txt

      - name: Run monitor
        env:
          SERVERCHAN_KEY: ${{ secrets.SERVERCHAN_KEY }}
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
          TZ: Asia/Shanghai
        run: python src/main.py

      - name: Commit state
        if: always()        # 即使 Run 失败也尝试保存 hotness 快照
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add data/state.json 2>/dev/null || exit 0
          git diff --staged --quiet || git commit -m "state: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
          git push
```

### 6.9 `.github/workflows/heartbeat.yml`(新增)

```yaml
name: Daily Heartbeat

on:
  schedule:
    # 每日北京时间 09:00 = UTC 01:00
    - cron: '0 1 * * *'
  workflow_dispatch:

permissions:
  contents: read

jobs:
  heartbeat:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Setup Python
        uses: actions/setup-python@v5
        with: { python-version: '3.11', cache: 'pip' }
      - name: Install deps
        run: pip install -r requirements.txt
      - name: Heartbeat
        env:
          SERVERCHAN_KEY: ${{ secrets.SERVERCHAN_KEY }}
          TZ: Asia/Shanghai
        run: python src/main.py --heartbeat
```

`--heartbeat` 模式:
- 读取 `data/state.json`
- 若 `health.last_success` 距当前 > 30 分钟 → 推送告警"监控异常,最近 30 分钟无成功运行"
- 若 `health.consecutive_scrape_failures >= 3` → 推送告警"抓取解析连续失败 N 次,可能 HTML 结构变更"
- 否则推每日报告"昨日推送 N 条,系统运行正常"(用 1 条 Server酱 配额)

### 6.10 `requirements.txt`

```
requests==2.32.3
beautifulsoup4==4.12.3
lxml==5.3.0
PyYAML==6.0.2
```

注:**不用 `zhipuai` SDK**,改用 `requests` 直调 OpenAI 兼容接口,减少依赖。

### 6.11 `.gitignore`

```
__pycache__/
*.pyc
.env
*.local
.venv/
data/state.local.json    # 本地测试副本
*.log
```

### 6.12 GitHub Secrets 需配置

- `SERVERCHAN_KEY`:Server酱 sendkey(https://sct.ftqq.com/ 微信扫码注册后获取)
- `LLM_API_KEY`:智谱 API Key(https://open.bigmodel.cn/ 注册 + 实名后创建)

---

## 七、实施步骤

| # | 任务 | 文件 |
|---|------|------|
| 1 | 初始化骨架:`.gitignore`、`requirements.txt`、`src/__init__.py` | 多个 |
| 2 | 实现 `scrapers.py`:虎扑(列表页+解析 X亮Y回复)+ 直播吧(列表页标题+URL),含 sanity check | `src/scrapers.py` |
| 3 | 实现 `state.py`:JSON 读写 + 冷启动判定 + git commit + 清理策略 | `src/state.py` |
| 4 | 实现 `filter.py`:URL 去重 + 跨源 2-gram 相似度去重 + 即时通道 + 绝对热度 + 蹿升速度 + 节流截断 | `src/filter.py` |
| 5 | 实现 `llm.py`:`requests` 调 GLM-4-Flash,JSON 解析 + 降级 + 软限流 | `src/llm.py` |
| 6 | 实现 `push.py`:Server酱 POST + 区分 pushed/attempted + 错误分类处理 | `src/push.py` |
| 7 | 实现 `main.py`:串联 1-6 + `--test-push` / `--dry-run` / `--heartbeat` | `src/main.py` |
| 8 | 编写 `config.yaml`:板块、阈值、关键词(按 6.1) | `config.yaml` |
| 9 | 编写 `monitor.yml` + `heartbeat.yml` | `.github/workflows/*` |
| 10 | 编写 `README.md`:本地测试、Secrets 配置、阈值调优、智谱注册步骤 | `README.md` |
| 11 | 本地 `python src/main.py --dry-run`(空 Secrets)验证抓取+过滤链路 | - |
| 12 | 本地 `python src/main.py --test-push`(配 Server酱 key)验证推送 | - |

---

## 八、验证方式

**本地验证(开发期):**
```bash
# 验证抓取+过滤(不推送)
python src/main.py --dry-run

# 验证 Server酱 推送(配置 SERVERCHAN_KEY 后)
$env:SERVERCHAN_KEY="你的sendkey"
python src/main.py --test-push
```

**端到端验证(部署后):**
1. GitHub 仓库手动触发 `workflow_dispatch` 跑 `monitor.yml`
2. 查 Actions 日志:抓取条数、过滤候选数、LLM 判断、Server酱 响应
3. 查微信是否收到推送
4. 查 `data/state.json` 是否被 commit 回仓库,内容是否含 `pushed`/`hotness_history`
5. 等 10 分钟看下一次自动运行是否无重复推送
6. 手动改坏 `scrapers.py` 的 CSS selector,触发连续失败,等 `heartbeat` 推送告警

**长期调优:**
- 5 条/日 Server酱 不够 → 调高 `min_replies_hupu` 至 500/1000;或升级 Turbo(¥10/月,5000 条)
- LLM 配额耗尽 → 调高 `min_replies_hupu` 减少候选 → 减少 LLM 调用
- 扩展板块:在 `config.yaml` 添加 `sources.hupu.sections` 条目即可

---

## 九、假设与决策

1. **假设**:虎扑和直播吧静态 HTML 抓取可持续。若站点升级 SPA 或加风控,需切到 playwright 或抓移动端 API,本期不预留此复杂度。
2. **决策**:LLM 用 GLM-4-Flash(智谱,免费 + 国内访问),用户需自注册。LLM 失败降级到纯规则,保证链路可用。
3. **决策**:状态持久化用 git commit 而非 Actions cache。原因:cache 有 7 天空闲过期风险 + 并发不保证;git commit 透明可审计且不会丢失。commit 噪音对个人项目可接受。
4. **决策**:cron 每 10 分钟。新闻延迟 10 分钟可接受;改 5 分钟也可但留意反爬。
5. **决策**:直播吧因列表页无热度指标,只用关键词即时通道 + 跨源去重;评论数抓取作为后续优化项(需逆向 AJAX 接口)。
6. **决策**:跨源去重用 2-gram Jaccard 而非 jieba,零额外依赖,精度够用。
7. **决策**:冷启动首日阈值 ×2 + max_push_per_run=1,防止首日撞 5 条限额。
8. **未决项**:用户后续可能想加更多源(懂球帝、腾讯体育),`scrapers.py` 按"每源一函数"设计,易扩展。

---

## 十、五轮校验总结

| 轮次 | 关注点 | 发现数 | 全部修正 |
|------|--------|--------|---------|
| 1 | 数据源真实性 | 5 | ✅ 修正直播吧策略,虎扑详情页可选 |
| 2 | 调度与可靠性 | 5 | ✅ 串行化、时区、attempted/pushed 区分 |
| 3 | 成本与冷启动 | 4 | ✅ LLM 软限流、冷启动 ×2、心跳作业 |
| 4 | 过滤逻辑漏洞 | 6 | ✅ 跨源去重、真实时间戳、即时通道 |
| 5 | 运维/UX | 8 | ✅ 测试命令、sanity check、心跳告警 |

**合计修正 28 处**,核心修订:
- 直播吧从"评论数过滤"改为"关键词即时通道 + 跨源去重"
- 新增跨源 2-gram 相似度去重
- 新增串行化 concurrency 控制
- 新增冷启动保护
- 新增心跳作业 + sanity check
- 状态区分 pushed/attempted
- 新增 `--test-push` / `--dry-run` / `--heartbeat` 三个运维模式
- LLM 改用 requests 直调(去 zhipuai SDK 依赖)
