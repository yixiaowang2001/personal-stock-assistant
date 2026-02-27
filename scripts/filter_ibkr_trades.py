#!/usr/bin/env python3
"""
IBKR 交易记录过滤工具：解析 Flex CSV 中的 Trades 段，按规则过滤未成交记录并输出已成交/未成交两份 CSV。

用法：
  python scripts/filter_ibkr_trades.py [csv文件路径]
  不传路径时，默认使用 data/ibkr_flex/ 下最新一份 ibkr_flex_statement_*.csv
"""
from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

import pandas as pd

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IBKR_DIR = PROJECT_ROOT / "data" / "ibkr_flex"


def find_trades_section(lines: list[str]) -> tuple[list[str], list[str] | None]:
    """
    在完整 Flex CSV 行列表中定位 Trades 段：表头同时包含 Buy/Sell 与 TradeDate，返回表头与数据行。
    """
    header = None
    data_start = None

    for i, line in enumerate(lines):
        line_stripped = line.strip()
        if not line_stripped:
            continue
        # 支持标准 CSV 表头（可能带引号）
        if "Buy/Sell" in line_stripped and "TradeDate" in line_stripped:
            # 用 csv.reader 解析该行，避免列名内逗号/引号问题
            row = next(csv.reader([line_stripped]))
            header = [c.strip().strip('"') for c in row]
            data_start = i + 1
            break

    if header is None or data_start is None:
        return lines, None

    # 从 data_start 起收集数据行，直到遇到新段落（下一段表头多为 ClientAccountID 开头）
    data_lines = []
    for line in lines[data_start:]:
        line_stripped = line.strip()
        if not line_stripped:
            continue
        if line_stripped.startswith("ClientAccountID") or (
            "ClientAccountID" in line_stripped.split(",")[0]
        ):
            break
        data_lines.append(line_stripped)

    return header, data_lines


def parse_ibkr_flex_csv(file_path: str | Path) -> pd.DataFrame:
    """
    解析 IBKR Flex CSV 文件，提取交易记录部分为 DataFrame。
    表头通过包含 Buy/Sell 与 TradeDate 的行识别，数据行按 CSV 解析。
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    header, data_lines = find_trades_section(lines)
    if data_lines is None:
        raise ValueError("未找到交易记录部分（需包含 Buy/Sell 与 TradeDate 的表头），请检查 CSV 格式")

    # 用 csv.reader 解析每行，保证引号内逗号正确
    rows = []
    for line in data_lines:
        row = next(csv.reader([line]))
        rows.append(row)

    if not rows:
        return pd.DataFrame(columns=header)

    # 对齐列数：以表头为准，不足补空
    n_cols = len(header)
    aligned = []
    for row in rows:
        if len(row) < n_cols:
            row = row + [""] * (n_cols - len(row))
        elif len(row) > n_cols:
            row = row[:n_cols]
        aligned.append(row)

    df = pd.DataFrame(aligned, columns=header)

    # 数值列
    numeric_fields = [
        "Quantity",
        "TradePrice",
        "TradeMoney",
        "Proceeds",
        "IB Commission",
        "IBCommission",
        "NetCash",
        "Cost Basis",
        "CostBasis",
        "FifoPnlRealized",
        "RealizedPNL",
        "Realized P&L",
    ]
    for field in numeric_fields:
        if field in df.columns:
            df[field] = pd.to_numeric(df[field], errors="coerce")

    # 日期列
    for date_col in ("TradeDate", "ReportDate", "DateTime"):
        if date_col in df.columns:
            try:
                df[date_col] = pd.to_datetime(df[date_col], format="%Y%m%d", errors="coerce")
            except Exception:
                try:
                    df[date_col] = pd.to_datetime(
                        df[date_col], format="%Y%m%d %H:%M:%S", errors="coerce"
                    )
                except Exception:
                    pass

    return df


def filter_unfilled_trades(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    按规则过滤未成交记录，返回 (已成交, 未成交) 两个 DataFrame。
    规则：满足「4 个关键金额全为空」且「卖出且 Quantity>0 / TransactionType 空 / IBOrderID 空」之一则视为未成交。
    缺失的列不参与条件判断（不报错）。
    """
    df = df.copy()

    # 条件1：4 个关键金额字段全部为空或 NaN
    money_cols = ["TradeMoney", "Proceeds", "IB Commission", "IBCommission", "NetCash"]
    existing_money = [c for c in money_cols if c in df.columns]
    if not existing_money:
        # 至少要有 TradeMoney 或 Proceeds 之一
        existing_money = [c for c in ("TradeMoney", "Proceeds") if c in df.columns]
    if not existing_money:
        return df, pd.DataFrame()

    def is_empty(ser: pd.Series, treat_zero: bool = False) -> pd.Series:
        out = ser.isna() | (ser.astype(str).str.strip() == "")
        if treat_zero and pd.api.types.is_numeric_dtype(ser):
            out = out | (ser == 0)
        return out

    condition1 = is_empty(df[existing_money[0]], treat_zero=True)
    for c in existing_money[1:]:
        condition1 = condition1 & is_empty(df[c], treat_zero=True)

    # 条件2：卖出且 Quantity 为正（已成交卖出在 Flex 里常为负）
    if "Buy/Sell" in df.columns and "Quantity" in df.columns:
        qty = pd.to_numeric(df["Quantity"], errors="coerce")
        condition2 = (
            (df["Buy/Sell"].astype(str).str.upper().str.strip() == "SELL") & (qty > 0)
        )
    else:
        condition2 = pd.Series(False, index=df.index)

    # 条件3：TransactionType 为空
    if "TransactionType" in df.columns:
        condition3 = is_empty(df["TransactionType"])
    else:
        condition3 = pd.Series(False, index=df.index)

    # 条件4：IBOrderID 或 IB Order ID 为空
    order_id_col = None
    for c in ("IBOrderID", "IB Order ID"):
        if c in df.columns:
            order_id_col = c
            break
    if order_id_col is not None:
        condition4 = is_empty(df[order_id_col])
    else:
        condition4 = pd.Series(False, index=df.index)

    # 条件5：卖出但 TradeMoney 为 0 或空（典型未成交）
    if "TradeMoney" in df.columns and "Buy/Sell" in df.columns:
        tm = pd.to_numeric(df["TradeMoney"], errors="coerce")
        condition5 = (
            (tm.isna() | (tm == 0))
            & (df["Buy/Sell"].astype(str).str.upper().str.strip() == "SELL")
        )
    else:
        condition5 = pd.Series(False, index=df.index)

    unfilled = condition1 & (condition2 | condition3 | condition4) | condition5
    filled = df[~unfilled].copy()
    unfilled_df = df[unfilled].copy()

    return filled, unfilled_df


