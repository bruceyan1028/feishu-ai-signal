import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import dashboard, publish


# 腾讯行情真实响应的截断样本：只保留到总市值那一位，够覆盖全部取值下标。
def _quote_line(code: str, name: str, price: str, prev_close: str, pct: str, cap: str) -> str:
    parts = [""] * 46
    parts[0] = "1"
    parts[dashboard._Q_NAME] = name
    parts[2] = code
    parts[dashboard._Q_PRICE] = price
    parts[dashboard._Q_PREV_CLOSE] = prev_close
    parts[dashboard._Q_OPEN] = prev_close
    parts[dashboard._Q_STAMP] = "2026-08-27 15:00:00"
    parts[dashboard._Q_CHANGE_PCT] = pct
    parts[dashboard._Q_HIGH] = price
    parts[dashboard._Q_LOW] = prev_close
    parts[dashboard._Q_MARKET_CAP] = cap
    return f'v_{code}="{"~".join(parts)}";'


class ParseQuotesTest(unittest.TestCase):
    def test_parses_price_and_derives_change(self):
        body = _quote_line("sh688256", "寒武纪", "1048.00", "1020.00", "2.75", "6590.39")
        rows = dashboard.parse_quotes(
            body, {"sh688256": "A股"}, {"sh688256": "cambricon.com"}
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "寒武纪")
        self.assertEqual(row["url"], "https://www.sse.com.cn/assortment/stock/list/info/company/index.shtml?COMPANY_CODE=688256")
        self.assertEqual(row["market"], "A股")
        self.assertEqual(row["currency"], "CNY")
        self.assertEqual(row["logoDomain"], "cambricon.com")
        self.assertEqual(row["price"], 1048.0)
        self.assertEqual(row["change"], 28.0)
        self.assertEqual(row["changePct"], 2.75)
        self.assertEqual(row["marketCap"], 6590.4)

    def test_quote_names_drop_legal_suffixes_for_the_narrow_rail(self):
        self.assertEqual(dashboard.display_quote_name("Meta Platforms"), "Meta")
        self.assertEqual(dashboard.display_quote_name("Snowflake Inc."), "Snowflake")
        self.assertEqual(dashboard.display_quote_name("摩尔线程-U"), "摩尔线程")
        self.assertEqual(dashboard.display_quote_name("沐曦股份-U"), "沐曦股份")
        self.assertEqual(dashboard.display_quote_name("MINIMAX-W"), "MiniMax")
        self.assertEqual(dashboard.display_quote_name("宇树科技-W"), "宇树科技")
        self.assertEqual(
            dashboard.quote_url("usMETA", "meta.com"),
            "https://www.nasdaq.com/market-activity/stocks/meta",
        )
        self.assertEqual(
            dashboard.quote_url("sh688795", ""),
            "https://www.sse.com.cn/assortment/stock/list/info/company/index.shtml?COMPANY_CODE=688795",
        )
        self.assertEqual(
            dashboard.quote_url("sz300308"),
            "https://www.szse.cn/certificate/individual/index.html?code=300308",
        )
        self.assertEqual(
            dashboard.quote_url("hk02513"),
            "https://www.hkex.com.hk/Market-Data/Securities-Prices/Equities/Equities-Quote?sc_lang=zh-HK&sym=2513",
        )

    def test_currency_follows_market_prefix(self):
        body = (
            _quote_line("usNVDA", "英伟达", "209.66", "213.05", "-1.59", "50781.7")
            + _quote_line("hk00700", "腾讯控股", "447.800", "445.400", "0.54", "40763.89")
        )
        rows = dashboard.parse_quotes(body, {"usNVDA": "美股", "hk00700": "港股"})
        self.assertEqual([r["currency"] for r in rows], ["USD", "HKD"])

    def test_change_pct_computed_when_field_blank(self):
        """接口在个别市场会把涨跌幅那一位留空，此时用现价和昨收现算，不能显示 0。"""
        body = _quote_line("usAMD", "超威半导体", "480.93", "479.18", "", "7851.06")
        row = dashboard.parse_quotes(body, {"usAMD": "美股"})[0]
        self.assertAlmostEqual(row["changePct"], 0.37, places=2)

    def test_skips_truncated_and_priceless_rows(self):
        broken = 'v_shbad="1~缺字段~shbad~1.00";'
        halted = _quote_line("sh600000", "停牌股", "0.00", "10.00", "0.00", "100")
        rows = dashboard.parse_quotes(broken + halted, {})
        self.assertEqual(rows, [])


