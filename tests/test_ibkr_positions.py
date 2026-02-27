#!/usr/bin/env python3
"""
测试 IBKR 持仓解析逻辑。
- parse_positions_from_csv：使用合成 CSV 校验解析结果（SUMMARY 行、LOT 忽略、空输入）。
"""
import os

# 项目根目录
import sys
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.services.ibkr_flex_service import parse_positions_from_csv


# 最小持仓表头与一行 SUMMARY 数据（与 ibkr_flex_statement 持仓段结构一致）
MINIMAL_POSITIONS_CSV = """ClientAccountID,AccountAlias,Model,CurrencyPrimary,FXRateToBase,AssetClass,SubCategory,Symbol,Description,Conid,SecurityID,SecurityIDType,ListingExchange,ReportDate,Quantity,MarkPrice,PositionValue,OpenPrice,CostBasisPrice,CostBasisMoney,PercentOfNAV,FifoPnlUnrealized,Side,LevelOfDetail
U123,,,USD,1,STK,COMMON,AAPL,APPLE INC,265598,US0378331005,ISIN,NASDAQ,20260224,10,270.5,2705,268,268,2680,,25,Long,SUMMARY
"""


def test_parse_positions_from_csv_minimal():
    """解析仅含一条 SUMMARY 持仓的 CSV，校验 as_of_date、base_currency、positions、summary。"""
    snapshot = parse_positions_from_csv(MINIMAL_POSITIONS_CSV)

    assert snapshot["as_of_date"] == "20260224"
    assert snapshot["base_currency"] == "USD"
    assert isinstance(snapshot["positions"], list)
    assert len(snapshot["positions"]) == 1

    pos = snapshot["positions"][0]
    assert pos["symbol"] == "AAPL"
    assert pos["description"] == "APPLE INC"
    assert pos["quantity"] == 10
    assert pos["mark_price"] == 270.5
    assert pos["position_value"] == 2705
    assert pos["avg_cost"] == 268.0
    assert pos["unrealized_pnl"] == 25
    assert pos["currency_primary"] == "USD"

    summary = snapshot["summary"]
    assert summary["total_position_value"] == 2705
    assert summary["total_unrealized_pnl"] == 25
    assert summary["position_count"] == 1


def test_parse_positions_from_csv_skips_lot_rows():
    """LOT 明细行被忽略，只保留 SUMMARY。"""
    csv_with_lot = MINIMAL_POSITIONS_CSV.rstrip() + """
U123,,,USD,1,STK,COMMON,AAPL,APPLE INC,265598,US0378331005,ISIN,NASDAQ,20260224,5,270.5,1352.5,268,268,1340,,12.5,Long,LOT
"""
    snapshot = parse_positions_from_csv(csv_with_lot)
    # 仍只有一条汇总
    assert len(snapshot["positions"]) == 1
    assert snapshot["positions"][0]["quantity"] == 10


def test_parse_positions_from_csv_empty():
    """无持仓表头时返回空列表与空汇总。"""
    snapshot = parse_positions_from_csv("OtherSection,Col1,Col2\n1,2,3\n")
    assert snapshot["positions"] == []
    assert snapshot.get("as_of_date") is None
    assert snapshot.get("base_currency") is None
    assert snapshot["summary"]["position_count"] == 0
    assert snapshot["summary"]["total_position_value"] == 0
    assert snapshot["summary"]["total_unrealized_pnl"] == 0
