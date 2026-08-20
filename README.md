# 体育大新闻自动推送系统

定时抓取 **虎扑** 与 **直播吧** 的体育新闻,识别大新闻(绝对热度高 / 蹿升速度快 / 命中事件关键词),经 LLM 智能总结后通过 **Server酱** 推送到微信,无需打开 App 即可第一时间收到通知。

## 工作流

```
GitHub Actions (每 10 分钟)
    ↓
抓取 虎扑热帖 + 直播吧列表  →  规则过滤(去重/阈值/蹿升速度/即时关键词)
    ↓
GLM-4-Flash 判断重要性 + 生成标题摘要  →  Server酱推送到微信
    ↓
state.json git commit 持久化(去重 / 速度计算 / 冷启动保护)
```

## 部署步骤

### 1. Fork / 克隆本仓库到 GitHub

公开仓库 GitHub Actions 无分钟限制。

### 2. 配置 GitHub Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret

| Secret 名 | 用途 | 获取方式 |
|----------|------|---------|
| `SERVERCHAN_KEY` | Server酱 sendkey | https://sct.ftqq.com/ 微信扫码注册,在"发送消息"页获取 `SCT` 开头的 sendkey |
| `LLM_API_KEY` | 智谱 GLM-4-Flash API Key | https://open.bigmodel.cn/ 注册 + 实名,在 API Keys 页创建 |

> 两个 Secret 都必须配置,否则推送/LLM 环节会自动降级,但功能可用。

### 3. 启用 workflows

仓库 → Actions 页 → 分别启用 `Sport News Monitor` 和 `Daily Heartbeat` 两个 workflow。

首次会立即按 cron 自动运行,也可点 `Run workflow` 手动触发。

### 4. 验证

- 仓库 Actions → `Sport News Monitor` → 最新一次运行 → 查看日志,应有 `抓取到 N 条` `过滤后候选 N 条` `推送成功: ...`
- 微信应收到 Server酱 推送
- 仓库 `data/state.json` 应被 commit(状态持久化生效)

## 本地测试

```bash
# 安装依赖
pip install -r requirements.txt

# 仅打印候选(不推送,验证抓取+过滤链路)
python src/main.py --dry-run

# 发送测试推送(验证 Server酱 配置)
# PowerShell:
$env:SERVERCHAN_KEY="你的sendkey"
python src/main.py --test-push

# CMD:
set SERVERCHAN_KEY=你的sendkey
python src/main.py --test-push

# 心跳作业(检查系统健康)
python src/main.py --heartbeat
```

## 配置调优

所有阈值在 `config.yaml` 中:

| 字段 | 含义 | 调优建议 |
|------|------|---------|
| `sources.hupu.sections` | 虎扑订阅板块 | 添加 `all-vote`(湿乎乎话题)等 |
| `sources.zhibo8.sections` | 直播吧板块 | 直播吧仅支持 nba/zuqiu/dianjing |
| `rules.min_replies_hupu` | 虎扑回复数下限 | 推送过多 → 调高到 500/1000 |
| `rules.min_likes_hupu` | 虎扑亮数下限 | 同上 |
| `rules.velocity_replies_per_min` | 蹿升回复速度阈值 | 想抓更早期热点 → 调低 |
| `rules.max_push_per_run` | 单次运行推送上限 | Server酱 Turbo 升级后可调高 |
| `rules.daily_push_limit` | 每日推送上限 | 免费版 5,Turbo 可设 5000 |
| `rules.cold_start_multiplier` | 冷启动倍率 | 首日阈值 ×2,防撞日限 |
| `rules.cross_source_dedup_threshold` | 跨源去重 Jaccard 阈值 | 0.6 较保守,0.4 更激进 |
| `instant_keywords` | 即时通道关键词 | 命中即推,不走热度阈值 |
| `keyword_boost` | 加权关键词(阈值 ×0.5) | 调节哪些记者/事件优先 |
| `keyword_block` | 黑名单 | 标题命中即跳过 |

## 关键设计决策(5 轮校验产出)

1. **直播吧无热度指标**:列表页只有标题,详情页评论数是 JS 异步加载抓不到。改为关键词即时通道 + 跨源去重。
2. **跨源同事件去重**:虎扑+直播吧常发同事件。用标题 2-gram Jaccard ≥ 0.6 视为同事件,避免重复推送。
3. **冷启动保护**:首日 `state.pushed` 为空时所有阈值 ×2,且 `max_push_per_run` 实际仅放行热度 ×2 的硬阈值,防止首日撞 5 条 Server酱 日限。
4. **状态区分 pushed/attempted**:推送超时/4xx 不重试,标 `attempted`(不重复推,但也不占日限配额);5xx 才重试一次;`code != 0`(配额耗尽)立即停止本次所有推送。
5. **真实时间戳算速度**:GitHub Actions cron 不准点(可能延迟 5-15 分钟),所有蹿升速度用 `state.hotness_history` 中真实时间戳差值计算。
6. **concurrency 串行化**:防上一次未完成时下一次并发引发 state.json git 冲突 + 重复推送。
7. **心跳作业**:北京时间 09:00 单独 cron,检查 `last_success` 与 `consecutive_scrape_failures`,异常或抓取连续失败 ≥ 3 次则推送告警。
8. **LLM 软限流**:每日 ≤ 50 次调用,撞限则降级为纯规则推送(原 title 作为 headline)。
9. **LLM 失败降级**:API key 未配 / HTTP 非 200 / JSON 解析失败 → 全部降级为 `important=True` 直推,保证链路可用。

## 局限与后续优化项

- **直播吧评论数**:需逆向 AJAX 接口(如 `cache.zhibo8.com`),实现后可恢复评论数过滤
- **虎扑详情页 posted_at**:当前不抓取,无法按"发布 < 30 分钟"过滤
- **HTML 结构变更**:抓取后做 sanity check(< 5 条警告),连续 3 次失败触发心跳告警,但仍需手动修 CSS 选择器
- **更多数据源**:`scrapers.py` 按每源一函数设计,扩展懂球帝/腾讯体育等需新增 fetcher

## 费用

- GitHub Actions:免费(公开仓库)
- Server酱 免费版:5 条/天,200 条/月
- 智谱 GLM-4-Flash:免费额度(需实名)

月成本 ¥0。若 5 条/日不够:升级 Server酱 Turbo(¥10/月,5000 条/日),并将 `daily_push_limit` 调至 5000。

## 目录结构

```
.
├── .github/workflows/
│   ├── monitor.yml          # 每 10 分钟抓取+推送
│   └── heartbeat.yml        # 每日 09:00 健康检查
├── src/
│   ├── scrapers.py          # 虎扑 + 直播吧 抓取
│   ├── filter.py            # 规则过滤 + 跨源去重
│   ├── llm.py               # GLM-4-Flash 调用 + 降级
│   ├── push.py              # Server酱 推送
│   ├── state.py             # 状态持久化
│   └── main.py              # 主入口 + CLI 模式
├── data/state.json          # 运行时状态(自动 commit)
├── config.yaml              # 阈值/板块/关键词配置
└── requirements.txt
```