class FetchMarketTest(unittest.TestCase):
    @patch("src.dashboard.requests.get")
    def test_orders_quotes_by_configured_tickers(self, mock_get: MagicMock):
        mock_get.return_value = MagicMock(
            status_code=200,
            text=(
                _quote_line("hk00700", "腾讯控股", "447.8", "445.4", "0.54", "40763")
                + _quote_line("usNVDA", "英伟达", "209.66", "213.05", "-1.59", "50781")
            ),
        )
        board = dashboard.fetch_market(
            (("usNVDA", "美股", "nvidia.com"), ("hk00700", "港股", "tencent.com"))
        )
        self.assertEqual(board["error"], "")
        self.assertEqual([q["code"] for q in board["quotes"]], ["usNVDA", "hk00700"])
        self.assertEqual(board["quotes"][0]["logoDomain"], "nvidia.com")

    @patch("src.dashboard.requests.get", side_effect=RuntimeError("boom"))
    def test_network_failure_degrades_to_error_field(self, _mock_get: MagicMock):
        board = dashboard.fetch_market((("usNVDA", "美股", "nvidia.com"),))
        self.assertEqual(board["quotes"], [])
        self.assertIn("boom", board["error"])

    def test_ticker_codes_are_unique(self):
        """代码在内部按 dict 归集，重复会让某只股票被静默顶掉。"""
        codes = [code for code, _, _ in dashboard.TICKERS]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(isinstance(domain, str) for _, _, domain in dashboard.TICKERS))

    def test_tickers_are_ai_supply_chain_not_internet_platforms(self):
        codes = {code for code, _, _ in dashboard.TICKERS}
        self.assertNotIn("hk00700", codes)
        self.assertNotIn("hk09988", codes)
        self.assertNotIn("hk03690", codes)
        self.assertNotIn("usBABA", codes)
        self.assertNotIn("usBIDU", codes)
        us = [code for code, market, _ in dashboard.TICKERS if market == "美股"]
        cn = [code for code, market, _ in dashboard.TICKERS if market != "美股"]
        self.assertGreaterEqual(len(us), 8)
        self.assertGreaterEqual(len(cn), 8)
        self.assertGreater(len(dashboard.TICKERS), 12)
        self.assertIn("usNVDA", codes)
        self.assertIn("usSPCX", codes)
        self.assertNotIn("usSMCI", codes)
        self.assertIn("sh688256", codes)
        self.assertIn("sh688836", codes)
        self.assertIn("hk02513", codes)


