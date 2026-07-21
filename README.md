# 飞书真实情报 Demo

一个可端到端跑通的 AI 情报闭环：

`飞书参数表 → 真实 RSS / Scrape 抓取 → 条目表 → LLM 分析回写 → 每日简报表 → GitHub Pages → 飞书消息卡片`

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

1. 在[飞书开放平台](https://open.feishu.cn/)创建**企业自建应用**，开启**机器人**能力。
2. 开通权限：多维表格数据表/记录的**读取、新增、更新**；以应用身份**发送消息**（`im:message`）。
3. 准备一个多维表（Base），至少包含：
   - **参数表**（信号源配置）、**条目表**（采集入库）、**每日简报表**（留空会自动创建）。
   - 5 张**类型化筛选配置表**（论文 / 公众号 / 视频 / 社交 / GitHub，见下文），可留空默认。
   - 把该 Base **授权给你的应用**（多维表右上角「…」→ 添加文档应用）。
4. 从各表 URL 里取 `base_id`（`app_token`）与 `table_id` 填进 `.env`。

> 默认 `.env.example` 里的 base/table id 指向**作者的多维表**，其他人**没有写入权限**，务必换成你自己的。最省事的做法是把作者的 Base「另存为副本」到自己空间，字段结构即可保持一致。

### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填 FEISHU_APP_ID / FEISHU_APP_SECRET / 你自己的 base、table id / LLM_API_KEY
```

关键变量：

| 变量 | 说明 |
| --- | --- |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 必填，应用凭证 |
| `FEISHU_BASE_ID` | 多维表 app_token |
| `FEISHU_PARAM_TABLE_ID` / `FEISHU_ENTRY_TABLE_ID` | 参数表 / 条目表 |
| `FEISHU_BRIEF_TABLE_ID` | 每日简报表，留空自动创建 |
| `FEISHU_*_CONFIG_TABLE_ID` | 5 张类型化配置表，可留空用默认 |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 生成每日简报用（OpenAI 兼容） |
| `JINA_API_KEY` | 可选，Scrape 抓取 |
| `FEISHU_RECIPIENT_OPEN_IDS` | 卡片接收人 `open_id`，逗号分隔（发卡片时需要） |
| `PUBLIC_BASE_URL` | 卡片里跳转的公网站点地址 |

### 4. 跑一遍流水线

```bash
# 加载 .env 到当前 shell
set -a && source .env && set +a

# ① 采集 RSS 源写入条目表
python -m src.main

# ② 采集 Scrape 源（公众号 / GitHub热榜 / HF·PwC 论文等）写入条目表
python -m src.diag_scrape --write

# ③ LLM 分析近七日候选，生成每日简报（需要 LLM_API_KEY）
python -m src.daily --output output/daily-brief.json

# ④ 从飞书拉数据生成静态站点
python -m src.publish --input output/daily-brief.json

# ⑤ 本地预览
python -m http.server 4173 --directory site
```

打开 <http://localhost:4173> 查看。

### 5.（可选）发送飞书卡片

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

不同类型的筛选参数很多，按类型拆成独立配置表（一源一行，主键 `source_id`；留空即不过滤）：

| 类型 | 默认 table_id | 环境变量 |
| --- | --- | --- |
| 论文 | `tblhTzn8NyjeU779` | `FEISHU_PAPER_CONFIG_TABLE_ID` |
| 公众号 | `tblNLmDgL2HpI29U` | `FEISHU_WECHAT_CONFIG_TABLE_ID` |
| 视频 | `tblh8FXqPevU7pBq` | `FEISHU_VIDEO_CONFIG_TABLE_ID` |
| 社交 | `tbl7lTtZRBajtmrQ` | `FEISHU_SOCIAL_CONFIG_TABLE_ID` |
| GitHub热榜 | `tblpZTJWyTRzkQyF` | `FEISHU_GITHUB_CONFIG_TABLE_ID` |

### 关键词过滤（含密度门）

- 每个源可配 `keyword_regex`（AI 相关词的正则）；正文/摘要过短会被丢弃。
- `extra_config.keyword_min_hits`（默认 1）：**标题命中直接通过**；否则正文关键词命中次数需 ≥ 该值。用于压制正文导航/推荐位蹭词造成的假阳性（公众号站点常见）。噪音大的源可设 2+。
- `extra_config.link_path_include`：列表页抽链时只保留匹配路径的 URL（如 `^/article/`），挡掉个人页/标签页。
- `extra_config.force_direct`：强制直连抓取（跳过 Jina），适合 Jina 渲染后抽链失败的站点。

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

- **采集**：参数表中 `status=active` 的 `RSS` 源走 `python -m src.main`；`Scrape` 源走 `python -m src.diag_scrape --write`。
- **简报候选**：近七日、`active` 的 **RSS + Scrape** 源（公众号、GitHub热榜、HF/PwC 论文都能进简报）。官方源优先，arXiv 受 `MAX_ARXIV_ITEMS` 限制。
- 原文链接、来源、发布时间、摘要、评分、路由来源（RSS/Scrape）均写入条目表；去重键沿用条目表；每日简报表首次运行自动创建。

---

## GitHub Pages 自动化

两个工作流：

- `daily-brief.yml`：北京时间每天 09:00 采集 RSS、LLM 分析、部署 Pages、发送飞书卡片；支持手动运行（`workflow_dispatch`）与强制重发。
- `ingest.yml`：仅手动触发（`workflow_dispatch`）的 RSS 采集，日常采集已并入 `daily-brief.yml`（每日一次）。

在 GitHub Actions Secrets 配置：

- 必填：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`LLM_API_KEY`、`FEISHU_RECIPIENT_OPEN_IDS`
- 可选：`FEISHU_BASE_ID`、`FEISHU_PARAM_TABLE_ID`、`FEISHU_ENTRY_TABLE_ID`、`FEISHU_BRIEF_TABLE_ID`
- 可选论文/LLM：`PAPER_QUALITY_MIN_SCORE`、`MAX_ARXIV_ITEMS`、`PAPER_ENRICH_ENABLED`、`LLM_BASE_URL`、`LLM_MODEL`

仓库 Settings → Pages 的 Source 选择 **GitHub Actions**，首次可在 Actions 手动运行 `Daily AI Signal Brief`。

> 注：本地 `python -m src.daily` 若因 LLM 端点仅限内网/CI 而报 405，改用 CI 的 `daily-brief` 工作流生成简报即可。

---

## 诊断与测试

```bash
# Scrape 源诊断（默认不写飞书），报告见 output/scrape-pipeline-diag.json
python -m src.diag_scrape [--engine auto|jina|direct] [--limit N] [--source-id xxx]

# 论文富集诊断
python -m src.diag_paper

# 单元测试
python -m unittest discover -s tests -v
```

不要提交 `.env` 或任何真实密钥。若旧提交曾包含明文密钥，应先在飞书与数据供应商后台轮换。