def main() -> None:
    if len(sys.argv) >= 2:
        input_path = Path(sys.argv[1])
    else:
        if not DEFAULT_IBKR_DIR.is_dir():
            print(f"未找到目录: {DEFAULT_IBKR_DIR}")
            print("请先刷新持仓以生成 CSV，或执行: python scripts/filter_ibkr_trades.py <csv路径>")
            sys.exit(1)
        files = sorted(
            DEFAULT_IBKR_DIR.glob("ibkr_flex_statement_*.csv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not files:
            print(f"目录下无 ibkr_flex_statement_*.csv: {DEFAULT_IBKR_DIR}")
            sys.exit(1)
        input_path = files[0]
        print(f"使用最新文件: {input_path.name}")

    print("1. 正在解析 IBKR Flex CSV...")
    try:
        full_df = parse_ibkr_flex_csv(input_path)
    except Exception as e:
        print(f"解析失败: {e}")
        sys.exit(1)

    if full_df.empty:
        print("未解析到任何交易记录。")
        sys.exit(0)

    print(f"   共 {len(full_df)} 条交易记录")

    print("\n2. 正在过滤未成交记录...")
    filled_df, _ = filter_unfilled_trades(full_df)
    n_filled = len(filled_df)
    n_unfilled = len(full_df) - n_filled
    print(f"   已成交: {n_filled} 条（仅保留此项）")
    if len(full_df) > 0 and n_unfilled > 0:
        print(f"   已过滤未成交: {n_unfilled} 条")

    out_dir = input_path.parent
    base_name = input_path.stem

    out_path = out_dir / f"{base_name}_filled.csv"
    filled_df.to_csv(out_path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_ALL)
    print(f"\n3. 已成交记录已保存: {out_path}")

    if "Symbol" in filled_df.columns and not filled_df.empty:
        print("\n4. 按代码统计（已成交，示例）：")
        for sym in filled_df["Symbol"].dropna().unique()[:10]:
            if not str(sym).strip():
                continue
            n = (filled_df["Symbol"] == sym).sum()
            print(f"   {sym}: {n} 条")

    print("\n完成。")


if __name__ == "__main__":
    main()