class FetchLeaderboardTest(unittest.TestCase):
    @staticmethod
    def _model(name, index, **extra):
        payload = {
            "id": name,
            "name": name,
            "model_creator": {"name": "Lab"},
            "evaluations": {"artificial_analysis_intelligence_index": index},
            "pricing": {"price_1m_blended_3_to_1": 1.5},
            "median_output_tokens_per_second": 100.0,
            "median_time_to_first_token_seconds": 0.5,
        }
        payload.update(extra)
        return payload

    @patch.dict("os.environ", {"ARTIFICIAL_ANALYSIS_API_KEY": "k"}, clear=False)
    @patch("src.dashboard.requests.get")
    def test_sorts_by_intelligence_and_drops_unscored(self, mock_get: MagicMock):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "data": [
                    self._model("mid", 60),
                    self._model("top", 70),
                    self._model("unscored", None, evaluations={}),
                ]
            },
        )
        board = dashboard.fetch_leaderboard(limit=5)
        self.assertEqual([m["name"] for m in board["models"]], ["top", "mid"])
        self.assertEqual([m["rank"] for m in board["models"]], [1, 2])
        self.assertEqual(board["error"], "")

    @patch.dict("os.environ", {"ARTIFICIAL_ANALYSIS_API_KEY": "k"}, clear=False)
    @patch("src.dashboard.requests.get")
    def test_keeps_only_best_variant_per_model(self, mock_get: MagicMock):
        """同一模型的推理档位在接口里并列，不去重会被一家的四个档位占满整个榜。"""
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "data": [
                    self._model("Claude Opus 5 (Adaptive Reasoning, Max)", 63.1),
                    self._model("Claude Opus 5 (Adaptive Reasoning, High)", 61.5),
                    self._model("Claude Opus 5 (medium)", 58.6),
                    self._model("GPT-5.6 Sol (max)", 60.9),
                ]
            },
        )
        board = dashboard.fetch_leaderboard(limit=5)
        self.assertEqual(
            [m["name"] for m in board["models"]], ["Claude Opus 5", "GPT-5.6 Sol"]
        )
        self.assertEqual(board["models"][0]["intelligence"], 63.1)
        # 原始条目名留在 variant 里，便于回溯这一分来自哪一档
        self.assertEqual(
            board["models"][0]["variant"], "Claude Opus 5 (Adaptive Reasoning, Max)"
        )

    def test_base_model_name_strips_only_trailing_variant(self):
        self.assertEqual(dashboard._base_model_name("Grok 4.6 (high)"), "Grok 4.6")
        self.assertEqual(dashboard._base_model_name("GLM-5.3"), "GLM-5.3")
        # 名字里本来就带括号的不能被吃掉整段
        self.assertEqual(
            dashboard._base_model_name("Qwen3.8 (Preview) 2.4T"), "Qwen3.8 (Preview) 2.4T"
        )

    def test_creator_domain_matches_known_labs_and_degrades(self):
        # 用接口真实返回的厂商名，不是想当然的写法：Grok 挂在 SpaceXAI 下，智谱叫 Z AI
        self.assertEqual(dashboard._creator_domain("OpenAI"), "openai.com")
        self.assertEqual(dashboard._creator_domain("SpaceXAI"), "x.ai")
        self.assertEqual(dashboard._creator_domain("Z AI"), "z.ai")
        self.assertEqual(dashboard._creator_domain("Kimi"), "moonshot.cn")
        self.assertEqual(dashboard._creator_domain("ByteDance Seed"), "bytedance.com")
        self.assertEqual(dashboard._creator_domain("Alibaba"), "qwen.ai")
        # 认不出的厂商不该硬凑域名：前端拿到空串会回退字标
        self.assertEqual(dashboard._creator_domain("某个新实验室"), "")

    @patch.dict("os.environ", {"ARTIFICIAL_ANALYSIS_API_KEY": "k"}, clear=False)
    @patch("src.dashboard.requests.get")
    def test_limit_caps_rows(self, mock_get: MagicMock):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [self._model(f"m{i}", i) for i in range(20)]},
        )
        self.assertEqual(len(dashboard.fetch_leaderboard(limit=3)["models"]), 3)

    @patch.dict("os.environ", {"ARTIFICIAL_ANALYSIS_API_KEY": ""}, clear=False)
    def test_missing_key_is_reported_not_raised(self):
        board = dashboard.fetch_leaderboard()
        self.assertEqual(board["models"], [])
        self.assertIn("ARTIFICIAL_ANALYSIS_API_KEY", board["error"])

    @patch.dict("os.environ", {"ARTIFICIAL_ANALYSIS_API_KEY": "k"}, clear=False)
    @patch("src.dashboard.requests.get", side_effect=RuntimeError("429"))
    def test_api_failure_degrades_to_error_field(self, _mock_get: MagicMock):
        board = dashboard.fetch_leaderboard()
        self.assertEqual(board["models"], [])
        self.assertIn("429", board["error"])


class WritePayloadTest(unittest.TestCase):
    def test_writes_utf8_json(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "data" / "dashboard-latest.json"
            dashboard.write_payload({"market": {"quotes": [{"name": "寒武纪"}]}}, output)
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["market"]["quotes"][0]["name"], "寒武纪")

    def test_keep_last_good_retains_old_board_when_new_fetch_fails(self):
        previous = {
            "leaderboard": {"error": "", "models": [{"name": "Claude Opus 5"}]},
            "market": {"error": "", "quotes": [{"name": "寒武纪"}]},
        }
        incoming = {
            "leaderboard": {"error": "429", "models": []},
            "market": {"error": "", "quotes": [{"name": "英伟达"}]},
        }
        merged = dashboard.keep_last_good(incoming, previous)
        self.assertEqual(merged["leaderboard"]["models"][0]["name"], "Claude Opus 5")
        self.assertEqual(merged["market"]["quotes"][0]["name"], "英伟达")

    @patch.dict("os.environ", {"ARTIFICIAL_ANALYSIS_API_KEY": "k"}, clear=False)
    @patch("src.dashboard.fetch_market")
    @patch("src.dashboard.fetch_leaderboard")
    def test_refresh_into_overwrites_stale_snapshot(
        self, mock_board: MagicMock, mock_market: MagicMock
    ):
        mock_board.return_value = {"error": "", "models": [{"name": "GPT-5.6 Sol"}]}
        mock_market.return_value = {"error": "", "quotes": [{"name": "英伟达"}]}
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "dashboard-latest.json"
            output.write_text(
                json.dumps(
                    {
                        "leaderboard": {
                            "updatedAt": "2026-08-28T12:15:30+08:00",
                            "models": [{"name": "旧模型"}],
                        }
                    }
                ),
                encoding="utf-8",
            )
            dashboard.refresh_into(output)
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["leaderboard"]["models"][0]["name"], "GPT-5.6 Sol")


