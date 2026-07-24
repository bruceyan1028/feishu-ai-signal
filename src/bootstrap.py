"""一键初始化：在「你自己的」飞书多维表格 base 里建好本项目需要的全部表和字段。

面向「下载即用」的新用户：
  1. 在飞书开放平台建一个自建应用，拿到 App ID / App Secret，并开通多维表格读写权限。
  2. 新建一个多维表格（Base），从地址栏复制 base 的 app_token（形如 bascnXXXX / RuI1b...）。
  3. 把上面三样填进 .env 的 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_BASE_ID。
  4. 运行：  python -m src.bootstrap
     脚本会在你的 base 里幂等创建 8 张表（参数表 / 条目表 / 每日简报 + 5 张筛选配置表），
     并把每张表的 table_id 以「可直接粘进 .env」的形式打印出来。
  5. 把打印出的 FEISHU_*_TABLE_ID 复制进 .env，再填上 LLM_API_KEY / LLM_BASE_URL 即可跑流水线。

可选：加 --seed 往「参数表」写入几个稳妥的示例 RSS 源，方便第一次跑通看到数据。

幂等：重复运行只补缺失的表/字段，不会重建或清空已有数据。
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """在 import config 之前，把 .env 灌进 os.environ，
    这样 config 模块在导入时就能读到用户自己的凭证与 base_id。"""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

import argparse  # noqa: E402
import logging  # noqa: E402
from typing import Any  # noqa: E402

from . import config, feishu  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bootstrap")

# 飞书字段类型码
TEXT = 1
NUMBER = 2
SELECT = 3
MULTI = 4
DATETIME = 5
CHECKBOX = 7
URL = 15

_FETCH_METHODS = ("RSS", "Scrape", "Bridge", "API", "Manual")
_STATUSES = ("active", "experimental", "paused")
_PRIORITIES = ("P0", "P1", "P2")
_URGENCY = ("Pending", "高", "中", "低")
_ENTRY_STATUS = ("待分析", "已分析")


def _f(name: str, ftype: int, options: tuple[str, ...] | None = None) -> dict[str, Any]:
    field: dict[str, Any] = {"field_name": name, "type": ftype}
    if options:
        field["property"] = {"options": [{"name": o} for o in options]}
    return field


# 参数表：源配置 + 采集统计回写字段（对应 sources._base_feed 读取 & feishu 统计回写）
_PARAM_FIELDS = [
    _f("source_id", TEXT),  # 主键
    _f("name", TEXT),
    # endpoint 存纯文本 URL：sources.cell() 对 URL 字段会返回显示文本而非 link，
    # 用文本字段直接存原始地址，读出来即可用。
    _f("endpoint", TEXT),
    _f("fetch_method", SELECT, _FETCH_METHODS),
    _f("status", SELECT, _STATUSES),
    _f("来源类型", SELECT, feishu.SIGNAL_FORMAT_OPTIONS),
    _f("dimension", TEXT),
    _f("tier", TEXT),
    _f("priority", SELECT, _PRIORITIES),
    _f("lookback_window", TEXT),
    _f("keyword_regex", TEXT),
    _f("dedup_key", TEXT),
    _f("extra_config", TEXT),
    _f("min_content_chars", NUMBER),
    _f("通过", CHECKBOX),
    _f("最近采集时间", DATETIME),
    _f("条目数", NUMBER),
    _f("查重过滤", NUMBER),
    _f("时间窗过滤", NUMBER),
]

# 条目表：对应 process.format_for_feishu 与 daily.py 写回的全部字段
_ENTRY_FIELDS = [
    _f("标题", TEXT),  # 主键
    _f("链接", URL),
    _f("来源", TEXT),
    _f("来源类型", SELECT, feishu.SIGNAL_FORMAT_OPTIONS),
    _f("路由来源", TEXT),
    _f("分类", TEXT),
    _f("层级", TEXT),
    _f("发布时间", DATETIME),
    _f("采集时间", DATETIME),
    _f("原文", TEXT),
    _f("中文标题", TEXT),
    _f("中文摘要", TEXT),
    _f("为何重要", TEXT),
    _f("主题", MULTI, ("AI", "LLM", "Agent")),
    _f("影响分", NUMBER),
    _f("新颖度", NUMBER),
    _f("可行动性", NUMBER),
    _f("紧迫度", SELECT, _URGENCY),
    _f("状态", SELECT, _ENTRY_STATUS),
    _f("去重键", TEXT),
    _f("source_id", TEXT),
    _f("媒体资源", TEXT),
    _f("图片链接", URL),
    _f("质量分", NUMBER),
    _f("录用会议", TEXT),
    _f("社区热度", NUMBER),
    _f("论文指标", TEXT),
]


def _config_fields(entity_type: str) -> list[dict[str, Any]]:
    """按 typed_config._SCHEMAS 生成某张筛选配置表的字段（source_id + schema + 备注）。"""
    from . import typed_config as tcfg

    fields = [_f("source_id", TEXT)]  # 主键
    for field_name, (_key, parser) in tcfg._SCHEMAS[entity_type].items():
        if parser is tcfg._as_num:
            ftype = NUMBER
        elif parser is tcfg._as_bool:
            ftype = CHECKBOX
        else:
            ftype = TEXT
        fields.append(_f(field_name, ftype))
    fields.append(_f("备注", TEXT))
    return fields


# (env 变量名, 表名, 字段列表)。顺序即创建顺序。
def _table_specs() -> list[tuple[str, str, list[dict[str, Any]]]]:
    return [
        ("FEISHU_PARAM_TABLE_ID", "参数表", _PARAM_FIELDS),
        ("FEISHU_ENTRY_TABLE_ID", "条目表", _ENTRY_FIELDS),
        ("FEISHU_PAPER_CONFIG_TABLE_ID", "论文筛选配置", _config_fields("paper")),
        ("FEISHU_WECHAT_CONFIG_TABLE_ID", "公众号筛选配置", _config_fields("wechat")),
        ("FEISHU_VIDEO_CONFIG_TABLE_ID", "视频筛选配置", _config_fields("video")),
        ("FEISHU_SOCIAL_CONFIG_TABLE_ID", "社交筛选配置", _config_fields("social")),
        ("FEISHU_GITHUB_CONFIG_TABLE_ID", "GitHub筛选配置", _config_fields("github")),
    ]


def _create_table(token: str, name: str, fields: list[dict[str, Any]]) -> str:
    url = f"{config.FEISHU_HOST}/open-apis/bitable/v1/apps/{config.FEISHU_BASE_ID}/tables"
    resp = feishu._SESSION.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json={"table": {"name": name, "default_view_name": "全部", "fields": fields}},
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise feishu.FeishuError(f"建表 {name} 失败: {data.get('code')} {data.get('msg')}")
    payload = data.get("data") or {}
    return str(payload.get("table_id") or (payload.get("table") or {}).get("table_id"))


def _add_field(token: str, table_id: str, field: dict[str, Any]) -> None:
    url = (
        f"{config.FEISHU_HOST}/open-apis/bitable/v1/apps/{config.FEISHU_BASE_ID}"
        f"/tables/{table_id}/fields"
    )
    resp = feishu._SESSION.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json=field,
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise feishu.FeishuError(
            f"给表 {table_id} 补字段 {field.get('field_name')} 失败: {data.get('code')} {data.get('msg')}"
        )


def _ensure_table(
    token: str, name: str, fields: list[dict[str, Any]], dry_run: bool = False
) -> tuple[str, bool]:
    """按表名幂等建表：存在则补缺失字段，不存在则整表创建。返回 (table_id, created)。"""
    by_name = {str(t.get("name")): str(t.get("table_id")) for t in feishu.list_tables(token)}
    if name in by_name:
        table_id = by_name[name]
        have = {f.get("field_name") for f in feishu._list_fields(token, table_id)}
        missing = [f for f in fields if f["field_name"] not in have]
        if dry_run:
            log.info(
                "[dry-run] 表『%s』已存在（%s），将补 %d 个字段：%s",
                name, table_id, len(missing), [f["field_name"] for f in missing],
            )
            return table_id, False
        for field in missing:
            _add_field(token, table_id, field)
        log.info("表『%s』已存在（%s），补齐 %d 个缺失字段", name, table_id, len(missing))
        return table_id, False
    if dry_run:
        log.info("[dry-run] 将新建表『%s』（%d 字段）", name, len(fields))
        return "(待创建)", True
    table_id = _create_table(token, name, fields)
    log.info("已创建表『%s』→ %s（%d 字段）", name, table_id, len(fields))
    return table_id, True


# 示例源：第一次跑通用，端点均为稳定的公开 RSS。用户可随后在参数表增删改。
_SEED_SOURCES = [
    {
        "source_id": "huggingface-blog",
        "name": "Hugging Face Blog",
        "endpoint": "https://huggingface.co/blog/feed.xml",
        "fetch_method": "RSS",
        "status": "active",
        "来源类型": "纯网页",
        "dimension": "开源生态",
        "priority": "P1",
    },
    {
        "source_id": "bair-blog",
        "name": "Berkeley AI Research Blog",
        "endpoint": "https://bair.berkeley.edu/blog/feed.xml",
        "fetch_method": "RSS",
        "status": "active",
        "来源类型": "纯网页",
        "dimension": "学术前沿",
        "priority": "P1",
    },
    {
        "source_id": "arxiv-cs-ai",
        "name": "arXiv cs.AI",
        "endpoint": "http://export.arxiv.org/rss/cs.AI",
        "fetch_method": "RSS",
        "status": "active",
        "来源类型": "论文",
        "dimension": "学术前沿",
        "priority": "P2",
    },
]


def _seed_param_sources(token: str, param_table_id: str) -> int:
    """仅当参数表为空时写入示例源，避免覆盖用户已有配置。"""
    existing = feishu.read_all_records(token, param_table_id, field_names=["source_id"])
    if existing:
        log.info("参数表已有 %d 行，跳过示例源写入", len(existing))
        return 0
    url = (
        f"{config.FEISHU_HOST}/open-apis/bitable/v1/apps/{config.FEISHU_BASE_ID}"
        f"/tables/{param_table_id}/records/batch_create"
    )
    resp = feishu._SESSION.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json={"records": [{"fields": s} for s in _SEED_SOURCES]},
        timeout=60,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise feishu.FeishuError(f"写入示例源失败: {data.get('code')} {data.get('msg')}")
    log.info("已写入 %d 个示例源", len(_SEED_SOURCES))
    return len(_SEED_SOURCES)


def run(seed: bool = False, dry_run: bool = False) -> dict[str, str]:
    if not config.FEISHU_APP_ID or config.FEISHU_APP_ID.startswith(("your_", "cli_xxxx")):
        raise config.ConfigError("请先在 .env 填写 FEISHU_APP_ID")
    if not config.FEISHU_APP_SECRET or config.FEISHU_APP_SECRET.startswith("your_"):
        raise config.ConfigError("请先在 .env 填写 FEISHU_APP_SECRET")
    if not config.FEISHU_BASE_ID or config.FEISHU_BASE_ID.startswith("your_"):
        raise config.ConfigError("请先在 .env 填写 FEISHU_BASE_ID（你自己多维表格的 app_token）")

    token = feishu.get_tenant_access_token()
    log.info("已获取 tenant_access_token，目标 base=%s%s", config.FEISHU_BASE_ID,
             "（dry-run，不做任何写入）" if dry_run else "")

    results: dict[str, str] = {}
    for env_var, name, fields in _table_specs():
        table_id, _created = _ensure_table(token, name, fields, dry_run=dry_run)
        results[env_var] = table_id

    # 每日简报表：复用 feishu 里已有的幂等建表逻辑（按名『每日简报』查找/创建）
    if dry_run:
        existing_brief = next(
            (str(t["table_id"]) for t in feishu.list_tables(token) if t.get("name") == "每日简报"),
            "(待创建)",
        )
        results["FEISHU_BRIEF_TABLE_ID"] = existing_brief
        log.info("[dry-run] 每日简报表 → %s", existing_brief)
    else:
        results["FEISHU_BRIEF_TABLE_ID"] = feishu.ensure_daily_brief_table(token)
        log.info("每日简报表 → %s", results["FEISHU_BRIEF_TABLE_ID"])

    if seed and not dry_run:
        _seed_param_sources(token, results["FEISHU_PARAM_TABLE_ID"])

    if dry_run:
        print("\n[dry-run] 预览结束，未做任何写入。去掉 --dry-run 即可真正创建。")
        return results

    print("\n" + "=" * 68)
    print("初始化完成。把下面几行复制进你的 .env（覆盖同名项）：")
    print("=" * 68)
    print(f"FEISHU_BASE_ID={config.FEISHU_BASE_ID}")
    for env_var in (
        "FEISHU_PARAM_TABLE_ID",
        "FEISHU_ENTRY_TABLE_ID",
        "FEISHU_BRIEF_TABLE_ID",
        "FEISHU_PAPER_CONFIG_TABLE_ID",
        "FEISHU_WECHAT_CONFIG_TABLE_ID",
        "FEISHU_VIDEO_CONFIG_TABLE_ID",
        "FEISHU_SOCIAL_CONFIG_TABLE_ID",
        "FEISHU_GITHUB_CONFIG_TABLE_ID",
    ):
        print(f"{env_var}={results[env_var]}")
    print("=" * 68)
    if not seed:
        print("提示：想写入几个示例 RSS 源以便第一次跑通，可加 --seed 重跑本命令。")
    print("下一步：填好 .env 里的 LLM_API_KEY / LLM_BASE_URL，然后运行 python -m src.main")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="在你自己的飞书 base 里初始化本项目所需的全部表")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="参数表为空时写入几个稳妥的示例 RSS 源",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只预览将创建/补齐哪些表和字段，不做任何写入",
    )
    args = parser.parse_args()
    run(seed=args.seed, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
