# 飞书真实情报 Demo

这是一个可直接演示的真实 AI 情报闭环：

`飞书参数表 → 真实 RSS → 条目表 → LLM 分析回写 → 每日简报表 → GitHub Pages → 飞书消息卡片`

网页严格沿用 `ai-signal-dashboard/demo/index.html` 的视觉与交互。今日简报、信号列表和详情使用真实数据；评论、笔记、周报、辩论和会议等演示功能仍为本地模拟。

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
- 可选 LLM 覆盖：`LLM_BASE_URL`、`LLM_MODEL`

仓库 Settings → Pages 的 Source 选择 **GitHub Actions**。首次可在 Actions 中手动运行 `Daily AI Signal Brief`。

## 测试

```bash
python -m unittest discover -s tests -v
```

不要提交 `.env` 或任何真实密钥。若旧工作流曾包含明文密钥，应先在飞书与数据供应商后台轮换。