class PublishIntegrationTest(unittest.TestCase):
    def test_daily_workflow_does_not_fetch_dashboard_twice(self):
        text = Path(".github/workflows/daily-brief.yml").read_text(encoding="utf-8")
        self.assertIn("python -m src.publish", text)
        self.assertNotIn("python -m src.dashboard", text)

    @patch("src.dashboard.refresh_into")
    def test_publish_refreshes_dashboard_after_rebuild(self, mock_refresh: MagicMock):
        mock_refresh.return_value = Path("site/data/dashboard-latest.json")
        with tempfile.TemporaryDirectory() as temp:
            site = Path(temp)
            (site / "data").mkdir()
            publish.refresh_side_boards(site)
            mock_refresh.assert_called_once_with(site / "data" / "dashboard-latest.json")

    def test_dashboard_survives_site_rebuild(self):
        """日报重建会清空 site/，旧看板必须先暂存；publish 随后会重拉，失败则沿用这份。"""
        self.assertIn("dashboard-latest.json", publish._PERSISTENT_DATA_GLOBS)
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            data.mkdir()
            (data / "dashboard-latest.json").write_text("{}", encoding="utf-8")
            kept = publish.stash_persistent_site_data(Path(temp))
            self.assertIn("dashboard-latest.json", kept)


class FrontendContractTest(unittest.TestCase):
    def test_index_renders_both_boards_from_static_json(self):
        template = Path("index.html").read_text(encoding="utf-8")
        self.assertIn("data/dashboard-latest.json", template)
        self.assertIn("function dataRailHtml", template)
        self.assertIn("Artificial Analysis", template)
        self.assertIn("智能指数", template)
        self.assertIn("AI 概念股", template)
        self.assertNotIn("模型竞技场", template)
        self.assertNotIn("MODEL ARENA", template)
        self.assertNotIn("AI EQUITIES", template)
        # 条款要求署名；标题本身链回官网，不再单独做底栏
        self.assertIn("artificialanalysis.ai", template)
        self.assertNotIn("腾讯财经", template)
        self.assertIn("qt.gtimg.cn/q=", template)
        self.assertIn("function fetchLiveMarket", template)
        self.assertIn("function refreshLiveMarket", template)
        self.assertIn("const MARKET_TICKERS", template)

    def test_live_quote_tickers_match_the_pipeline(self):
        template = Path("index.html").read_text(encoding="utf-8")
        block = template.split("const MARKET_TICKERS = [")[1].split("];")[0]
        codes = re.findall(r"\['([^']+)'", block)
        self.assertEqual(codes, [code for code, _, _ in dashboard.TICKERS])

    def test_model_board_logos_use_official_colors(self):
        logos = Path("site/data/logos")
        openai = (logos / "openai.svg").read_text(encoding="utf-8")
        anthropic = (logos / "anthropic.svg").read_text(encoding="utf-8")
        qwen = (logos / "qwen.svg").read_text(encoding="utf-8")
        zai = (logos / "zai.svg").read_text(encoding="utf-8")
        self.assertIn('fill="#000000"', openai)
        self.assertNotIn("#10A37F", openai)
        self.assertIn('fill="#000000"', anthropic)
        self.assertNotIn("#D4A27F", anthropic)
        self.assertIn("#615CED", qwen)
        self.assertIn("#191919", zai)
        template = Path("index.html").read_text(encoding="utf-8")
        self.assertIn("data/logos/qwen.svg", template)
        self.assertNotIn("alibabadotcom.svg", template)

    def test_masthead_drops_title_and_intro(self):
        template = Path("index.html").read_text(encoding="utf-8")
        self.assertNotIn("hero-title", template)
        self.assertNotIn("hero-intro", template)


if __name__ == "__main__":
    unittest.main()
