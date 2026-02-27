#!/usr/bin/env python3
"""
测试 IBKR 成交解析与接口输出的一致性：
- parse_trades_from_csv：解析负数量卖出成交以及 realized_pnl 字段。
"""
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.services.ibkr_flex_service import parse_trades_from_csv


MINIMAL_TRADES_CSV = """ClientAccountID,AccountAlias,Model,CurrencyPrimary,AssetClass,Symbol,Description,ReportDate,TradeDate,Quantity,TradePrice,TradeMoney,Buy/Sell,RealizedPNL,LevelOfDetail,ListingExchange
U123,,,USD,STK,AAPL,APPLE INC,20260224,20260218,1,263.85,263.85,BUY,,EXECUTION,NASDAQ
U123,,,USD,STK,AAPL,APPLE INC,20260224,20260224,-1,270.46,-270.46,SELL,6.61,EXECUTION,NASDAQ
"""


def test_parse_trades_from_csv_with_realized_pnl_and_negative_quantity():
    """卖出行数量为负时，仍能解析 side / quantity / realized_pnl。"""
    trades = parse_trades_from_csv(MINIMAL_TRADES_CSV)

    assert len(trades) == 2

    buy, sell = trades

    assert buy["symbol"] == "AAPL"
    assert buy["side"] == "BUY"
    assert buy["quantity"] == 1
    assert buy["price"] == 263.85
    assert buy["amount"] == 263.85
    assert buy.get("realized_pnl") is None

    assert sell["symbol"] == "AAPL"
    assert sell["side"] == "SELL"
    # Flex 报表中的卖出数量通常为负数，这里应保持原始符号
    assert sell["quantity"] == -1
    assert sell["price"] == 270.46
    assert sell["amount"] == -270.46
    assert sell["realized_pnl"] == 6.61


def test_parse_trades_filters_out_orders_and_non_executions():
    """只保留 LevelOfDetail 为 EXECUTION 的已成交行，过滤掉 Orders、汇总等。"""
    csv_with_orders = """ClientAccountID,AccountAlias,Model,CurrencyPrimary,AssetClass,Symbol,Description,ReportDate,TradeDate,Quantity,TradePrice,TradeMoney,Buy/Sell,RealizedPNL,LevelOfDetail,ListingExchange
U123,,,USD,STK,AAPL,APPLE INC,20260224,20260224,1,263.00,263.00,BUY,,ORDERS,NASDAQ
U123,,,USD,STK,AAPL,APPLE INC,20260224,20260218,1,263.85,263.85,BUY,,EXECUTION,NASDAQ
U123,,,USD,STK,AAPL,APPLE INC,20260224,20260224,-1,270.46,-270.46,SELL,6.61,EXECUTION,NASDAQ
U123,,,USD,STK,AAPL,APPLE INC,20260224,20260224,0,270.00,0,SELL,,SYMBOL_SUMMARY,NASDAQ
"""
    trades = parse_trades_from_csv(csv_with_orders)
    # 只应保留 2 条 EXECUTION，ORDERS 和 SYMBOL_SUMMARY 被过滤；0 数量也会被跳过
    assert len(trades) == 2
    assert all(t.get("symbol") == "AAPL" for t in trades)
    assert trades[0]["side"] == "BUY" and trades[0]["quantity"] == 1
    assert trades[1]["side"] == "SELL" and trades[1]["quantity"] == -1


def test_parse_trades_filters_out_unfilled_by_trade_money():
    """TradeMoney 为 0 或空的行（未成交）不保留，只保留真实成交。"""
    csv_with_unfilled = """ClientAccountID,AccountAlias,Model,CurrencyPrimary,AssetClass,Symbol,Description,ReportDate,TradeDate,Quantity,TradePrice,TradeMoney,Buy/Sell,RealizedPNL,LevelOfDetail,ListingExchange
U123,,,USD,STK,AAPL,APPLE INC,20260224,20260218,1,263.85,263.85,BUY,,EXECUTION,NASDAQ
U123,,,USD,STK,AAPL,APPLE INC,20260224,20260224,-1,270.46,-270.46,SELL,6.61,EXECUTION,NASDAQ
U123,,,USD,STK,AAPL,APPLE INC,20260224,20260224,-1,264.85,0,SELL,,EXECUTION,NASDAQ
"""
    trades = parse_trades_from_csv(csv_with_unfilled)
    # 只应保留 2 条（买入 + 270.46 卖出）；264.85 卖出 TradeMoney=0 视为未成交，过滤
    assert len(trades) == 2
    assert trades[0]["side"] == "BUY" and trades[0]["price"] == 263.85
    assert trades[1]["side"] == "SELL" and trades[1]["price"] == 270.46
