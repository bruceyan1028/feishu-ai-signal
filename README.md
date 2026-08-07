# 飞书真实情报 Demo

一个可端到端跑通的 AI 情报闭环：

`飞书参数表 → RSS / Scrape / Media / Social / Podcast 抓取 → 条目表 → LLM 分析回写 → 每日简报表 → GitHub Pages → 飞书消息卡片`

网页沿用 `ai-signal-dashboard/demo/index.html` 的视觉与交互；今日简报、信号列表与详情为真实数据，评论/笔记/周报等演示功能仍为本地模拟。

---

## 快速开始（本地部署）

> 目标：在自己的机器上把「采集 → 分析 → 生成网页」跑通。发送飞书卡片、GitHub Pages 部署为可选进阶步骤。

### 0. 前置条件

- Python 3.11+（CI 用 3.12）
- 一个**飞书企业自建应用**（拿 App ID / App Secret）
- 一个 **OpenAI 兼容的 LLM**（DeepSeek / 通义 / OpenAI 等，仅生成每日简报时需要）
- 可选：[Jina Reader](https://jina.ai/reader/) 的 `JINA_API_KEY`（Scrape 抓取更稳定）

### 1. 克隆与安装

```bash
git clone https://github.com/bruceyan1028/feishu-ai-signal.git
cd feishu-ai-signal

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 准备飞书应用与多维表

1. 在[飞书开放平台](https://open.feishu.cn/)创建**企业自建应用**，开启**机器人**能力，发布版本。
2. 开通权限：多维表格数据表/记录的**读取、新增、更新**（含建表 `bitable:app`）；以应用身份**发送消息**（`im:message`）。
3. 在飞书里**新建一个空的多维表格（Base）**，把它**授权给你的应用**（Base 右上角「…」→ 添加文档应用 → 选中你的应用）。
4. 从浏览器地址栏取该 Base 的 `app_token`：形如 `https://xxx.feishu.cn/base/<app_token>?...`，`base/` 后面那一串就是 `FEISHU_BASE_ID`。

> 无需手动建任何数据表、也无需复制作者的模板——下一步的初始化命令会在**你自己的 Base** 里自动建好全部 9 张表和字段，并写入与母版一致的默认配置。

### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，先填这三项：FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_BASE_ID
```

### 4. 一键初始化多维表（重点）

```bash
set -a && source .env && set +a
python -m src.bootstrap          # 加 --no-seed 可只建表不写默认数据
```

命令会在你的 Base 里**幂等创建** 9 张表（信号源表 / 一级参数 / 条目表 / 每日简报 + 论文 / 公众号 / 视频 / 社媒 / GitHub 5 张二级参数表），
表名、字段与作者母版**完全一致**，并默认把母版的信号源清单与各级参数**写入空表**，让你的库开箱即与母版同构同内容（`src/seed_default.json`，条目表不写）。
结尾会打印一段可直接粘贴的 `FEISHU_*_TABLE_ID`，**把它们复制回 `.env` 覆盖占位符**，再填上 LLM 相关变量：

| 变量 | 说明 |
| --- | --- |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 必填，飞书自建应用凭证 |
| `FEISHU_BASE_ID` | 你自己多维表的 app_token（必填） |
| `FEISHU_PARAM_TABLE_ID` / `FEISHU_ENTRY_TABLE_ID` / `FEISHU_BRIEF_TABLE_ID` | bootstrap 输出，粘回来 |
| `FEISHU_*_CONFIG_TABLE_ID` | 5 张类型化配置表，同样粘 bootstrap 输出 |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 生成每日简报用（OpenAI 兼容，如 DeepSeek / 通义 / OpenAI） |
| `JINA_API_KEY` | 可选，Scrape 抓取更稳定 |
| `X_BEARER_TOKEN` | 可选，启用 X 白名单账号 Social 源时必填 |
| `ASR_API_KEY` / `ASR_BASE_URL` / `ASR_MODEL` | 可选，播客没有公开 transcript 时调用的托管转录接口 |
| `FEISHU_RECIPIENT_OPEN_IDS` | 卡片接收人 `open_id`，逗号分隔（发卡片时需要） |
| `PUBLIC_BASE_URL` | 卡片里跳转的公网站点地址 |

> 重复运行 `python -m src.bootstrap` 是安全的：只会补齐缺失的表/字段，不会清空或重建已有数据。日后升级若新增字段，再跑一次即可对齐。

### 5. 跑一遍流水线

```bash
# 加载 .env 到当前 shell
set -a && source .env && set +a

# ① 采集 RSS 源写入条目表
python -m src.main

# ② 采集 Scrape 源（公众号 / GitHub热榜 / HF·PwC 论文等）写入条目表
python -m src.diag_scrape --write

# ③ 只读诊断 experimental X 白名单源（需要 X_BEARER_TOKEN）
python -m src.diag_social --source-id social-media

# ④ 诊断播客 RSS、转录和完整摘要；加 --write 才写条目
python -m src.diag_podcast --source-id podcast-latent-space [--write]

# ⑤ LLM 分析近七日候选，生成每日简报（需要 LLM_API_KEY）
python -m src.daily --output output/daily-brief.json

# ⑥ 从飞书拉数据生成静态站点
python -m src.publish --input output/daily-brief.json

# ⑥ 本地预览
python -m http.server 4173 --directory site
```

打开 <http://localhost:4173> 查看。

### 6. 信号源页面与本地配置台

站点的「信号源」页读的是 `site/data/sources.json`，由 `src.publish` 随简报一起导出，
内容是参数表里 108 个源的状态、层级、优先级、最近采集时间，以及各源近 7 期真正进入
简报的条数。筛选规则（`keyword_regex`、`min_content_chars`、`dedup_key`、`extra_config`）
不会导出——站点是公开的。

公开站上这一页只读。要在网页里直接改配置，在本机启动配置台：

```bash
python -m src.sources_api            # http://127.0.0.1:8787
python -m src.source_view --seed     # 无飞书凭据时，用仓库快照生成 sources.json 预览
```

配置台只监听回环地址（它持有飞书凭据），提供 `/api/sources` 的读写，改动直接写回一级
参数表。采集本身仍然跑在 GitHub Actions 上，配置台不参与定时任务。新增的源一律先落
`experimental`，诊断通过后再在页面上改成「已接入」。

### 7.（可选）发送飞书卡片

```bash
python -m src.notify --input output/daily-brief.json
python -m src.notify --input output/daily-brief.json --force   # 强制重发
```

### 只想验证飞书连通性？

```bash
# 只跑一个源、写入条目表，最快确认「凭证 + 表权限 + 写入」是否 OK
python -m src.diag_scrape --write --source-id huxiu --limit 1
```

---

## 信息源类型与筛选

参数表（一级）字段 `来源类型` 为显式载体类型：`论文 / 纯网页 / 视频 / 社交媒体 / 公众号 / Github热榜 / 播客 / 其他`。采集时优先读该字段，再回落到配置表归属与 id/URL 启发式。可用 `python -m src.backfill_source_type` 回填。

不同类型的筛选参数很多，按类型拆成独立配置表（一源一行，主键 `source_id`；表内没配到的源即不按该类型过滤）。这些表都由 `python -m src.bootstrap` 自动创建，`table_id` 见其输出，对应环境变量如下：

| 类型 | 环境变量 |
| --- | --- |
| 论文 | `FEISHU_PAPER_CONFIG_TABLE_ID` |
| 公众号 | `FEISHU_WECHAT_CONFIG_TABLE_ID` |
| 视频 | `FEISHU_VIDEO_CONFIG_TABLE_ID` |
| 社交 | `FEISHU_SOCIAL_CONFIG_TABLE_ID` |
| GitHub热榜 | `FEISHU_GITHUB_CONFIG_TABLE_ID` |

### 关键词过滤（含密度门）

- 每个源可配 `keyword_regex`（AI 相关词的正则）；正文/摘要过短会被丢弃。
- `extra_config.keyword_min_hits`（默认 1）：**标题命中直接通过**；否则正文关键词命中次数需 ≥ 该值。用于压制正文导航/推荐位蹭词造成的假阳性（公众号站点常见）。噪音大的源可设 2+。
- `extra_config.link_path_include`：列表页抽链时只保留匹配路径的 URL（如 `^/article/`），挡掉个人页/标签页。
- `extra_config.force_direct`：强制直连抓取（跳过 Jina），适合 Jina 渲染后抽链失败的站点。

### X 白名单账号

- `Social` 使用 X API v2 的账号时间线与 `since_id` 增量水位；水位存于一级参数的「采集游标」，条目成功处理后才推进。
- 二级参数-社媒维护账号白名单、P0/P1 分级、回复/转发策略、评分阈值与账号日上限。默认种子含 10 个 P0 官方账号，保持 `experimental`。
- 帖子依次经过硬过滤、0–100 可解释评分和临界区 LLM 精筛；原创 thread 会按 `conversation_id` 合并，去重键为 `x:{post_id}`。
- 先运行 `python -m src.diag_social` 验收读取数、筛选漏斗和样本；链路通过后再把一级参数改为 `active`，并同步将信号源表状态改为「已接入」。

### 播客完整摘要

- `Podcast` 每档白名单节目对应一条 RSS 源；节目准入本身就是筛选器，白名单内每期处理。
- 文本按 `podcast:transcript` → 节目页文字稿/YouTube 字幕 → 托管 ASR 的顺序获取。长音频由 ffmpeg 切片，ASR 使用独立的 OpenAI-compatible 配置；未配置 ASR 时允许降级为“结构化官方简介”，必须清理宣传尾巴并明确标记为非完整转录。
- 逐字稿先按时间段归纳，再合并成带时间戳的证据稿、300–600 字完整中文摘要和深度解读；逐字稿仅作中间数据。
- 先用 `python -m src.diag_podcast --source-id ... --write` 验收并回写采集统计，通过后再同步升级两张源表状态。当前已验收 `硅谷 101`、`十字路口 Crossing`、`No Priors`；其余源保持 `experimental/待测`。

### 论文质量（A/D/E）

- **A 录用**：解析 arXiv comment / journal_ref 的 `Accepted to …`（仅常见会议/期刊），对照「期刊会议白/黑名单」。
- **D 社区热度**：Hugging Face Papers upvotes。
- **E LLM**：仅对简报候选打 `rigor / novelty_paper / relevance`，与入库质量分合成最终分。
- 综合分 `0.40*录用 + 0.25*社区 + 0.35*signal_score`（缺维时归一化）；默认阈值 `PAPER_QUALITY_MIN_SCORE=60`。
- **arXiv 长尾降噪**：预印本默认要求**有社区热度**（HF upvotes）才保留，已录用会议/顶刊豁免；`ARXIV_REQUIRE_COMMUNITY_HEAT=0` 可关闭。轻量 `signal_score` 阈值 `ARXIV_MIN_SIGNAL_SCORE=55`，可在配置表备注写 `min_signal_score=60` 覆盖。
- arXiv 每轮和每份简报合计最多 `MAX_ARXIV_ITEMS`（默认 10）条，按质量分优先截断。
- 已去掉作者影响力维（不再调用 Semantic Scholar）。

---

## 数据规则

- **采集**：参数表中 `status=active` 的 `RSS / Scrape / Media / Social` 源走默认 `python -m src.main`；Podcast 由独立工作流调用 `python -m src.main --method Podcast`。experimental 源先用对应诊断命令验收。
- **简报候选**：近七日、`active` 的 **RSS + Scrape + Media + Social + Podcast** 源。官方源优先，arXiv 与视频/播客分别受数量限制。
- 原文链接、来源、发布时间、摘要、评分、路由来源均写入条目表；去重键沿用条目表；每日简报表首次运行自动创建。

---

## GitHub Pages 自动化

四个工作流：

- `daily-brief.yml`：北京时间每天 09:00 采集 RSS、LLM 分析、部署 Pages、发送飞书卡片；支持手动运行（`workflow_dispatch`）与强制重发。
- `ingest.yml`：仅手动触发（`workflow_dispatch`）的 RSS 采集，日常采集已并入 `daily-brief.yml`（每日一次）。
- `social-ingest.yml`：每 2 小时触发 X 白名单采集；采集器内部将 P0/P1 分别限制为 2 小时/4 小时间隔。
- `podcast-ingest.yml`：每天独立处理白名单播客，安装 ffmpeg，并为长音频转录保留更长超时。

在 GitHub Actions Secrets 配置：

- 必填：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`LLM_API_KEY`、`FEISHU_RECIPIENT_OPEN_IDS`
- 可选：`FEISHU_BASE_ID`、`FEISHU_PARAM_TABLE_ID`、`FEISHU_ENTRY_TABLE_ID`、`FEISHU_BRIEF_TABLE_ID`
- 可选论文/LLM：`PAPER_QUALITY_MIN_SCORE`、`MAX_ARXIV_ITEMS`、`PAPER_ENRICH_ENABLED`、`LLM_BASE_URL`、`LLM_MODEL`
- 可选社媒：`X_BEARER_TOKEN`（启用 Social 源时必填）
- 可选播客：`ASR_API_KEY`、`ASR_BASE_URL`、`ASR_MODEL`（无公开 transcript 时必填）

仓库 Settings → Pages 的 Source 选择 **GitHub Actions**，首次可在 Actions 手动运行 `Daily AI Signal Brief`。

> 注：本地 `python -m src.daily` 若因 LLM 端点仅限内网/CI 而报 405，改用 CI 的 `daily-brief` 工作流生成简报即可。

---

## 诊断与测试

```bash
# Scrape 源诊断（默认不写飞书），报告见 output/scrape-pipeline-diag.json
python -m src.diag_scrape [--engine auto|jina|direct] [--limit N] [--source-id xxx]

# 论文富集诊断
python -m src.diag_paper

# X 白名单源只读诊断
python -m src.diag_social --source-id social-media

# 播客 RSS / transcript / ASR / 摘要诊断（采集统计始终回写；加 --write 写条目）
python -m src.diag_podcast --source-id podcast-latent-space [--write]

# 单元测试
python -m unittest discover -s tests -v
```

不要提交 `.env` 或任何真实密钥。若旧提交曾包含明文密钥，应先在飞书与数据供应商后台轮换。
