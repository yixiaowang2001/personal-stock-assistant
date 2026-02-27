"""
IBKR Flex Web Service 集成与持仓解析服务

- 从环境变量读取 IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID
- 调用 FlexStatementService.SendRequest / GetStatement 获取报表（CSV）
- 解析持仓部分，提取 SUMMARY 级别的持仓快照
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


def _get_http_proxies() -> Optional[Dict[str, str]]:
    """
    从 .env 配置或进程环境变量读取 HTTP(S) 代理，与本地可跑通的 ibkr_flex_test.py 行为一致。
    优先用 app 的 settings（.env），没有则用 os.environ，以便与直接运行脚本时的环境一致。
    """
    proxies: Dict[str, str] = {}
    try:
        from app.core.config import settings
        if (settings.HTTPS_PROXY or "").strip():
            proxies["https"] = (settings.HTTPS_PROXY or "").strip()
        if (settings.HTTP_PROXY or "").strip():
            proxies["http"] = (settings.HTTP_PROXY or "").strip()
    except Exception:
        pass
    if not proxies:
        # 与 ibkr_flex_test.py 一致：使用进程环境变量（脚本能跑通时多为 shell 里 export 的代理）
        https = (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or "").strip()
        http = (os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or "").strip()
        if https:
            proxies["https"] = https
        if http:
            proxies["http"] = http
    return proxies if proxies else None


FLEX_VERSION = "3"
USER_AGENT = "Java"
MAX_POLL_ATTEMPTS = 5
POLL_INTERVAL_SECONDS = 2
BASE_URL = "https://www.interactivebrokers.com/Universal/servlet"


def _extract_xml_tag(text: str, tag_name: str) -> Optional[str]:
    """
    从 XML 文本中抽取指定标签的内容（简单正则，不做严格 XML 校验）。
    """
    pattern = rf"<{tag_name}>(.*?)</{tag_name}>"
    m = re.search(pattern, text, flags=re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else None


def _get_env_credentials() -> Tuple[str, str]:
    """
    从环境变量获取 IBKR Flex 凭据。

    Returns:
        (flex_token, flex_query_id)
    """
    token = os.getenv("IBKR_FLEX_TOKEN", "").strip()
    query_id = os.getenv("IBKR_FLEX_QUERY_ID", "").strip()

    if not token or token.lower().startswith("your_"):
        raise RuntimeError("IBKR_FLEX_TOKEN 未配置或仍为占位符，请在 .env 中配置后重启后端。")
    if not query_id or query_id.lower().startswith("your_"):
        raise RuntimeError("IBKR_FLEX_QUERY_ID 未配置或仍为占位符，请在 .env 中配置后重启后端。")

    return token, query_id


def send_flex_request(token: Optional[str] = None, query_id: Optional[str] = None) -> str:
    """
    调用 FlexStatementService.SendRequest，返回 ReferenceCode。

    Args:
        token: 可选，Flex Web Service Token；为空则从环境变量读取
        query_id: 可选，Flex Query ID；为空则从环境变量读取
    """
    if token is None or query_id is None:
        token, query_id = _get_env_credentials()

    url = f"{BASE_URL}/FlexStatementService.SendRequest"
    params = {
        "t": token,
        "q": query_id,
        "v": FLEX_VERSION,
    }
    headers = {
        "User-Agent": USER_AGENT,
    }
    # 与 ibkr_flex_test.py 一致：有代理时显式传入，无代理时不传（requests 会用进程环境变量）
    proxies = _get_http_proxies()
    if proxies:
        logger.info("使用代理访问 IBKR Flex: %s", list(proxies.keys()))
    logger.info("调用 IBKR Flex SendRequest 获取 ReferenceCode...")
    if proxies:
        resp = requests.get(url, params=params, headers=headers, timeout=30, proxies=proxies)
    else:
        # 不传 proxies 参数，让 requests 按默认行为使用进程环境变量（与 ibkr_flex_test.py 一致）
        resp = requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()

    text = resp.text
    status = _extract_xml_tag(text, "Status")
    if not status or status.lower() != "success":
        error_code = _extract_xml_tag(text, "ErrorCode") or ""
        error_msg = _extract_xml_tag(text, "ErrorMessage") or ""
        raise RuntimeError(
            f"Flex SendRequest 调用失败，Status={status!r}, "
            f"ErrorCode={error_code!r}, ErrorMessage={error_msg!r}"
        )

    ref_code = _extract_xml_tag(text, "ReferenceCode")
    if not ref_code:
        raise RuntimeError("未在 SendRequest 响应中找到 ReferenceCode。")

    logger.info("IBKR Flex SendRequest 成功，ReferenceCode=%s", ref_code)
    return ref_code


def get_flex_statement(reference_code: str, token: Optional[str] = None) -> str:
    """
    轮询 FlexStatementService.GetStatement，直到报表生成完成或超时。

    返回最终的报表内容（XML/CSV 文本）。
    """
    if token is None:
        token, _ = _get_env_credentials()

    url = f"{BASE_URL}/FlexStatementService.GetStatement"
    headers = {
        "User-Agent": USER_AGENT,
    }
    proxies = _get_http_proxies()

    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        params = {
            "t": token,
            "q": reference_code,
            "v": FLEX_VERSION,
        }
        if proxies:
            resp = requests.get(url, params=params, headers=headers, timeout=60, proxies=proxies)
        else:
            resp = requests.get(url, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
        text = resp.text

        if "Statement generation in progress" in text:
            logger.info(
                "IBKR 报表仍在生成中 (attempt=%s/%s)...",
                attempt,
                MAX_POLL_ATTEMPTS,
            )
            if attempt == MAX_POLL_ATTEMPTS:
                raise TimeoutError("报表仍在生成中，已达到最大轮询次数。")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        logger.info("IBKR 报表获取成功。")
        return text

    raise TimeoutError("在限定次数内未能获取到报表。")


@dataclass
class IbkrPosition:
    symbol: str
    description: str
    asset_class: str
    currency_primary: str
    quantity: float
    mark_price: float
    position_value: float
    avg_cost: Optional[float]
    unrealized_pnl: Optional[float]
    report_date: Optional[str]
    side: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "description": self.description,
            "asset_class": self.asset_class,
            "currency_primary": self.currency_primary,
            "quantity": self.quantity,
            "mark_price": self.mark_price,
            "position_value": self.position_value,
            "avg_cost": self.avg_cost,
            "unrealized_pnl": self.unrealized_pnl,
            "report_date": self.report_date,
            "side": self.side,
        }


def _safe_float(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def parse_positions_from_csv(csv_text: str) -> Dict[str, Any]:
    """
    从 IBKR Flex CSV 报表中解析持仓信息。

    解析逻辑：
    - 定位包含 Quantity / MarkPrice / PositionValue / FifoPnlUnrealized 的表头行
    - 使用该表头解析后续记录，直到遇到下一个以 ClientAccountID 开头的表头
    - 仅保留 LevelOfDetail == SUMMARY 的持仓行（忽略 LOT 明细）
    """
    lines = csv_text.splitlines()
    reader = csv.reader(lines)

    header_row: Optional[List[str]] = None
    header_index: Dict[str, int] = {}
    in_positions_section = False
    positions: List[IbkrPosition] = []
    base_currency: Optional[str] = None
    report_date: Optional[str] = None

    for row in reader:
        if not row:
            continue

        # 尚未进入持仓部分：寻找表头（持仓段）
        if not in_positions_section:
            if (
                row[0] == "ClientAccountID"
                and "Quantity" in row
                and "MarkPrice" in row
                and "PositionValue" in row
            ):
                header_row = row
                header_index = {name: idx for idx, name in enumerate(header_row)}
                in_positions_section = True
            continue

        # 已在持仓部分：遇到新的表头则结束
        if row[0] == "ClientAccountID":
            break

        # 保护性检查
        if not header_index:
            continue

        def col(name: str) -> Optional[str]:
            idx = header_index.get(name)
            if idx is None or idx >= len(row):
                return None
            val = row[idx]
            return val if val != "" else None

        level = col("LevelOfDetail")
        if level and level != "SUMMARY":
            # 忽略 LOT 等明细行，只保留汇总
            continue

        quantity = _safe_float(col("Quantity")) or 0.0
        if quantity == 0:
            # 零仓位跳过
            continue

        mark_price = _safe_float(col("MarkPrice")) or 0.0
        position_value = _safe_float(col("PositionValue")) or 0.0
        cost_basis_money = _safe_float(col("CostBasisMoney"))
        fifo_unrealized = _safe_float(col("FifoPnlUnrealized"))

        if base_currency is None:
            base_currency = col("CurrencyPrimary")
        if report_date is None:
            report_date = col("ReportDate")

        avg_cost: Optional[float] = None
        if cost_basis_money is not None and quantity:
            try:
                avg_cost = cost_basis_money / quantity
            except Exception:
                avg_cost = None

        pos = IbkrPosition(
            symbol=col("Symbol") or "",
            description=col("Description") or "",
            asset_class=col("AssetClass") or "",
            currency_primary=col("CurrencyPrimary") or "",
            quantity=quantity,
            mark_price=mark_price,
            position_value=position_value,
            avg_cost=avg_cost,
            unrealized_pnl=fifo_unrealized,
            report_date=report_date,
            side=col("Side"),
        )
        positions.append(pos)

    # 汇总信息
    total_value = sum(p.position_value for p in positions)
    total_unrealized = sum(p.unrealized_pnl or 0.0 for p in positions)

    snapshot: Dict[str, Any] = {
        "as_of_date": report_date,
        "base_currency": base_currency,
        "positions": [p.to_dict() for p in positions],
        "summary": {
            "total_position_value": total_value,
            "total_unrealized_pnl": total_unrealized,
            "position_count": len(positions),
        },
    }

    # 尝试解析 Cash Report 段，提取期末现金（Ending Cash / Ending Settled Cash）
    # 结构参考 IBKR Cash Report 文档：https://www.ibkrguides.com/reportingreference/reportguide/cash%20reportfq.htm
    try:
        cash_total: Optional[float] = None
        cash_settled: Optional[float] = None

        reader2 = csv.reader(lines)
        cash_header_row: Optional[List[str]] = None
        cash_header_index: Dict[str, int] = {}
        in_cash_section = False

        for row in reader2:
            if not row:
                continue

            # 定位 Cash Report 表头：同时包含 CurrencyPrimary / EndingCash / EndingSettledCash
            if not in_cash_section:
                if (
                    "CurrencyPrimary" in row
                    and "EndingCash" in row
                    and "EndingSettledCash" in row
                ):
                    cash_header_row = row
                    cash_header_index = {name: idx for idx, name in enumerate(cash_header_row)}
                    in_cash_section = True
                continue

            # 进入 Cash Report 段之后，遇到新表头或空行则结束
            if row[0] in ("ClientAccountID", "ClientAccountID\""):
                break

            if not cash_header_index:
                continue

            def cash_col(name: str) -> Optional[str]:
                idx = cash_header_index.get(name)
                if idx is None or idx >= len(row):
                    return None
                val = row[idx]
                return val if val != "" else None

            cur = cash_col("CurrencyPrimary")
            if not cur:
                continue

            # 仅统计基准货币的现金；若尚未解析出 base_currency，则接受第一个货币
            if base_currency and cur != base_currency:
                continue

            ending_cash = _safe_float(cash_col("EndingCash"))
            ending_settled = _safe_float(cash_col("EndingSettledCash"))

            if ending_cash is not None:
                cash_total = (cash_total or 0.0) + ending_cash
            if ending_settled is not None:
                cash_settled = (cash_settled or 0.0) + ending_settled

        summary = snapshot["summary"]
        if cash_total is not None:
            summary["ending_cash"] = cash_total
        if cash_settled is not None:
            summary["ending_settled_cash"] = cash_settled
    except Exception:
        # 解析 Cash Report 失败时静默忽略，不影响持仓部分
        pass

    return snapshot


def _backfill_realized_pnl_fifo(trades: List[Dict[str, Any]]) -> None:
    """
    当 Flex 报表 Trades 段没有提供 Realized P&L 列时，
    基于逐笔成交价格和数量，按符号（symbol）维度使用 FIFO 规则补全卖出成交的已实现盈亏。
    """
    # key: (symbol, currency_primary)
    positions: Dict[Tuple[str, str], List[Dict[str, float]]] = {}

    for trade in trades:
        symbol = (trade.get("symbol") or "").strip()
        currency = (trade.get("currency_primary") or "").strip()
        if not symbol:
            continue
        key = (symbol, currency)

        side = trade.get("side")
        qty_raw = trade.get("quantity")
        price_raw = trade.get("price")
        if not isinstance(qty_raw, (int, float)) or not isinstance(price_raw, (int, float)):
            continue

        qty = float(qty_raw)
        price = float(price_raw)
        lots = positions.setdefault(key, [])

        # Flex 中 BUY 数量通常为正，SELL 为负
        if side == "BUY" and qty > 0:
            lots.append({"qty": qty, "price": price})
        elif side == "SELL" and qty < 0:
            remaining = abs(qty)
            realized = 0.0

            # 使用 FIFO 将卖出数量与历史买入逐笔配对
            while remaining > 0 and lots:
                lot = lots[0]
                match_qty = min(remaining, lot["qty"])
                realized += (price - lot["price"]) * match_qty
                lot["qty"] -= match_qty
                remaining -= match_qty
                if lot["qty"] <= 0:
                    lots.pop(0)

            # 若本条成交此前未提供 realized_pnl，且确实产生了配对数量，则写入补全值
            if trade.get("realized_pnl") is None and abs(realized) > 0:
                # 保留两位小数以对齐报表常见格式
                trade["realized_pnl"] = round(realized, 2)


def parse_trades_from_csv(csv_text: str) -> List[Dict[str, Any]]:
    """
    从 IBKR Flex CSV 报表中解析历史成交记录（Trades 段）。

    解析思路（尽量与官方 Flex Trades 段结构兼容）：
    - 再次扫描 CSV，找到同时包含 AssetClass / Symbol / Quantity / TradePrice / Buy/Sell 的表头行；
    - 使用该表头解析后续记录，直到遇到新的段落表头（例如再次出现 ClientAccountID 或空行）；
    - 仅保留数量非 0 的行。
    """
    lines = csv_text.splitlines()
    reader = csv.reader(lines)

    header_row: Optional[List[str]] = None
    header_index: Dict[str, int] = {}
    in_trades_section = False
    trades: List[Dict[str, Any]] = []

    for row in reader:
        if not row:
            continue

        # 尚未进入 Trades 段：寻找表头
        if not in_trades_section:
            if (
                "AssetClass" in row
                and "Symbol" in row
                and "Quantity" in row
                and "TradePrice" in row
                and ("Buy/Sell" in row or "Side" in row)
            ):
                header_row = row
                header_index = {name: idx for idx, name in enumerate(header_row)}
                in_trades_section = True
            continue

        # 已在 Trades 段：遇到新的段落表头则结束解析
        if row[0] == "ClientAccountID":
            break

        if not header_index:
            continue

        def col(name: str) -> Optional[str]:
            idx = header_index.get(name)
            if idx is None or idx >= len(row):
                return None
            val = row[idx]
            return val if val != "" else None

        level = (col("LevelOfDetail") or "").strip().upper()
        # 只保留“已成交”行：LevelOfDetail 为 EXECUTION/EXECUTIONS；过滤掉 Orders、汇总、Closed Lots 等
        if level not in ("EXECUTION", "EXECUTIONS"):
            continue

        # 数量为 0 的记录跳过
        quantity = _safe_float(col("Quantity")) or 0.0
        if quantity == 0:
            continue

        price = _safe_float(col("TradePrice")) or 0.0
        amount = _safe_float(col("TradeMoney"))

        # 仅保留真实成交：TradeMoney 非零（未成交/挂单常为 0 或空）
        if amount is None or amount == 0:
            continue

        # 若报表包含 IB Execution ID 列，则只保留该列非空的行（已成交才有执行 ID）
        id_cols = ("IB Execution ID", "IBExecutionID", "IbExecutionId")
        if any(c in header_index for c in id_cols):
            ib_exec_id = col("IB Execution ID") or col("IBExecutionID") or col("IbExecutionId")
            if not (ib_exec_id and str(ib_exec_id).strip()):
                continue

        # 与 filter_ibkr_trades 一致：卖出但 Quantity 为正视为未成交（Flex 已成交卖出为负）
        raw_side = col("Buy/Sell") or col("Side")
        if raw_side and raw_side.strip().upper().startswith("S") and quantity > 0:
            continue
        # 若存在 TransactionType 列且为空，视为未成交
        if "TransactionType" in header_index and not (col("TransactionType") or "").strip():
            continue
        # 若存在 IBOrderID 列且为空，视为未成交
        for order_col in ("IBOrderID", "IB Order ID"):
            if order_col in header_index:
                if not (col(order_col) or "").strip():
                    continue
                break

        # 已实现盈亏（Realized P&L），不同 Flex 模板可能使用不同列名，做兼容性解析
        realized_pnl_raw: Optional[str] = None
        for name in (
            "RealizedPNL",
            "Realized P&L",
            "RealizedPL",
            "Realized P/L",
            "RealizedPnl",
            "RealizedProfitLoss",
        ):
            realized_pnl_raw = col(name)
            if realized_pnl_raw is not None:
                break
        realized_pnl = _safe_float(realized_pnl_raw)

        raw_side = col("Buy/Sell") or col("Side")
        side: Optional[str] = None
        if raw_side:
            s = raw_side.strip().upper()
            if s.startswith("B"):
                side = "BUY"
            elif s.startswith("S"):
                side = "SELL"

        raw_date = col("TradeDate") or col("ReportDate")
        trade_date: Optional[str] = None
        if raw_date:
            d = raw_date.strip()
            # Flex TradeDate / ReportDate 多为 YYYYMMDD，统一为 YYYY-MM-DD 便于前端展示和区间过滤
            if len(d) == 8 and d.isdigit():
                trade_date = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
            else:
                trade_date = d

        trade: Dict[str, Any] = {
            "symbol": col("Symbol") or "",
            "description": col("Description") or "",
            "asset_class": col("AssetClass") or "",
            "currency_primary": col("CurrencyPrimary") or col("Currency") or "",
            "quantity": quantity,
            "price": price,
            "amount": amount,
            "realized_pnl": realized_pnl,
            "side": side,
            "trade_date": trade_date,
            "report_date": col("ReportDate"),
            "exchange": col("ListingExchange") or col("Exchange"),
        }
        trades.append(trade)

    # 若 Flex 报表本身未提供 Realized P&L 列或对应单元格为空，
    # 使用 FIFO 规则在内存中补全卖出成交的已实现盈亏。
    _backfill_realized_pnl_fifo(trades)

    return trades


def fetch_ibkr_positions_snapshot() -> Dict[str, Any]:
    """
    统一入口：从 IBKR Flex 获取最新报表并解析为持仓快照。

    此函数为同步调用，建议在 FastAPI 路由中通过线程池执行以避免阻塞事件循环。
    """
    token, query_id = _get_env_credentials()
    ref_code = send_flex_request(token=token, query_id=query_id)
    statement_text = get_flex_statement(reference_code=ref_code, token=token)

    # 刷新时把原始报表保存到本地，便于测试和对照 CSV 列
    try:
        project_root = Path(__file__).resolve().parent.parent.parent
        save_dir = project_root / "data" / "ibkr_flex"
        save_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        save_path = save_dir / f"ibkr_flex_statement_{ts}.csv"
        save_path.write_text(statement_text, encoding="utf-8", errors="replace")
        logger.info("IBKR Flex 报表已保存到本地: %s", save_path)
    except Exception as e:
        logger.warning("保存 IBKR Flex 报表到本地失败（不影响刷新）: %s", e)

    # 假定 Flex Query 输出格式已配置为 CSV
    snapshot = parse_positions_from_csv(statement_text)
    trades = parse_trades_from_csv(statement_text)

    # 附加元数据
    snapshot.setdefault("fetched_at", datetime.utcnow().isoformat())
    snapshot.setdefault("source", "ibkr_flex")
    return snapshot, trades

