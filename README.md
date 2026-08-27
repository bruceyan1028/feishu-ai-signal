# 飞书 AI 情报（feishu-ai-signal）

端到端 AI 行业情报系统：从飞书多维表读源配置，采集网页 / RSS / 视频 / 社媒 / 播客，清洗去重后写入条目表，再用 LLM 生成每日简报与周报，发布到 GitHub Pages，并通过飞书卡片推送到群聊。

```
飞书一级参数表（源配置，运行时唯一真相）
        │
        ▼
RSS / Scrape / Media / Social / Podcast
        │
        ▼
清洗去重（时间窗、关键词、类型规则、白宫 AI 门槛）
        │
        ▼
飞书条目表 ──► LLM 分析 ──► 每日简报 / 周报 / 动向追踪
        │
        ▼
site/（GitHub Pages） + 飞书消息卡片
```

网页沿用 `index.html` 的视觉与交互。今日简报、信号列表、详情、周报、动向追踪是真实数据；评论/笔记、多模型辩论等仍是前端本地模拟。

本文给后续开发维护用：先讲数据与逻辑，再讲怎么跑。仓库公开，**不要把 `.env`、飞书 token、`open_id` 写进文档或提交**。

---

## 目录

1. [给后续维护者的硬约定](#给后续维护者的硬约定)
2. [仓库结构](#仓库结构)
3. [数据真相：飞书 vs 种子 vs 站点](#数据真相飞书-vs-种子-vs-站点)
4. [飞书表模型](#飞书表模型)
5. [源状态与验收](#源状态与验收)
6. [采集流水线](#采集流水线)
7. [清洗与过滤](#清洗与过滤)
8. [每日简报](#每日简报)
9. [周报与动向追踪](#周报与动向追踪)
10. [静态站与配置台](#静态站与配置台)
11. [飞书卡片](#飞书卡片)
12. [模块地图](#模块地图)
13. [extra_config 与专用解析器](#extra_config-与专用解析器)
14. [GitHub Actions 与 Runner](#github-actions-与-runner)
15. [环境变量](#环境变量)
16. [本地怎么跑](#本地怎么跑)
17. [诊断、测试与种子导出](#诊断测试与种子导出)
18. [常见改动清单](#常见改动清单)

---

## 给后续维护者的硬约定

1. **采集配置只认飞书一级参数表**，不认 `src/seed_default.json`。种子只是 bootstrap / 回滚快照。改源之前不要扫仓库 JSON 当现行配置。
2. **唯一维护的飞书 Base** 是当前 `.env` / GitHub Secrets 里的那套（wiki 与本地 Cursor 规则会写明 `app_token`）。历史 Base 已废弃，不要读、不要写、不要拿来对比。
3. `status`：`active` 正式流水线会跑；`experimental` 只给诊断；`paused` 不跑。改 endpoint / `fetch_method` / 抽取规则后先标 `experimental`，验收再改 `active`。参数表与信号源表状态必须对齐。
4. 每次采集结束必须调用 `feishu.sync_param_collect_stats(...)` 回写：最近采集时间、条目数、查重过滤、时间窗过滤。该函数在入库 > 0 时会勾「通过」。人工验收场景不要另写一套漏字段回写。
5. 改飞书源配置或二级参数后：`python -m tools.export_seed` 刷新 `src/seed_default.json`，再和代码一起提交。条目表、每日简报是产物，不导出。
6. **公开站不带筛选规则**。`keyword_regex`、`min_content_chars`、`dedup_key`、`extra_config` 不得出现在 `site/data/sources.json`。
7. 列表抽取是解析器问题，不要用 LLM 替代。模型只适合临界内容精筛（社媒已有；网页媒体可按源开关加）。
8. `open_id` / `chat_id` 由自建应用签发、跨应用不通用。换应用必须重取，并把机器人重新拉进目标群。
9. 本地 `127.0.0.1` 预览别人打不开。发给别人看的是 GitHub Pages 公网地址。

---

## 仓库结构

| 路径 | 作用 |
| --- | --- |
| `src/` | 全部业务逻辑。入口都是 `python -m src.<模块>` |
| `src/seed_default.json` | 信号源表 + 一级参数 + 五张二级参数的快照 |
| `src/bootstrap.py` | 在空 Base 里幂等建表、可选灌种子 |
| `tools/export_seed.py` | 从现行飞书表反写种子 |
| `index.html` | 前端单页；发布时复制到 `site/index.html` |
| `site/` | GitHub Pages 产物：`index.html` + `data/*.json` + `media/` |
| `tests/` | `unittest` |
| `.github/workflows/` | 日报、周报、社媒、播客、Pages 预览 |
| `output/` | 本地诊断/简报 JSON，已 gitignore |
| `.cursor/rules/` | 给 Cursor 的约定；部分含真实 ID，不入库 |

---

## 数据真相：飞书 vs 种子 vs 站点

| 对象 | 谁在用 | 能不能当现行配置 |
| --- | --- | --- |
| 飞书一级参数表 | `main` / `daily` / `diag_*` / `sources_api` | **是。采集与简报白名单都读它** |
| 飞书二级参数表 | `typed_config.load_typed_configs` | 是。论文/公众号/视频/社媒/GitHub 的类型规则 |
| 飞书信号源表 | 人工目录 + 配置台同步「自动化状态」 | 否。不能当 ingest 配置；名称常和参数表对不齐 |
| `src/seed_default.json` | `bootstrap`、无飞书时的预览 | 否。可能落后于飞书 |
| `site/data/brief-*.json` | 公开站今日简报 | 产物 |
| `site/data/sources.json` | 公开站信号源页（只读快照） | 展示用，规则字段已剥掉 |
| `site/data/weekly-*.json` | 周报页 | 产物 |
| `site/data/timeline-latest.json` | 动向追踪 | 产物 |

配置台（`python -m src.sources_api`）在本机把飞书参数表现成可写 UI；公开 Pages 永远只读快照。

---

## 飞书表模型

`python -m src.bootstrap` 幂等创建表和字段，不重建已有数据。表名必须与代码里的查找名一致。

### 配置表（要进种子）

| 表 | 环境变量 | 主键 | 用途 |
| --- | --- | --- | --- |
| 信号源表 | `FEISHU_SOURCE_TABLE_ID` | 名称 | 人工目录：分类、层级、主要看什么、自动化状态 |
| 一级参数 | `FEISHU_PARAM_TABLE_ID` | `source_id` | **ingest 配置 + 采集统计** |
| 二级参数-论文 | `FEISHU_PAPER_CONFIG_TABLE_ID` | `source_id` | 录用/热度/摘要门槛等 |
| 二级参数-公众号 | `FEISHU_WECHAT_CONFIG_TABLE_ID` | `source_id` | 账号白名单、软广、字数 |
| 二级参数-视频 | `FEISHU_VIDEO_CONFIG_TABLE_ID` | `source_id` | 频道与时长等 |
| 二级参数-社媒 | `FEISHU_SOCIAL_CONFIG_TABLE_ID` | `source_id` | X 白名单、评分阈值、LLM 精筛开关 |
| 二级参数-GitHub | `FEISHU_GITHUB_CONFIG_TABLE_ID` | `source_id` | 热榜选仓/打分 |

### 产物表（不进种子）

| 表 | 环境变量 | 用途 |
| --- | --- | --- |
| 条目表 | `FEISHU_ENTRY_TABLE_ID` | 每条信号的原文、译文、评分、媒体 JSON |
| 每日简报 | `FEISHU_BRIEF_TABLE_ID` | 按日期 upsert 一份简报 |
| 周报 / 周报待纳入 | `FEISHU_WEEKLY_TABLE_ID` / `FEISHU_WEEKLY_PENDING_TABLE_ID` | 可空，首次按表名创建 |
| 追踪对象 / 追踪事件 | `FEISHU_TRACKED_*` | 可空，`timeline` 按表名查找或创建 |

### 一级参数关键字段

| 字段 | 含义 |
| --- | --- |
| `source_id` | 稳定 ID，代码按它分支（如 `anthropic-news`、`whitehouse-tech-actions`） |
| `status` | `active` / `experimental` / `paused` |
| `fetch_method` | `RSS` / `Scrape` / `Media` / `Social` / `Podcast` / `Manual` / … |
| `endpoint` | 列表页、RSS 或频道入口 |
| `lookback_window` | 如 `24h`、`7d`、`30d`。未配则清洗默认 168h |
| `keyword_regex` | 主题门。标题命中即过，否则看正文命中次数 |
| `min_content_chars` | 正文过短丢弃 |
| `dedup_key` | 通常 `normalize(url)`；X 为 `x_post_id` |
| `extra_config` | JSON 字符串，专用解析器与排除规则 |
| `来源类型` | 载体：论文 / 纯网页 / 视频 / 社交媒体 / 公众号 / Github热榜 / 播客 / 其他 |
| `dimension` | 主题分类（前沿模型公司、政策监管地缘、中文媒体等），写入简报 `category` |
| `tier` / `priority` | L1–L4，P0–P2 |
| `通过` | 验收勾选；`sync_param_collect_stats` 在本轮入库 > 0 时也会勾 |
| `最近采集时间` / `条目数` / `查重过滤` / `时间窗过滤` | 每轮采集回写 |
| `采集游标` | Social 的 `since_id` 等水位 |

### 条目表关键字段

标题、链接、来源、`source_id`、发布时间、原文、中文正文、中文标题、中文摘要、AI深度解读、为何重要、主题、影响分 / 新颖度 / 可行动性 / 紧迫度、状态（待分析/已分析）、去重键、媒体资源（JSON）、质量分、论文/社媒/播客指标。

`daily` 分析后回写中文标题、摘要、解读和分数。简报 JSON 由这些字段组装，不再依赖分析时的飞书原文。

---

## 源状态与验收

| 参数表 `status` | 信号源表 | 正式流水线 |
| --- | --- | --- |
| `active` | 已接入 | 会跑 |
| `experimental` | 待测 | 默认不跑，诊断可跑 |
| `paused` | 已暂停 | 不跑 |

标 `active`：链路已验证，或已稳定入库，或核心 RSS（如 `arxiv-*`）本身可解析。

标 `experimental`：新源、诊断 `list_empty` / `no_links` / 正文全被滤掉、招聘/榜单/强反爬未验收、刚改抽取规则。

`daily.select_candidates` 的白名单只有 `active`。配置台关掉开关 = `paused`，旧条目也不会再进简报。

---

## 采集流水线

入口：`python -m src.main`（`src/main.py`）。

默认方法：`RSS` + `Scrape` + `Media`。`Social` 默认不自动跑（可用 `--method Social`）。`Podcast` 由独立工作流 `--method Podcast`。

```
读一级参数 + 二级参数
  → map_*_sources（只取 active，Scrape 再排除 B 类）
  → 各通道抓 RawItem（title/url/body/published_raw/feed/…）
  → process_and_clean
  → 跨轮去重（条目表已有去重键）
  → 论文 PDF / 政策 PDF / RSS 正文补全 / 播客转录摘要
  → sync_param_collect_stats（覆盖式快照回写参数表）
  → health.write_records（按轮留存分源漏斗，见「分源健康记录」）
  → format_for_feishu → 批量写入条目表
```

### 各通道

| 方法 | 映射 | 抓取模块 | 说明 |
| --- | --- | --- | --- |
| RSS | `sources.map_feed_sources` | `rss.fetch_feed_sources_with_stats` | 整份 feed；摘要型官方 RSS 会标 `needs_fulltext` 稍后回源 |
| Scrape | `sources.map_scrape_sources` | `scrape.fetch_scrape_sources_with_stats` | 列表抽链再抓正文；engine=`auto` 时 Jina 不可达会降级 direct |
| Media | `sources.map_media_sources` | `video.fetch_video_sources` | YouTube Data API；`extra_config.channel_id` |
| Social | `sources.map_social_sources` | `social.fetch_social_sources` | X API v2 + `since_id`；需 `X_BEARER_TOKEN` |
| Podcast | `sources.map_podcast_sources` | `podcast.fetch_podcast_sources` | 白名单节目 RSS；转录见下 |

单通道失败通常只打日志、不拖垮整轮（Scrape / Media / Social 有 try/except）。

RSS 与 Scrape 的 `*_with_stats` 变体额外返回分源抓取结果（`error` / `entries` / `links` / `article_ok` / `timing_ms`），进 `health` 记录。不带 stats 的旧函数保留，只是丢掉统计。

### 分源健康记录

`src/health.py`。参数表回写的四个字段是覆盖式快照，答不了「这个源连续几天零产出」和「哪天开始死的」；清洗漏斗原先也只按原因聚合、不按源归属，所以看得见「本轮 keyword_regex 淘汰 40 条」，看不见是哪个源被自己的正则卡死。

- `Funnel`：`process_and_clean` 的第 4 个可选出参。总计照旧打日志，不传它时行为与改造前一致。
- `build_records`：本轮每个尝试过的源出一行，含 `fetch`（链路结果）、`funnel`（分源漏斗）、`written`、`dedup_dropped`、`blocked_at`（抓到却一条不留时淘汰最多的那一步）。**抓取为 0 的源也出行**，否则彻底静默的源在数据里根本不存在。
- `write_records`：落 `output/health/dt=YYYY-MM-DD.jsonl`（`output/` 已 gitignore）。这是唯一落盘点，要换对象存储或数据库只改这一个函数。
- `FUNNEL_STAGES` 必须与 `process.py` 的淘汰原因名对齐，`tests/test_health.py` 会校验。

```bash
python -m src.health              # 体检报告：按断流天数与卡点排序
python -m src.health --days 30
```

报告把四类问题分开：抓取为 0 是链路问题；抓到很多却 `blocked_at=keyword_regex` 是规则问题；`dry_days` 有值是能定位到哪天断的；`dedup_dropped` 高是一直在重复抓老内容。

### B 类源（正式 Scrape 不跑）

`sources._B_SET`：`chatbot-arena`、`artificial-analysis`、`papers-with-code-sota`。另：`extra_config` 含 `snapshot_mode` / `diff_mode` 也视为 B 类。诊断 `diag_scrape` 可以包含它们。`papers-with-code-trending` 会跑，但链接常被改写到 Hugging Face Papers，站点上不一定出现 paperswithcode 域名。

### 正文补全与附件

- `rss.backfill_full_text`：RSS 摘要过短时回源；直连失败再试 Jina。
- `paper_fulltext.enrich_item`：仅对最终拟入库的论文下 PDF，抽章节与图表页。
- `policy_document.enrich_items`：白宫源在 `document_pdf_enrich` 打开时拉官方 PDF。
- 播客：`podcast:transcript` → 节目页/YouTube 字幕 → 托管 ASR（独立 `ASR_*`，不能假设文本 LLM 也能转写）。

---

## 清洗与过滤

`process.process_and_clean` 顺序（命中即丢）：

1. 每源条数上限 `MAX_ITEMS_PER_FEED`（默认 80）
2. 无标题或无链接
3. `title_exclude_regex`（来自 `extra_config`）
4. 无合法发布时间（不准用采集时间冒充新稿）
5. 超出 `lookback_window`（`heat_keep` 超高热度旧文可例外）
6. 正文过短（官方摘要 RSS 可先放行再补全）
7. `keyword_regex`：标题命中即过；否则正文命中次数 ≥ `keyword_min_hits`（默认 1）
8. **白宫源额外**：`whitehouse-tech-*` 必须标题或正文命中 AI/ML/模型词（`process.is_ai_policy_text`）。`science and technology`、`R&D` 不能单独过。简报候选也会再卡一次，避免已入库的航天/关税备忘录再进推送
9. 论文再走 `typed_config` 信号分、录用、社区热度

漏斗计数：`lookback`、`keyword_regex`、`not_ai_policy`、`min_content_chars`、`title_exclude_regex` 等。时间窗过滤条数回写参数表「时间窗过滤」。

### 论文质量

综合分约 `0.40*录用 + 0.25*社区 + 0.35*signal_score`。默认 `PAPER_QUALITY_MIN_SCORE=60`。arXiv 预印本默认要有 HF 社区热度；已录用会议/顶刊豁免。每轮入库和每份简报的 arXiv 合计不超过 `MAX_ARXIV_ITEMS`（默认 10）。简报排序里 arXiv 质量分再乘 `ARXIV_QUALITY_WEIGHT`（默认 0.7）。海外 runner 上论文富集易超时，日报 CI 里 `PAPER_ENRICH_ENABLED=0`。

### 社媒精筛（已实现的「规则 + AI」）

硬过滤 → 0–100 可解释分 → 高分直接过 → 临界分才问 LLM → 低分丢。`enable_llm_filter` 在二级社媒表。LLM 失败按精确率优先丢弃。

---

## 每日简报

入口：`python -m src.daily --output output/daily-brief.json`。需要 `LLM_API_KEY`。

```
读 active 源 + 条目表
  → 仍在各源 lookback（且不超过 7 天）内的候选
  → 白宫非 AI 文再滤一遍
  → 按 P0 优先、质量分、时间排序；给非 P0 留 DAILY_MIN_NON_P0
  → 论文/视频/播客/GitHub/每源上限
  → cluster.collapse_for_brief 标题近似折叠
  → 无存量分析则 analyze_signal（中文标题/摘要/解读/打分）
  → 文章配图、论文图表
  → LLM 写 intro + bullets
  → upsert 每日简报表
```

规模默认：候选 30、输出信号 30、论文最多 4、视频 1–4、播客最多 2、GitHub 最多 5、每源最多 4。

分析字段：`title_cn`、`summary_cn`、`why`、`deep_analysis_cn`、`impact` / `novelty` / `actionability`（0–100）、`urgency`（高/中/低）、`topics`（从固定集合选 2–4 个）。端侧主题由规则强制补「端侧」；白宫政策强制补「监管」。

英文正文按优先级/影响分翻译，上限见 `BODY_TRANSLATE_*`。详情页展示深度解读，不堆整篇译文。

同事件：`cluster` 用标题相似度选主条目，其它源进 `eventPeers` / 详情页「事件聚合」。

单条 LLM 失败会跳过该条，不整份作废；大面积失败才中止发布。

简报 JSON 形状：

```json
{
  "date": "2026-08-22",
  "title": "AI Signal 每日情报 · 2026-08-22",
  "intro": "两句导语",
  "bullets": [{"title": "...", "text": "...", "refs": [1]}],
  "signals": [{ "recordId": "...", "sourceId": "...", "titleCn": "...", "summary": "...", "mediaAssets": {} }]
}
```

`publish` 把它写成 `site/data/brief-YYYY-MM-DD.json` 和 `brief-latest.json`。前端无 `?date=` 时读 latest。

---

## 周报与动向追踪

### 周报

`python -m src.weekly`。聚合近 `WEEKLY_LOOKBACK_DAYS`（默认 7）天**已分析**信号，可并入「周报待纳入」表。LLM 生成主题、综述和信号列表，写入周报表，并导出 `site/data/weekly-{weekId}.json` / `weekly-latest.json`。

周报工作流：北京时间每周一 11:30，先在自建 Mac Runner 上生成，再由 `ubuntu-latest` 部署 Pages 并推飞书。

### 动向追踪

`python -m src.timeline`。按表名查找或创建「追踪对象」「追踪事件」。对象类型：机构 / 人物 / 技术。字段含别名、关键词、排除词、回溯天数、最低影响分。

事件用 `entity_id + 信号 recordId` 去重。新建对象会回填历史；日报工作流每日增量更新 `timeline-latest.json`。配置台可增删对象并立即回填。公开站只读。

---

## 静态站与配置台

### 公开站 `site/`

`python -m src.publish --input output/daily-brief.json`：

- 复制 `index.html` → `site/index.html`
- 写近几日 `brief-*.json`、`brief-latest.json`
- 导出剥掉规则的 `sources.json`
- 镜像论文 PDF 页图、政策图、部分 OpenAI/虎嗅配图到 `site/media/`
- `build_site` 会先清空 `site/` 再重建，但会 stash/restore 周报与时间线 JSON，避免发布日报时丢掉周报

前端页面（`index.html`，单页应用）：

| `?page=` | 内容 | 数据 |
| --- | --- | --- |
| 默认 / `brief` | 今日简报、列表、详情 | `data/brief-latest.json` 或 `brief-{date}.json` |
| `sources` | 信号源表 | `data/sources.json`；本机优先 `/api/sources` |
| `tasks` | 周报 / 动向追踪 | `weekly-*.json`、`timeline-latest.json` |

本机 `localhost` / `127.0.0.1` 会被当成配置台主机，去打 `/api/sources`。若只开了 `python -m http.server`，接口 404，会退回静态快照。

### 配置台

```bash
python -m src.sources_api    # http://127.0.0.1:8787 ，只绑回环
```

同时托管 `site/` 和 API：`/api/sources`、周报待纳入、追踪对象。写回一级参数（状态、优先级、新建、删除），并同步信号源表自动化状态。新源一律 `experimental`。

`GET /api/sources/{recordId}/detail` 返回完整采集配置（`keyword_regex`、`min_content_chars`、`extra_config`、时间窗等），信号源表每行的「配置」按钮据此就地编辑，不必再开飞书多维表格。这些规则字段**只走这个按需接口**，不进 `build_payload`——那份载荷会被 `publish` 导出成公开站点的 `sources.json`。静态快照结构上就装不下一个实时接口的数据，所以不靠「记得别把字段加进那个 builder」来保证不泄露。

`PATCH /api/sources/{recordId}` 的约定：

- 从严校验。`process._safe_regex` 对坏正则、`sources.parse_lookback_hours` 对认不出的时间窗写法都会静默回落成默认值，这类错误在漏斗里跟「正常过滤」一模一样，所以拒在写入前。
- 改了 `source_view.RULE_FIELDS`（endpoint / fetch_method / 来源类型 / 时间窗 / 正文门槛 / 去重键 / keyword_regex / extra_config）中任一字段 → 自动退回 `experimental` 并同步信号源表；显式传 `status` 的请求以调用方为准。只改名称、备注、层级、维度不降级。
- 规则字段变了 → 自动跑 `tools.export_seed` 刷新种子，避免飞书与仓库快照漂移。
- 提交值与当前值相同的字段不写回；`extra_config` 的空格与键序差异不算改动，否则每次保存都会无故降级一个正在跑的源。
- 带非 localhost `Origin` 的写请求返回 403。这个服务持有飞书凭据，只绑回环挡不住浏览器里其他页面发来的跨站写请求。

无飞书时：`python -m src.source_view --seed` 用种子生成只读 `sources.json`。

---

## 飞书卡片

`python -m src.notify --input site/data/brief-latest.json`

- 接收群：`FEISHU_RECIPIENT_CHAT_IDS`（逗号分隔，优先）。配置后不再逐人私聊
- 接收人回退：`FEISHU_RECIPIENT_OPEN_IDS`（仅未配置群聊时使用），名称 `FEISHU_RECIPIENT_NAMES` 按序对应
- 查群：把机器人拉进群后 `python -m tools.list_bot_chats`
- 投递汇总：`FEISHU_DELIVERY_REPORT_OPEN_ID`
- 卡片：纯文字目录。居中色块标题 + 分板块的标题清单（最多 4 个板块、8 条），标题即原文链接，来源与形态是 `notation` 灰字；摘要只在网页，卡片和网页都不展示影响分 / 紧迫度
- 标题为了居中放进正文色块，卡片不再带 `header`
- 本地预览：`python -m tools.preview_card`，渲染 `notify.build_card` 的真实 JSON，不连飞书
- 发预览到群验收：`python -m tools.send_card_preview --chat-id oc_xxx`，只发消息不改简报发送状态
- 跳转：`PUBLIC_BASE_URL/?date=YYYY-MM-DD`；周报为 `?page=tasks&tab=report&week=`
- 默认一天不重发；`--force` 强制
- 代理断开时上传/发送有重试

---

## 模块地图

| 模块 | 职责 |
| --- | --- |
| `config` | 环境变量与阈值。`validate()` 要求飞书应用 + Base + 参数表 + 条目表 |
| `bootstrap` | 建表灌种；**import 时先读 `.env`**，其它多数入口要自己 `set -a; source .env` |
| `feishu` | tenant token、读写记录、统计回写、建表 |
| `sources` | 参数记录 → feed；载体类型推断；B 类；lookback 解析 |
| `typed_config` | 五张二级表 → `source_id` 过滤参数 |
| `rss` / `scrape` / `video` / `social` / `podcast` | 各通道抓取 |
| `process` | 清洗、白宫 AI 门、`format_for_feishu` |
| `paper_enrich` / `paper_fulltext` / `policy_document` | 论文质量、PDF 证据、白宫附件 |
| `cluster` | 同事件折叠与详情聚合 |
| `report` | 统一 LLM JSON（chat/completions，失败试 responses） |
| `daily` / `weekly` / `timeline` | 简报、周报、追踪 |
| `publish` | 静态站 |
| `notify` | 飞书卡片：分组、排版、群聊优先发送与投递汇总 |
| `source_view` / `sources_api` | 信号源展示模型与本机配置台（含规则字段编辑与校验） |
| `health` | 分源采集健康记录与体检报告（`python -m src.health`） |
| `openai_charts` | OpenAI 文章里的 Vega 图转图片 |
| `diag_*` | 单通道诊断，默认不写条目（播客统计仍回写；`--write` 才入库） |
| `analyze` | 源贡献统计 HTML/CSV |
| `backfill_*` | 历史字段修补（来源类型、发布时间、论文全文） |

诊断入口：`diag_scrape`、`diag_rss`、`diag_paper`、`diag_video`、`diag_social`、`diag_podcast`。预览：`podcast_preview`、`podcast_all_preview`、`diag_video --offline`。

---

## extra_config 与专用解析器

一级参数 `extra_config` 是 JSON 字符串，常见键：

| 键 | 作用 |
| --- | --- |
| `list_parser` | `anthropic_news`：按官网 News 表日期序抽链，避免 URL 字母序截断 `/news/claude-*`；`zhipu_news`：智谱卡片 |
| `max_articles` | 覆盖默认每源 8 条 |
| `keyword_min_hits` | 正文关键词最少命中（标题命中仍直接过） |
| `title_exclude_regex` | 标题丢弃 |
| `link_path_include` / `link_path_exclude` | 列表抽链路径过滤 |
| `force_direct` | 跳过 Jina |
| `allow_shallow_html` | 允许浅层 HTML |
| `policy_stage_extract` | 抽政策阶段/机构 |
| `document_pdf_enrich` / `max_document_pdfs` | 白宫 PDF |
| `channel_id` / `max_items` / `include_shorts` | YouTube |
| `modelscope_api` / `modelscope_mode` / `modelscope_owner` | ModelScope |
| `seed_api` / `seed_locale` / `seed_article_type` | 字节 Seed 博客 |
| `github` 相关 | 可被二级 GitHub 表覆盖 |
| `recent_days` / `min_upvotes` | HF/热榜热度窗 |

`papers-with-code-trending`、HF Papers 走论文列表解析，链接常改写到 `huggingface.co/papers/<id>`。

---

## GitHub Actions 与 Runner

日报 / 周报的 **build** 跑在自建 Runner：`[self-hosted, macOS, ARM64, feishu-ai-signal]`。原因：LLM 网关（如 `llm-center.modelbest.co`）只允许办公网，GitHub 托管出口会被拦。

部署 Pages 和发卡片在 `ubuntu-latest`，不再从公网打 LLM。

| 工作流 | 触发 | 做什么 |
| --- | --- | --- |
| `daily-brief.yml` | 每天 UTC 02:45（北京 10:45），可手动 | 测试 → `main` → `daily` → `publish` → `timeline` → 回写 `site/` → Pages → `notify` |
| `weekly-report.yml` | 周一 UTC 03:30 | 周报 + 部署 + 推送 |
| `pages-preview.yml` | 推送 `site/**` 或 `index.html` | 只部署当前仓库里的 `site/` |
| `ingest.yml` | 仅手动 | 单独采集 |
| `social-ingest.yml` | 每 2 小时 | X；采集器内部再限 P0/P1 间隔 |
| `podcast-ingest.yml` | 每天 | 播客，装 ffmpeg，超时更长 |

Runner 目录一般在本机 `~/actions-runner-feishu-ai-signal/`。可用 `./run.sh` 或 LaunchAgent `svc.sh`。Runner **offline** 时日报会一直 queued。访问 Google/YouTube 若 IPv6 黑洞，`src.main` 的 Media 可能长时间 `SYN_SENT`；本机补跑可用：

```bash
python -m src.main --method RSS --method Scrape
```

回写 `site/` 的 bot 提交常用 `[skip ci]`，避免再触发日报。只改前端时靠 `pages-preview.yml`。

GitHub Pages：仓库 Settings → Pages → Source = GitHub Actions。公网形如 `https://<user>.github.io/feishu-ai-signal/`。

---

## 环境变量

完整列表见 `.env.example`。必填：

- `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_BASE_ID`
- `FEISHU_PARAM_TABLE_ID` / `FEISHU_ENTRY_TABLE_ID`（bootstrap 打印）
- 生成简报：`LLM_API_KEY`（`LLM_BASE_URL` / `LLM_MODEL` 可选，默认 DeepSeek）
- 发卡片：`FEISHU_RECIPIENT_CHAT_IDS`（优先）或 `FEISHU_RECIPIENT_OPEN_IDS`、`PUBLIC_BASE_URL`

常用可选：各表 ID、`JINA_API_KEY`、`YOUTUBE_API_KEY`、`X_BEARER_TOKEN`、`ASR_*`、`DAILY_*`、`PAPER_*`、`MAX_ARXIV_ITEMS`。

`config.py` 在 **import 时**读环境。`bootstrap` 会先灌 `.env`；`python -m src.main` 等多数入口不会，请：

```bash
set -a && source .env && set +a
```

`.env`、`.env.*`、`.cursor/mcp.json`、含真实 Base ID 的规则已 gitignore。

---

## 本地怎么跑

### 前置

- Python 3.9+（本地常用仓库 `.venv`；CI 为 3.12）
- 飞书企业自建应用：多维表读写 + 机器人发消息
- OpenAI 兼容 LLM（简报 / 周报）
- 可选 Jina、YouTube、X、ASR

### 初始化

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # 先填 App ID / Secret / Base ID
set -a && source .env && set +a
python -m src.bootstrap       # --no-seed 只建表
# 把打印的 FEISHU_*_TABLE_ID 粘回 .env，再填 LLM_*
```

重复跑 bootstrap 只补缺失表/字段。

### 日常闭环

```bash
set -a && source .env && set +a
python -m src.main
python -m src.daily --output output/daily-brief.json
python -m src.publish --input output/daily-brief.json
python -m src.timeline --output site/data/timeline-latest.json
python -m src.sources_api          # 推荐：站点 + 实时参数表
# 或：python -m http.server 4173 --directory site
python -m src.notify --input site/data/brief-latest.json
```

只验证飞书写入：

```bash
python -m src.diag_scrape --write --source-id huxiu --limit 1
```

---

## 诊断、测试与种子导出

```bash
python -m src.diag_scrape [--engine auto|jina|direct] [--limit N] [--source-id xxx]
python -m src.diag_rss --source-id whitehouse-tech-actions
python -m src.diag_paper
python -m src.diag_video --source-id youtube-anthropic
python -m src.diag_social --source-id social-media
python -m src.diag_podcast --source-id podcast-latent-space [--write]
python -m unittest discover -s tests -v
python -m tools.export_seed
```

诊断报告多在 `output/`。`diag_scrape` 含 B 类和 experimental。加 `--write` 才写条目，但仍应 `sync_param_collect_stats`。

接入新源建议：

1. 在一级参数加行，`status=experimental`，配 endpoint、`fetch_method`、关键词、`lookback_window`、`extra_config`
2. 信号源表同步「待测」
3. `diag_*` 看列表 → 链接 → 正文 → 清洗漏斗
4. 确认后再 `active` / 「已接入」
5. `export_seed` 并提交

---

## 常见改动清单

**加一个官网新闻源（Scrape）**  
一级参数 + 信号源表 → 专用 `list_parser`（若通用抽链会按 URL 排序截断）→ 诊断 → active → export_seed。参考 `anthropic-news`。

**白宫又进了非 AI 政策**  
不要放宽 `science and technology`。改 `process.is_ai_policy_text` / 种子里的 `keyword_regex` 与 `title_exclude_regex`，并写回飞书两行 `whitehouse-tech-*`。已发布简报 JSON 要改站点数据才会从网页消失。

**简报论文太多**  
调 `DAILY_MAX_PAPERS`、`MAX_ARXIV_ITEMS`、`ARXIV_QUALITY_WEIGHT` 或论文二级表阈值。

**卡片没人收到**  
查机器人是否已进群、`chat_id` 是否当前应用可见、Runner/本机能否访问 `open.feishu.cn`、当日是否已发送需 `--force`。未配群聊时才查 `open_id` 是否当前应用签发。

**网站和飞书源状态不一致**  
公开站是上次 `publish` 的快照。本机用 `sources_api` 才实时。改完参数要重新 `publish` 或等日报。

**只改了 `index.html`**  
复制到 `site/index.html`（`pages-preview` 也会在 CI 里 `cp`），推 `main` 才会上线。未要求部署就不要推。

**前端模拟功能**  
评论、笔记、多模型辩论不要当成已接后端。不要为它们加飞书表，除非明确要产品化。

---

## 前端数据契约（给改 UI 的人）

`brief-*.json` 的 `signals[]` 常用字段：`recordId`、`sourceId`、`title`、`titleCn`、`source`、`url`、`category`、`contentType`、`tier`、`priority`、`publishedDate`、`summary`、`why`、`deepAnalysis`、`impact`、`novelty`、`actionability`、`urgency`、`tags`、`imageUrl`、`mediaAssets`（`images` / `videos` / `audio` / `documents`）、`eventAggregation`、`pdfUrl`、`paperVisualPages`。

`sources.json`：`generatedAt`、`origin`、`sources[]`（`id`、`name`、`status`、`statusLabel`、`format`、`tier`、`priority`、`fetchMethod`、`last`、`perDay`、`briefCount`）、`meta.writable`。

改 UI 后用浏览器走通简报列表 → 详情 → 信号源 → 任务视图；状态写在一处就检查其它页是否读同一份 JSON。

---

## 安全

不要提交 `.env` 或真实密钥。仓库若曾泄露密钥，先在飞书与模型供应商轮换。公开站不要带内部筛选正则。配置台只监听 `127.0.0.1`。
