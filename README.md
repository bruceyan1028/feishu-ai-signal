# 飞书真实情报 Demo

这是一个可直接演示的真实 AI 情报闭环：

`飞书参数表 → 真实 RSS → 条目表 → LLM 分析回写 → 每日简报表 → GitHub Pages → 飞书消息卡片`

网页严格沿用 `ai-signal-dashboard/demo/index.html` 的视觉与交互。今日简报、信号列表和详情使用真实数据；评论、笔记、周报、辩论和会议等演示功能仍为本地模拟。

## 类型化筛选配置表

不同类型的信息源筛选参数很多，为避免把稀疏字段全塞进参数表，按类型拆成 4 张独立配置表（多维表）：

| 类型 | 表名 | 默认 table_id | 环境变量 |
| --- | --- | --- | --- |
| 论文 | 论文筛选配置 | `tblhTzn8NyjeU779` | `FEISHU_PAPER_CONFIG_TABLE_ID` |
| 公众号 | 公众号筛选配置 | `tblNLmDgL2HpI29U` | `FEISHU_WECHAT_CONFIG_TABLE_ID` |
| 视频 | 视频筛选配置 | `tblh8FXqPevU7pBq` | `FEISHU_VIDEO_CONFIG_TABLE_ID` |
| 社交 | 社交筛选配置 | `tbl7lTtZRBajtmrQ` | `FEISHU_SOCIAL_CONFIG_TABLE_ID` |

约定与生效方式：

- **一源一行，主键 `source_id`。** 一个源出现在哪张表里，就说明它属于那个类型，走对应的类型过滤。
- **信号源表 / 参数表「来源类型」**（论文 / 纯网页 / 视频 / 社交媒体 / 公众号 / 播客 / 其他）为显式载体类型；采集时优先读该字段，再回落到论文配置表归属与 id/URL 启发式。可用 `python -m src.backfill_source_type` 回填。
- **留空即不过滤**，可随时增量填参数。
- **立即生效**（当前抓取层已有数据）：必含/排除关键词、正文/摘要最小字数、论文期刊会议白/黑名单（文本匹配）、从摘要推断的代码链接与论文信号分。
- **当前正式入库仍只抓 `active` + `RSS`**：OpenReview / HF Papers / PwC 等论文源若是 Scrape/API，会标成「论文」但不会自动进条目表，需改抓取方式或补适配器。
- **Scrape 诊断**：`python -m src.diag_scrape [--engine auto|jina|direct] [--limit N]`，只跑 Scrape、默认不写飞书，报告见 `output/scrape-pipeline-diag.json`。
- **arXiv 降噪（已启用）**：`lookback_window` 按配置生效（如 `24h`，不再强制 168h）；arXiv 同样走 `keyword_regex` 与长度过滤；论文配置表默认摘要≥500、排除课件/作业类词；轻量 `signal_score`（默认阈值 `ARXIV_MIN_SIGNAL_SCORE=55`，可在备注写 `min_signal_score=60` 覆盖）。
- **论文质量分（A/D/E）**：
  - A 录用：解析 arXiv comment / journal_ref 的 `Accepted to …`（仅常见会议/期刊），对照「期刊会议白/黑名单」
  - D 社区热度：Hugging Face Papers upvotes
  - E LLM：仅对简报候选打 `rigor / novelty_paper / relevance`，并与入库质量分合成最终分
  - 综合：`0.40*录用 + 0.25*社区 + 0.35*signal_score`（缺社区数据时按可用维归一化）；默认飞书可配 `最低质量分`；入库与简报均按质量分优先截断 `MAX_ARXIV_ITEMS`
  - arXiv 源按配置的 `lookback_window`（通常 `24h`）过滤；某类 RSS 在窗口内为空属正常（公告日滞后时下一轮再进）
  - 录用解析只认常见会议/期刊名；白名单仅在解析到可信录用时才硬过滤
  - 飞书可配：`要求已录用`、`最低质量分`、`最低社区热度`
  - 正式采集日志会输出清洗漏斗（各原因丢弃计数）
  - **已去掉作者影响力维**（不再调用 Semantic Scholar）
- **留钩子、待抓取层补数据后自动生效**：影响因子、阅读量、播放量、时长、粉丝/互动等。

## 数据规则

- 只采集参数表中 `status=active`、`fetch_method=RSS` 的来源。
- arXiv 每轮和每份简报合计最多 10 条。
- 原文链接、来源、发布时间、摘要和评分均保存在飞书多维表。
- 去重键沿用现有“条目表”；“每日简报”表首次运行时自动创建。

## 本地运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
set -a && source .env && set +a

python -m src.main
python -m src.daily --output output/daily-brief.json
python -m src.publish --input output/daily-brief.json
python -m http.server 4173 --directory site
```

打开 <http://localhost:4173>。发送卡片前还需配置公网 `PUBLIC_BASE_URL`：

```bash
python -m src.notify --input output/daily-brief.json
# 演示时强制重发
python -m src.notify --input output/daily-brief.json --force
```

## 飞书应用权限

企业自建应用需要启用机器人，并开通：

- 多维表格数据表与记录的读取、新增、更新权限；
- 以应用身份发送消息权限；
- 现有多维表格需授权给该应用。

接收人使用用户 `open_id`。多人用英文逗号配置在
`FEISHU_RECIPIENT_OPEN_IDS=ou_xxx,ou_yyy`；单人变量 `FEISHU_RECIPIENT_OPEN_ID` 仍兼容。

## GitHub Pages 自动化

仓库包含两个工作流：

- `ingest.yml`：每 6 小时采集一次真实 RSS。
- `daily-brief.yml`：北京时间每天 09:00 采集、分析、部署 Pages，再发送飞书卡片；也支持手动运行和强制重发。

在 GitHub Actions Secrets 中配置：

- 必填：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`LLM_API_KEY`、`FEISHU_RECIPIENT_OPEN_IDS`
- 可选：`FEISHU_BASE_ID`、`FEISHU_PARAM_TABLE_ID`、`FEISHU_ENTRY_TABLE_ID`、`FEISHU_BRIEF_TABLE_ID`
- 可选论文质量：`PAPER_QUALITY_MIN_SCORE`、`MAX_ARXIV_ITEMS`、`PAPER_ENRICH_ENABLED`
- 可选 LLM 覆盖：`LLM_BASE_URL`、`LLM_MODEL`

仓库 Settings → Pages 的 Source 选择 **GitHub Actions**。首次可在 Actions 中手动运行 `Daily AI Signal Brief`。

## 测试

```bash
python -m unittest discover -s tests -v
```

不要提交 `.env` 或任何真实密钥。若旧工作流曾包含明文密钥，应先在飞书与数据供应商后台轮换。
