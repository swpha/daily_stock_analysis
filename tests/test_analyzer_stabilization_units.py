# -*- coding: utf-8 -*-
"""决策稳定化/占位填充工具的特征测试（拆分护栏）。

这些函数原位于 src/analyzer.py 模块级（决策稳定化工具块），测试统一从
src.analyzer 导入：无论函数在 analyzer 内联还是迁移到独立模块，兼容层
都必须保证这些导入与行为不变。
"""

import unittest

from src.analyzer import (
    AnalysisResult,
    _contains_trend_hint,
    _filter_conflicting_trend_items,
    _infer_trend_direction,
    _normalize_risk_warning_values,
    apply_placeholder_fill,
    fill_price_position_if_needed,
)
from src.report_language import get_placeholder_text


def _result(**overrides) -> AnalysisResult:
    defaults = dict(
        code="600519",
        name="贵州茅台",
        sentiment_score=70,
        trend_prediction="看多",
        operation_advice="",
        analysis_summary="",
        risk_warning="",
        dashboard=None,
    )
    defaults.update(overrides)
    return AnalysisResult(**defaults)


class ApplyPlaceholderFillTests(unittest.TestCase):
    def test_missing_sentiment_score_defaults_to_50(self):
        result = _result(sentiment_score=0)
        # 0 也被视为已提供——函数只按 missing_fields 指定的键填充
        apply_placeholder_fill(result, ["sentiment_score"])
        self.assertEqual(result.sentiment_score, 50)

    def test_blank_operation_advice_and_summary_get_placeholder(self):
        result = _result()
        apply_placeholder_fill(result, ["operation_advice", "analysis_summary"])
        placeholder = get_placeholder_text("zh")
        self.assertEqual(result.operation_advice, placeholder)
        self.assertEqual(result.analysis_summary, placeholder)

    def test_non_blank_text_is_not_overwritten(self):
        result = _result(operation_advice="持有观察")
        apply_placeholder_fill(result, ["operation_advice"])
        self.assertEqual(result.operation_advice, "持有观察")

    def test_core_conclusion_one_sentence_falls_back_to_summary_then_advice(self):
        result = _result(analysis_summary="综合摘要内容")
        result.dashboard = {"core_conclusion": {"one_sentence": "  "}}
        apply_placeholder_fill(result, ["dashboard.core_conclusion.one_sentence"])
        self.assertEqual(result.dashboard["core_conclusion"]["one_sentence"], "综合摘要内容")

        result2 = _result(operation_advice="持有观察")
        result2.dashboard = {"core_conclusion": {}}
        apply_placeholder_fill(result2, ["dashboard.core_conclusion.one_sentence"])
        self.assertEqual(result2.dashboard["core_conclusion"]["one_sentence"], "持有观察")

    def test_risk_alerts_normalized_from_risk_warning_text(self):
        result = _result(risk_warning="短期波动风险")
        result.dashboard = {"intelligence": {"risk_alerts": "not-a-list"}}
        apply_placeholder_fill(result, ["dashboard.intelligence.risk_alerts"])
        self.assertEqual(
            result.dashboard["intelligence"]["risk_alerts"],
            ["短期波动风险"],
        )

    def test_phase_decision_list_fields_get_empty_containers(self):
        result = _result()
        apply_placeholder_fill(
            result,
            [
                "dashboard.phase_decision.watch_conditions",
                "dashboard.phase_decision.data_limitations",
                "dashboard.phase_decision.phase_context",
            ],
        )
        phase = result.dashboard["phase_decision"]
        self.assertEqual(phase["watch_conditions"], [])
        self.assertEqual(phase["data_limitations"], [])
        self.assertEqual(phase["phase_context"], {})


class FillPricePositionTests(unittest.TestCase):
    def test_fills_missing_fields_from_trend_result(self):
        result = _result()
        result.dashboard = {"data_perspective": {"price_position": {}}}
        trend = {
            "ma5": 10.0,
            "ma10": 9.8,
            "ma20": 9.5,
            "bias_ma5": 2.0,
            "current_price": 10.2,
            "support_levels": [9.6, 9.1],
            "resistance_levels": [11.0, 11.8],
        }
        fill_price_position_if_needed(result, trend_result=trend, realtime_quote=None)
        pp = result.dashboard["data_perspective"]["price_position"]
        self.assertEqual(pp["ma5"], 10.0)
        self.assertEqual(pp["current_price"], 10.2)
        self.assertEqual(pp["support_level"], 9.6)
        self.assertEqual(pp["resistance_level"], 11.0)

    def test_realtime_price_fills_when_trend_has_none(self):
        result = _result()
        result.dashboard = {"data_perspective": {"price_position": {}}}
        fill_price_position_if_needed(
            result,
            trend_result={"ma5": 10.0},
            realtime_quote={"price": 10.9},
        )
        pp = result.dashboard["data_perspective"]["price_position"]
        self.assertEqual(pp["ma5"], 10.0)
        self.assertEqual(pp["current_price"], 10.9)

    def test_existing_real_values_are_not_overwritten(self):
        result = _result()
        result.dashboard = {
            "data_perspective": {"price_position": {"ma5": 8.8, "current_price": 9.0}}
        }
        fill_price_position_if_needed(
            result,
            trend_result={"ma5": 10.0, "current_price": 10.2},
        )
        pp = result.dashboard["data_perspective"]["price_position"]
        self.assertEqual(pp["ma5"], 8.8)
        self.assertEqual(pp["current_price"], 9.0)

    def test_none_result_is_noop(self):
        fill_price_position_if_needed(None, trend_result={"ma5": 1.0})


class TrendHintHelpersTests(unittest.TestCase):
    def test_contains_trend_hint_positive_and_negated(self):
        self.assertTrue(_contains_trend_hint("均线呈多头排列", ("多头排列",)))
        # 否定词未被句读打断、间隔在窗口内 → 视为被否定
        self.assertFalse(_contains_trend_hint("未形成多头排列", ("多头排列",)))

    def test_filter_conflicting_trend_items_drops_only_conflicts(self):
        items = ["均线多头排列，动能延续", "量能温和放大", "暂无明显趋势"]
        filtered = _filter_conflicting_trend_items(items, ("多头排列",))
        self.assertEqual(filtered, ["量能温和放大", "暂无明显趋势"])

    def test_infer_trend_direction_via_hints(self):
        self.assertEqual(
            _infer_trend_direction({"trend_status": "多头排列", "ma_alignment": ""}),
            "bullish",
        )
        self.assertEqual(
            _infer_trend_direction({"trend_status": "空头排列", "ma_alignment": ""}),
            "bearish",
        )
        self.assertEqual(
            _infer_trend_direction({"trend_status": "未形成多头排列", "ma_alignment": "无空头迹象"}),
            "neutral",
        )

    def test_normalize_risk_warning_values_flattens_nested(self):
        self.assertEqual(
            _normalize_risk_warning_values(["a", ("b", None), {"k": "c"}]),
            ["a", "b", '{"k": "c"}'],
        )


if __name__ == "__main__":
    unittest.main()
