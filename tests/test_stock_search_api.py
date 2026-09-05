# -*- coding: utf-8 -*-
"""股票联想搜索（/api/v1/stocks/search + stock_index_loader.search_stock_index）回归测试。

打分规则必须与前端 apps/dsa-web/src/utils/searchStocks.ts 保持一致，
保证远程搜索与本地全量索引搜索的排序行为相同。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.v1.endpoints.stocks import search_stocks
from src.data import stock_index_loader


def _row(
    canonical: str,
    display: str,
    name: str,
    pinyin_full: str = "",
    pinyin_abbr: str = "",
    aliases: list | None = None,
    market: str = "CN",
    asset_type: str = "stock",
    active: bool = True,
    popularity: int = 100,
) -> list:
    return [
        canonical,
        display,
        name,
        pinyin_full,
        pinyin_abbr,
        aliases or [],
        market,
        asset_type,
        active,
        popularity,
    ]


_SAMPLE_ROWS = [
    _row("600519.SH", "600519", "贵州茅台", "guizhoumaotai", "gzmt", ["茅台", "股王"], popularity=999),
    _row("000001.SZ", "000001", "平安银行", "pinganyinhang", "payh", [], popularity=500),
    _row("00700.HK", "00700", "腾讯控股", "tengxunkonggu", "txkg", ["腾讯"], market="HK", popularity=800),
    _row("300750.SZ", "300750", "宁德时代", "ningdeshidai", "nds d".replace(" ", ""), [], popularity=300),
    _row("600000.SH", "600000", "浦发银行", "pufayinhang", "pfyh", [], active=False, popularity=100),
    _row("sh000001", "sh000001", "上证指数", "shangzhengzhishu", "szzs", ["上证综指"], asset_type="index", popularity=2000),
]


def _write_index(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_SAMPLE_ROWS, ensure_ascii=False), encoding="utf-8")


class SearchStockIndexTests(unittest.TestCase):
    def setUp(self):
        stock_index_loader._clear_stock_index_cache_for_tests()

    def tearDown(self):
        stock_index_loader._clear_stock_index_cache_for_tests()

    def _search(self, query: str, **kwargs):
        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "stocks.index.json"
            _write_index(index_path)
            with patch.object(
                stock_index_loader,
                "get_stock_index_candidate_paths",
                return_value=(index_path,),
            ):
                return stock_index_loader.search_stock_index(query, **kwargs)

    def test_exact_canonical_beats_everything(self):
        results = self._search("600519.SH")
        self.assertEqual(results[0]["canonical_code"], "600519.SH")
        self.assertEqual(results[0]["match_type"], "exact")
        self.assertEqual(results[0]["match_field"], "code")
        self.assertEqual(results[0]["score"], 100)

    def test_exact_display_code(self):
        results = self._search("000001")
        self.assertEqual(results[0]["canonical_code"], "000001.SZ")
        self.assertEqual(results[0]["score"], 99)

    def test_exact_chinese_name(self):
        results = self._search("贵州茅台")
        self.assertEqual(results[0]["score"], 98)
        self.assertEqual(results[0]["match_field"], "name")

    def test_exact_alias(self):
        results = self._search("股王")
        self.assertEqual(results[0]["score"], 97)
        self.assertEqual(results[0]["match_field"], "alias")

    def test_match_field_prefers_name_over_alias_like_frontend(self):
        # 前端 determineMatchField 先查 name 再查 alias："茅台" 包含于
        # "贵州茅台"，因此 match_field 是 name（对齐行为）。
        results = self._search("茅台")
        self.assertEqual(results[0]["score"], 97)
        self.assertEqual(results[0]["match_field"], "name")

    def test_exact_pinyin_abbr(self):
        results = self._search("txkg")
        self.assertEqual(results[0]["canonical_code"], "00700.HK")
        self.assertEqual(results[0]["score"], 96)

    def test_code_prefix_ranks_above_name_contains(self):
        results = self._search("600")
        scores = {(item["canonical_code"], item["score"]) for item in results}
        self.assertIn(("600519.SH", 80), scores)
        # inactive 行默认不出现（600000.SH 为 inactive 样本）
        self.assertNotIn(("600000.SH", 80), scores)

    def test_pinyin_prefix(self):
        results = self._search("gz")
        self.assertEqual(results[0]["canonical_code"], "600519.SH")
        self.assertEqual(results[0]["score"], 78)

    def test_contains_pinyin_full(self):
        results = self._search("maotai")
        self.assertEqual(results[0]["canonical_code"], "600519.SH")
        self.assertEqual(results[0]["score"], 58)

    def test_inactive_rows_excluded_by_default_and_included_on_demand(self):
        self.assertFalse(
            any(item["canonical_code"] == "600000.SH" for item in self._search("600000"))
        )
        included = self._search("600000", active_only=False)
        self.assertTrue(any(item["canonical_code"] == "600000.SH" for item in included))

    def test_index_asset_type_never_searchable(self):
        self.assertFalse(
            any(item["asset_type"] == "index" for item in self._search("上证"))
        )

    def test_sorting_is_score_then_popularity(self):
        results = self._search("600", active_only=False)
        codes = [item["canonical_code"] for item in results]
        # 同为 display 前缀 80 分，popularity 高的 600519 排在前
        self.assertEqual(codes[0], "600519.SH")
        self.assertIn("600000.SH", codes)

    def test_popular_mode_for_empty_query(self):
        results = self._search("")
        self.assertTrue(results, "empty query should return popular rows")
        self.assertTrue(all(item["match_type"] == "popular" for item in results))
        self.assertTrue(all(item["asset_type"] != "index" for item in results))
        popularities = [item["popularity"] for item in results]
        self.assertEqual(popularities, sorted(popularities, reverse=True))
        self.assertTrue(all(item["canonical_code"] != "600000.SH" for item in results))

    def test_limit_is_respected_and_clamped(self):
        self.assertEqual(len(self._search("", limit=2)), 2)
        self.assertLessEqual(len(self._search("600", limit=1)), 1)
        self.assertEqual(len(self._search("")), 4)


class StockSearchEndpointTests(unittest.TestCase):
    def setUp(self):
        stock_index_loader._clear_stock_index_cache_for_tests()

    def tearDown(self):
        stock_index_loader._clear_stock_index_cache_for_tests()

    def test_endpoint_returns_schema_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "stocks.index.json"
            _write_index(index_path)
            with patch.object(
                stock_index_loader,
                "get_stock_index_candidate_paths",
                return_value=(index_path,),
            ):
                response = search_stocks(q="茅台", limit=10, active_only=True)

        self.assertEqual(response.total, 1)
        item = response.items[0]
        self.assertEqual(item.canonical_code, "600519.SH")
        self.assertEqual(item.name_zh, "贵州茅台")
        self.assertEqual(item.match_type, "exact")
        self.assertEqual(item.match_field, "name")
        self.assertEqual(response.query, "茅台")

    def test_endpoint_popular_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "stocks.index.json"
            _write_index(index_path)
            with patch.object(
                stock_index_loader,
                "get_stock_index_candidate_paths",
                return_value=(index_path,),
            ):
                response = search_stocks(q="", limit=5, active_only=True)

        self.assertEqual(response.total, 4)
        self.assertEqual(response.items[0].canonical_code, "600519.SH")
        self.assertEqual(response.items[0].match_type, "popular")


if __name__ == "__main__":
    unittest.main()
