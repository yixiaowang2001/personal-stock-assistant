import logging
import json
import asyncio
import math
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.core.database import get_mongo_db
from app.routers.auth_db import get_current_user
from app.core.response import ok
from app.core.unified_config import UnifiedConfigManager
from app.models.config import LLMConfig
from tradingagents.llm_adapters.openai_compatible_base import create_openai_compatible_llm, OPENAI_COMPATIBLE_PROVIDERS

logger = logging.getLogger("webapi")
_unified_config = UnifiedConfigManager()


class PortfolioRecommendationRequest(BaseModel):
  report_ids: List[str] = Field(
    ...,
    description="分析报告ID列表（含持仓对应报告 + 可选报告，总数最多 20）",
  )
  model_name: Optional[str] = Field(
    default=None,
    description="可选，本次持仓推荐指定使用的大模型名称（model_name）",
  )

  @field_validator("report_ids")
  @classmethod
  def validate_report_ids(cls, v: List[str]) -> List[str]:
    unique_ids = list(dict.fromkeys([i for i in v if i]))
    if not unique_ids:
      raise ValueError("至少需要选择 1 份报告")
    if len(unique_ids) > 20:
      raise ValueError("报告总数不能超过 20 份（含持仓对应报告与可选报告）")
    return unique_ids


class PortfolioRecommendationItem(BaseModel):
  """
  V2 持仓推荐条目（已经过资金与股数计算的结果）。
  """

  ticker: str
  name: Optional[str] = None
  action: str

  # 当前持仓与价格信息
  current_price: Optional[float] = None
  current_shares: Optional[float] = None
  current_value: Optional[float] = None

  # 报告中解析出的行情信息（价格、涨跌幅、成交量等）
  quote_price: Optional[float] = None
  quote_change: Optional[float] = None
  quote_change_percent: Optional[float] = None
  quote_volume: Optional[float] = None

  # 目标仓位与建议股数（target_allocation 基于调整后总资产）
  target_allocation: Optional[float] = None
  suggested_shares: Optional[float] = None  # >0 买入股数，<0 卖出股数

  # 说明信息
  reason: Optional[str] = None
  risk: Optional[str] = None


class PortfolioRecommendationPayload(BaseModel):
  """
  V2 持仓推荐整体返回结构。
  """

  base_currency: Optional[str] = None
  as_of_date: Optional[str] = None

  # 资产视角
  total_value: Optional[float] = None
  cash_before: Optional[float] = None
  cash_after: Optional[float] = None

  # 现金配置（目标现金比例及说明）
  cash_allocation: Optional[float] = None
  cash_reason: Optional[str] = None

  # 诊断与行业配置建议
  analysis: Optional[str] = None
  sector_advice: Optional[str] = None

  # 逐股条目
  items: List[PortfolioRecommendationItem] = []

  # 价格缺失的标的列表（仅作为辅助信息）
  price_missing_tickers: List[str] = []

  # 记录本次用于生成推荐的大模型名称（规则兜底时为空）
  used_model: Optional[str] = None
  # 推荐模式：llm 或 rule_fallback
  mode: Optional[str] = None


class InsufficientFundsError(ValueError):
  """
  表示在根据目标仓位计算交易方案时，可用资金不足。
  """


router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


async def _get_latest_ibkr_snapshot(user_id: str) -> Dict[str, Any]:
  db = get_mongo_db()
  coll = db["ibkr_positions"]
  doc = await coll.find_one(
    {"user_id": user_id},
    sort=[("as_of_date", -1), ("created_at", -1)],
  )
  if not doc:
    raise HTTPException(
      status_code=status.HTTP_400_BAD_REQUEST,
      detail="尚未从 IBKR 同步持仓，请先在“持仓信息”页面点击刷新按钮。",
    )

  summary = doc.get("summary") or {}
  cash = None
  ending_settled = summary.get("ending_settled_cash")
  ending_cash = summary.get("ending_cash")
  if isinstance(ending_settled, (int, float)):
    cash = float(ending_settled)
  elif isinstance(ending_cash, (int, float)):
    cash = float(ending_cash)

  snapshot = {
    "as_of_date": doc.get("as_of_date"),
    "base_currency": doc.get("base_currency"),
    "cash": cash,
    "positions": doc.get("positions", []),
  }
  return snapshot


async def _load_reports(report_ids: List[str]) -> List[Dict[str, Any]]:
  db = get_mongo_db()
  object_ids: List[ObjectId] = []
  for rid in report_ids:
    try:
      object_ids.append(ObjectId(rid))
    except Exception:
      logger.warning("无效的报告ID，跳过: %s", rid)

  if not object_ids:
    raise HTTPException(
      status_code=status.HTTP_400_BAD_REQUEST,
      detail="报告ID无效，请重新选择报告。",
    )

  cursor = db.analysis_reports.find({"_id": {"$in": object_ids}})
  docs: List[Dict[str, Any]] = []
  async for doc in cursor:
    docs.append(doc)

  if not docs:
    raise HTTPException(
      status_code=status.HTTP_400_BAD_REQUEST,
      detail="未在数据库中找到对应的分析报告，请确认报告仍然存在。",
    )

  return docs


def _extract_price_from_report_doc(doc: Dict[str, Any]) -> Optional[float]:
  """
  兼容旧接口：仅从报告中提取参考价格。
  """
  quote = _extract_quote_from_report_doc(doc)
  if not quote:
    return None
  return quote.get("price")


def _extract_quote_from_report_doc(
  doc: Dict[str, Any],
) -> Optional[Dict[str, Optional[float]]]:
  """
  从报告文档中提取行情信息（价格、涨跌幅、成交量）。

  优先使用结构化字段 current_price/report_price/price_change 等；
  若缺失，则尝试从 reports 文本中的“当前价格/涨跌幅/成交量”段落里解析数字。
  """
  price: Optional[float] = None
  change: Optional[float] = None
  change_percent: Optional[float] = None
  volume: Optional[float] = None

  # 1）结构化字段
  for val in [
    doc.get("current_price"),
    doc.get("report_price"),
  ]:
    if isinstance(val, (int, float)):
      try:
        price = float(val)
        break
      except Exception:
        continue

  for val in [
    doc.get("price_change"),
    doc.get("change"),
  ]:
    if isinstance(val, (int, float)):
      try:
        change = float(val)
        break
      except Exception:
        continue

  for val in [
    doc.get("price_change_percent"),
    doc.get("pct_chg"),
    doc.get("change_percent"),
  ]:
    if isinstance(val, (int, float)):
      try:
        change_percent = float(val)
        break
      except Exception:
        continue

  for val in [
    doc.get("volume"),
    doc.get("vol"),
    doc.get("turnover_volume"),
  ]:
    if isinstance(val, (int, float)):
      try:
        volume = float(val)
        break
      except Exception:
        continue

  # 2）从文本块中解析 “当前价格/涨跌幅/成交量”
  reports_dict = doc.get("reports") or {}
  if not isinstance(reports_dict, dict):
    if any(v is not None for v in [price, change, change_percent, volume]):
      return {
        "price": price,
        "change": change,
        "change_percent": change_percent,
        "volume": volume,
      }
    return None

  text_blocks: List[str] = []
  # 优先从技术/基本面相关模块中查找
  for key in [
    "market_report",
    "fundamentals_report",
    "trader_investment_plan",
    "research_team_decision",
    "final_trade_decision",
  ]:
    block = reports_dict.get(key)
    if isinstance(block, str) and block:
      text_blocks.append(block)

  # 允许在字段名和数值之间存在 Markdown 粗体标记、冒号、空格等非数字字符
  price_pattern = re.compile(r"当前价格[^\d\r\n]{0,20}([+-]?\d+(?:\.\d+)?)")
  # 同时包含绝对变动和百分比：涨跌幅：+16.74 (+12.18%)
  change_both_pattern = re.compile(
    r"涨跌幅[^\d\r\n]{0,20}([+-]?\d+(?:\.\d+)?)\s*\(\s*([+-]?\d+(?:\.\d+)?)\s*%\s*\)"
  )
  # 仅有百分比：涨跌幅：-19.35%
  change_percent_pattern = re.compile(
    r"涨跌幅[^\d\r\n]{0,20}([+-]?\d+(?:\.\d+)?)\s*%"
  )
  # 仅有绝对值（目前未观察到，但保留以兼容潜在格式）
  change_pattern = re.compile(
    r"涨跌幅[^\d\r\n]{0,20}([+-]?\d+(?:\.\d+)?)\b(?!\s*%)"
  )
  # 成交量：31,434,900 股
  volume_pattern = re.compile(r"成交量[^\d\r\n]{0,20}([\d,]+)")

  # 先在优先模块中尝试
  for block in text_blocks:
    if price is None:
      try:
        m = price_pattern.search(block)
        if m:
          value_str = m.group(1)
          price = float(value_str)
      except Exception:
        pass

    if change is None or change_percent is None:
      try:
        m_both = change_both_pattern.search(block)
        if m_both:
          abs_str = m_both.group(1)
          pct_str = m_both.group(2)
          change = float(abs_str)
          change_percent = float(pct_str)
        else:
          if change is None:
            m_abs = change_pattern.search(block)
            if m_abs:
              abs_str = m_abs.group(1)
              change = float(abs_str)
          if change_percent is None:
            m_pct = change_percent_pattern.search(block)
            if m_pct:
              pct_str = m_pct.group(1)
              change_percent = float(pct_str)
      except Exception:
        pass

    if volume is None:
      try:
        m = volume_pattern.search(block)
        if m:
          vol_str = m.group(1).replace(",", "")
          volume = float(vol_str)
      except Exception:
        pass

  # 若仍未找到，则在所有文本型 reports 字段中兜底搜索一次
  for block in reports_dict.values():
    if not isinstance(block, str) or not block:
      continue
    if (
      price is not None
      and change is not None
      and change_percent is not None
      and volume is not None
    ):
      break
    try:
      if price is None:
        m_price = price_pattern.search(block)
        if m_price:
          value_str = m_price.group(1)
          price = float(value_str)
      if change is None or change_percent is None:
        m_both = change_both_pattern.search(block)
        if m_both:
          abs_str = m_both.group(1)
          pct_str = m_both.group(2)
          change = float(abs_str)
          change_percent = float(pct_str)
        else:
          if change is None:
            m_abs = change_pattern.search(block)
            if m_abs:
              abs_str = m_abs.group(1)
              change = float(abs_str)
          if change_percent is None:
            m_pct = change_percent_pattern.search(block)
            if m_pct:
              pct_str = m_pct.group(1)
              change_percent = float(pct_str)
      if volume is None:
        m_vol = volume_pattern.search(block)
        if m_vol:
          vol_str = m_vol.group(1).replace(",", "")
          volume = float(vol_str)
    except Exception:
      continue

  if all(v is None for v in [price, change, change_percent, volume]):
    return None
  return {
    "price": price,
    "change": change,
    "change_percent": change_percent,
    "volume": volume,
  }


def _ensure_unique_symbol_map(reports: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
  by_symbol: Dict[str, Dict[str, Any]] = {}
  for doc in reports:
    symbol = doc.get("stock_symbol") or doc.get("stock_code")
    if not symbol:
      continue
    if symbol in by_symbol:
      raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"股票代码 {symbol} 对应多份报告，请仅保留一份。",
      )
    by_symbol[symbol] = doc
  return by_symbol


def _build_llm_context(
  snapshot: Dict[str, Any],
  reports: List[Dict[str, Any]],
) -> Dict[str, Any]:
  positions = snapshot.get("positions", []) or []
  base_currency = snapshot.get("base_currency")
  cash = snapshot.get("cash")

  # 计算持仓市值与总资产（持仓市值 + 现金）
  total_position_value = None
  try:
    summary = snapshot.get("summary") or {}
    tv = summary.get("total_position_value")
    if isinstance(tv, (int, float)):
      total_position_value = float(tv)
  except Exception:
    total_position_value = None
  if total_position_value is None:
    acc = 0.0
    for p in positions:
      v = p.get("position_value")
      if isinstance(v, (int, float)):
        acc += float(v)
    total_position_value = acc if acc > 0 else None

  total_assets = None
  try:
    c = float(cash) if isinstance(cash, (int, float)) else 0.0
    pv = float(total_position_value) if isinstance(total_position_value, (int, float)) else 0.0
    total_assets = pv + c
  except Exception:
    total_assets = None

  by_symbol = _ensure_unique_symbol_map(reports)

  position_map: Dict[str, Dict[str, Any]] = {}
  for p in positions:
    sym = p.get("symbol")
    if sym:
      position_map[sym] = p

  report_items: List[Dict[str, Any]] = []
  for symbol, doc in by_symbol.items():
    stock_name = doc.get("stock_name") or symbol
    recommendation_text = str(doc.get("recommendation") or "")
    summary_text = str(doc.get("summary") or "")
    key_points = doc.get("key_points") or []
    key_points_text = "\n".join([str(k) for k in key_points if k])[:400]

    reports_dict = doc.get("reports") or {}
    final_decision_raw = reports_dict.get("final_trade_decision") or ""
    final_decision_excerpt = (
      str(final_decision_raw).strip().replace("#", "").replace("*", "")[:600]
      if final_decision_raw
      else ""
    )

    # 控制单报告文本总长度，避免上下文过长
    def _trim(text: str, max_len: int) -> str:
      if len(text) <= max_len:
        return text
      return text[: max_len - 3] + "..."

    summary_compact = _trim(summary_text, 600)
    recommendation_compact = _trim(recommendation_text, 600)
    final_decision_compact = _trim(final_decision_excerpt, 800)

    has_position = symbol in position_map and bool(
      position_map[symbol].get("quantity"),
    )
    position_snapshot = position_map.get(symbol) or {}

    # 报告中可能包含的参考价格字段（例如 current_price/report_price 或“当前价格：xxx”文本）
    report_price = _extract_price_from_report_doc(doc)

    report_items.append(
      {
        "stock_symbol": symbol,
        "stock_name": stock_name,
        "has_position": has_position,
        "position": {
          "quantity": position_snapshot.get("quantity"),
          "position_value": position_snapshot.get("position_value"),
          "avg_cost": position_snapshot.get("avg_cost"),
          "unrealized_pnl": position_snapshot.get("unrealized_pnl"),
          "currency": position_snapshot.get("currency_primary"),
        },
        # 若有，从报告中提供价格参考，供 LLM 和后续计算使用
        "report_price": report_price,
        "summary": summary_compact,
        "recommendation": recommendation_compact,
        "key_points": key_points,
        "final_decision_excerpt": final_decision_compact,
      }
    )

  context = {
    "portfolio": {
      "base_currency": base_currency,
      "cash": cash,
      "total_position_value": total_position_value,
      "total_assets": total_assets,
      "positions": positions,
    },
    "reports": report_items,
  }
  return context


def _select_llm_config(requested_model_name: Optional[str] = None) -> LLMConfig:
  try:
    llm_configs = _unified_config.get_llm_configs()
  except Exception as e:
    logger.error("获取 LLM 配置失败: %s", e)
    raise HTTPException(
      status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
      detail="获取大模型配置失败，请检查系统设置。",
    )

  enabled = [c for c in llm_configs if getattr(c, "enabled", True)]
  if not enabled:
    raise HTTPException(
      status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
      detail="未找到可用的大模型配置，请在设置中启用至少一个模型。",
    )

  # 1）如果前端显式指定了模型，则优先按 model_name 精确匹配
  if requested_model_name:
    for c in enabled:
      if c.model_name == requested_model_name:
        return c
    logger.warning(
      "未在已启用模型中找到指定的 model_name=%s，将回退到系统默认配置。",
      requested_model_name,
    )

  # 2）其次读取系统设置中的 quick_think_llm / quick_analysis_model
  chosen: Optional[LLMConfig] = None
  try:
    settings = _unified_config.get_system_settings()
    preferred = settings.get("quick_think_llm") or settings.get("quick_analysis_model")
    if preferred:
      for c in enabled:
        if c.model_name == preferred:
          chosen = c
          break
  except Exception as e:
    logger.warning("读取系统 LLM 设置失败，将使用第一个启用的模型: %s", e)

  if chosen is None:
    chosen = enabled[0]
  return chosen


async def _call_portfolio_llm(
  context: Dict[str, Any],
  requested_model_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
  """
  调用 TradingAgents 提供的 OpenAI 兼容 LLM，返回解析后的 JSON dict。

  约定返回结构（V2）：
  {
    "analysis": "组合诊断文本...",
    "suggestions": [
      {
        "ticker": "AMD",
        "name": "ADVANCED MICRO DEVICES",
        "action": "reduce",
        "target_allocation": 0.35,
        "reason": "...",
        "risk": "..."
      }
    ],
    "sector_advice": "行业配置建议文本...",
    "cash_target_allocation": 0.05
  }
  """
  llm_config = _select_llm_config(requested_model_name=requested_model_name)

  provider_raw = (llm_config.provider or "custom_openai").lower()
  if provider_raw in OPENAI_COMPATIBLE_PROVIDERS:
    provider_for_adapter = provider_raw
  else:
    # 其它提供商统一走 custom_openai，由配置的 api_base + 环境变量密钥控制
    provider_for_adapter = "custom_openai"

  # 为保证推荐结果稳定，这里强制使用 temperature=0
  temperature = 0.0
  max_tokens = llm_config.max_tokens or 2048
  timeout = getattr(llm_config, "timeout", 120) or 120
  api_base = llm_config.api_base or llm_config.custom_endpoint

  try:
    llm = create_openai_compatible_llm(
      provider=provider_for_adapter,
      model=llm_config.model_name,
      api_key=None,  # 具体密钥由适配器从环境变量或厂家配置中获取
      temperature=temperature,
      max_tokens=max_tokens,
      base_url=api_base,
      timeout=timeout,
    )
  except Exception as e:
    logger.error("创建 LLM 实例失败: %s", e)
    return None

  system_prompt = (
    "你是一位资深的投资组合经理，擅长多因子分析、风险预算和行业轮动。"
    "你只能基于给定的 JSON 上下文进行推理，不得引入任何实时行情、新闻或外部主观判断。"
    "你的任务是：在给定的 IBKR 持仓快照和若干单股分析报告摘要基础上，从组合视角给出结构化的持仓调整建议。"
    "所有结论仅用于个人研究和复盘记录，不构成任何形式的投资建议。\n\n"
    "上下文 JSON 包含：\n"
    "1）portfolio：当前组合快照，含 base_currency（基准货币）、cash（可用资金）、"
    "total_position_value（持仓总市值，如可用）、total_assets（总资产≈持仓市值+现金，如可用）、"
    "以及 positions（逐笔持仓，含 symbol、description、quantity、position_value、avg_cost、unrealized_pnl 等字段）。\n"
    "2）reports：若干已完成的单股分析报告摘要。每一项包含：stock_symbol、stock_name、"
    "has_position（当前是否有持仓）、position（若有持仓则给出数量、市值、成本、浮盈亏等）、"
    "report_price（若存在，则为报告时点或分析时点的参考价格）、"
    "summary/recommendation/final_decision_excerpt 等文本要点。\n\n"
    "你需要在组合层面和个股层面给出有条理的总结与目标“仓位比例”建议，"
    "特别要区分：已有持仓的股票与当前无持仓但有报告的新股票。"
    "避免使用“稳赚”“必然”“保证”等极端措辞，保持风险中性、表述克制。"
  )

  schema_description = {
    "analysis": "字符串，组合层面的诊断与说明，建议覆盖集中度、行业/风格暴露、现金水平和主要风险点，约 200 字以上",
    "suggestions": [
      {
        "ticker": "字符串，股票代码，必须来自 portfolio.positions 或 reports 的股票代码列表，不得虚构",
        "name": "字符串，股票名称，可选",
        "action": "字符串，increase/decrease/exit/hold/buy/avoid 之一（buy 用于当前无持仓但建议新建仓）",
        "target_allocation": "数字，0–1 之间，表示该股票在调整后总资产中的目标仓位比例；不打算持有则为 0，清仓/退出类操作必须显式给出 0",
        "reason": "字符串，核心逻辑与关键依据（结合报告要点与当前持仓情况）",
        "risk": "字符串，主要风险提示（如估值、业绩不确定性、政策/宏观风险、单一标的集中度等）",
      }
    ],
    "sector_advice": "字符串，对行业或板块配置方向的建议；可针对任意行业或领域（不限于当前持仓与报告中的标的），如军工、消费、医药、科技等，并可点名 1–2 只示例股票作为参考，这些示例不应出现在 suggestions 数组中",
    "cash_allocation": "数字，可选，0–1 之间，表示建议保留为现金的目标比例；如缺省则由程序使用 1-∑(股票 target_allocation) 自动推算",
    "cash_reason": "字符串，可选，对建议现金比例的解释（例如用于应对波动、等待新机会等）",
    "cash_target_allocation": "数字，可选，向后兼容字段，含义同 cash_allocation",
  }

  user_prompt = (
    "下面是当前 IBKR 投资组合快照以及若干已完成股票分析报告的 JSON 上下文。"
    "请基于该上下文，按如下步骤思考，并最终只输出一个 JSON 对象：\n"
    "1）诊断当前组合的集中度、行业/风格分布与风险暴露："
    "   - 关注前几大持仓及合计权重，对整体波动和回撤的影响；"
    "   - 从主要市场/币种和行业角度描述风险暴露方向（如美股科技、港股互联网等）；"
    "   - 评估当前现金比例及潜在流动性风险；"
    "   - 在 analysis 字段中，用不少于约 200 字的中文进行总结，并明确所有观点仅供复盘参考，不构成投资建议。\n"
    "2）结合 reports，对每只股票进行评估："
    "   - 对 has_position 为 true 的股票，判断是适度增持（increase）、减持（decrease）、清仓退出（exit）还是继续观望/持有（hold）；"
    "   - 对 has_position 为 false 的股票，在 buy（建议新建/建立一定仓位）与 avoid（暂不参与）之间做出选择；"
    "   - 对于不打算持有的股票，将 target_allocation 设为 0。\n"
    "3）考虑整体风险分散和资金效率，为每只“建议继续持有或新建仓位”的股票设置 target_allocation："
    "   - target_allocation 为 0–1 之间的小数，表示该股票在调整后总资产中的目标权重（不要求精确到小数点后两位）；"
    "   - 所有出现在 suggestions 中的股票都必须给出 target_allocation 字段，即使为 0；"
    "   - 对于清仓/退出类操作（如 action=exit/close/清仓），target_allocation 必须为 0；"
    "   - 所有股票 target_allocation 之和应 ≤ 1；如你给出 cash_allocation 或 cash_target_allocation，则股票权重之和应 ≤ 1-cash_allocation；"
    "   - 如存在仅定性建议但暂不打算实际配置的标的，请同样给出较小但明确的 target_allocation（或设为 0，表示暂不配置）。\n"
    "4）从行业与现金配置角度进行补充："
    "   - 在 sector_advice 中给出行业或板块配置建议，可涵盖任意领域（不限于上述持仓与报告中的标的），如军工、消费、医药、科技、金融等；若当前组合过于集中，可建议分散或增配的行业方向，并可点名 1–2 只示例股票作为参考；"
    "   - 这些示例股票不应出现在 suggestions 数组中；\n"
    "   - 在顶层 cash_allocation 字段中给出建议保留为现金的目标比例（如 0.25 表示 25%），并在 cash_reason 字段中简要说明该现金水平的用途与合理性。\n\n"
    "请严格按照上面给出的 JSON Schema 输出一个 JSON 对象："
    "不得输出任何额外说明文字、自然语言段落或 Markdown，只能输出 JSON。\n\n"
    f"【Schema说明】\n{json.dumps(schema_description, ensure_ascii=False, indent=2)}\n\n"
    f"【上下文JSON】\n{json.dumps(context, ensure_ascii=False)}"
  )

  messages = [
    ("system", system_prompt),
    ("human", user_prompt),
  ]

  try:
    # 使用线程池避免阻塞事件循环
    result = await asyncio.to_thread(llm.invoke, messages)
  except Exception as e:
    logger.error("调用 LLM 生成持仓推荐失败: %s", e)
    return None

  content = getattr(result, "content", None)
  if not content or not isinstance(content, str):
    logger.warning("LLM 返回内容为空或类型异常: %r", content)
    return None

  text = content.strip()

  # 解析 JSON，允许模型前后有少量噪声
  def _parse_json(s: str) -> Optional[Dict[str, Any]]:
    try:
      return json.loads(s)
    except Exception:
      # 尝试截取第一个 { 到最后一个 } 之间的内容
      try:
        start = s.index("{")
        end = s.rindex("}") + 1
        return json.loads(s[start:end])
      except Exception:
        return None

  data = _parse_json(text)
  if data is None:
    logger.warning("LLM 输出无法解析为 JSON，将回退规则版推荐。原始输出前200字符: %s", text[:200])
    return None

  if not isinstance(data, dict):
    logger.warning("LLM 输出 JSON 根对象不是 dict，将回退规则版推荐。")
    return None
  # 记录本次实际使用的模型名称，供后续构建 payload 时使用
  try:
    used_model_name = getattr(llm_config, "model_name", None)
    if used_model_name:
      data["_used_model_name"] = used_model_name
  except Exception as e:
    logger.warning("在 LLM 结果中记录 used_model_name 失败: %s", e)

  return data


def _compute_trades(
  total_value: float,
  cash: float,
  positions: List[Dict[str, Any]],
  suggestions: List[Dict[str, Any]],
) -> Dict[str, Any]:
  """
  根据目标仓位比例和当前持仓，计算需买卖金额与建议股数（整数股）。
  """
  # 构建当前持仓映射
  position_map: Dict[str, Dict[str, Any]] = {}
  for p in positions:
    sym = p.get("symbol")
    if sym:
      position_map[str(sym)] = p

  cash_before = float(cash) if isinstance(cash, (int, float)) else 0.0

  base_results: List[Dict[str, Any]] = []

  for s in suggestions:
    ticker = str(s.get("ticker") or "").strip()
    if not ticker:
      continue
    action = str(s.get("action") or "hold")
    target_allocation_raw = s.get("target_allocation")
    target_allocation = (
      float(target_allocation_raw)
      if isinstance(target_allocation_raw, (int, float))
      else None
    )

    pos = position_map.get(ticker) or {}
    quantity_raw = pos.get("quantity")
    quantity = float(quantity_raw) if isinstance(quantity_raw, (int, float)) else 0.0

    # 报告中可能给出的参考价格
    report_price = s.get("price")

    # 优先使用持仓 mark_price，其次用 position_value / quantity，再次用报告价格
    mark_price = pos.get("mark_price")
    position_value_raw = pos.get("position_value")
    position_value = (
      float(position_value_raw)
      if isinstance(position_value_raw, (int, float))
      else None
    )

    price: Optional[float] = None
    if isinstance(mark_price, (int, float)):
      price = float(mark_price)
    elif position_value is not None and quantity > 0:
      try:
        price = float(position_value) / quantity
      except Exception:
        price = None
    elif isinstance(report_price, (int, float)):
      try:
        price = float(report_price)
      except Exception:
        price = None

    current_value: Optional[float]
    if price is not None:
      current_value = quantity * price
    else:
      current_value = position_value

    target_value: Optional[float] = None
    delta_value: Optional[float] = None
    if isinstance(target_allocation, (int, float)):
      target_value = float(total_value) * float(target_allocation)
      base_current = current_value if isinstance(current_value, (int, float)) else 0.0
      delta_value = target_value - base_current

    base_results.append(
      {
        "ticker": ticker,
        "name": s.get("name"),
        "action": action,
        "target_allocation": target_allocation,
        "reason": s.get("reason"),
        "risk": s.get("risk"),
        "price": price,
        "quantity": quantity,
        "current_value": current_value,
        "target_value": target_value,
        "delta_value": delta_value,
        "new_quantity": quantity,
        "suggested_shares": None,
        "price_missing": False,
      }
    )

  # 资金约束检查：仅考虑价格与 delta_value 都已知的标的
  total_sell_value = 0.0
  total_buy_value = 0.0
  for r in base_results:
    dv = r["delta_value"]
    price = r["price"]
    qty = r["quantity"]
    if not isinstance(dv, (int, float)) or price is None:
      continue
    if dv < 0 and qty > 0:
      total_sell_value += min(-dv, qty * price)
    elif dv > 0:
      total_buy_value += dv

  funds_available = cash_before + total_sell_value
  if funds_available + 1e-6 < total_buy_value:
    raise InsufficientFundsError(
      "根据目标仓位计算，卖出后可用资金不足以完成所有买入，请适当降低部分标的目标仓位或减少新建仓标的数量后重试。"
    )

  cash_current = cash_before

  # 先卖出再买入
  for r in base_results:
    dv = r["delta_value"]
    price = r["price"]
    qty = r["new_quantity"]
    if not isinstance(dv, (int, float)) or price is None:
      continue
    if dv >= 0 or qty <= 0:
      continue

    desired_sell_value = min(-dv, qty * price)
    shares_to_sell = int(desired_sell_value // price)
    if shares_to_sell <= 0:
      continue
    if shares_to_sell > qty:
      shares_to_sell = int(qty)

    trade_value = shares_to_sell * price
    if trade_value <= 0:
      continue

    r["new_quantity"] = qty - shares_to_sell
    r["suggested_shares"] = -float(shares_to_sell)
    cash_current += trade_value

  for r in base_results:
    dv = r["delta_value"]
    price = r["price"]
    qty = r["new_quantity"]
    if not isinstance(dv, (int, float)) or price is None:
      continue
    if dv <= 0:
      continue

    desired_buy_value = dv
    max_value_can_use = min(desired_buy_value, cash_current)
    shares_to_buy = int(max_value_can_use // price)
    if shares_to_buy <= 0:
      continue

    trade_value = shares_to_buy * price
    if trade_value <= 0:
      continue

    r["new_quantity"] = qty + shares_to_buy
    prev = r.get("suggested_shares") or 0.0
    r["suggested_shares"] = float(prev + shares_to_buy)
    cash_current -= trade_value

  stock_results: List[Dict[str, Any]] = []
  for r in base_results:
    price = r["price"]
    new_qty = r["new_quantity"]
    quantity = r["quantity"]
    if price is not None:
      current_shares = new_qty
      current_value = new_qty * price
    else:
      current_shares = quantity
      current_value = r["current_value"]

    stock_results.append(
      {
        "ticker": r["ticker"],
        "name": r["name"],
        "action": r["action"],
        "current_price": price,
        "current_shares": current_shares,
        "current_value": current_value,
        "target_allocation": r["target_allocation"],
        "suggested_shares": r["suggested_shares"],
        "reason": r["reason"],
        "risk": r["risk"],
        "price_missing": price is None,
      }
    )

  return {
    "cash_before": cash_before,
    "cash_after": cash_current,
    "total_value": total_value,
    "stock_results": stock_results,
  }


def _build_recommendations_from_reports(
  snapshot: Dict[str, Any],
  reports: List[Dict[str, Any]],
) -> PortfolioRecommendationPayload:
  """
  规则兜底版本：仅基于报告关键词和当前持仓给出定性建议，不做目标仓位与资金约束计算。
  """
  positions = snapshot.get("positions", []) or []
  base_currency = snapshot.get("base_currency")
  cash = snapshot.get("cash")
  as_of_date = snapshot.get("as_of_date")

  by_symbol = _ensure_unique_symbol_map(reports)

  position_map: Dict[str, Dict[str, Any]] = {}
  for p in positions:
    sym = p.get("symbol")
    if sym:
      position_map[str(sym)] = p

  # 估算总资产：持仓市值 + 现金
  total_position_value = 0.0
  for p in positions:
    v = p.get("position_value")
    if isinstance(v, (int, float)):
      total_position_value += float(v)
  cash_val = float(cash) if isinstance(cash, (int, float)) else 0.0
  total_value = total_position_value + cash_val

  items: List[PortfolioRecommendationItem] = []

  for symbol, doc in by_symbol.items():
    symbol_str = str(symbol)
    stock_name = doc.get("stock_name") or symbol_str
    recommendation_text = str(doc.get("recommendation") or "")
    summary_text = str(doc.get("summary") or "")
    key_points = doc.get("key_points") or []
    key_points_text = "\n".join([str(k) for k in key_points if k])[:400]

    text = f"{recommendation_text}\n{summary_text}\n{key_points_text}".lower()

    pos = position_map.get(symbol_str) or {}
    has_position = bool(pos.get("quantity"))

    action = "hold"
    if any(k in text for k in ["清仓", "全仓卖出", "全部卖出", "sell all"]):
      action = "exit"
    elif any(k in text for k in ["减持", "获利了结", "降低仓位", "reduce"]):
      action = "decrease"
    elif any(k in text for k in ["卖出", "sell"]):
      action = "decrease" if has_position else "avoid"
    elif any(k in text for k in ["买入", "增持", "加仓", "buy", "add"]):
      action = "increase"

    rationale_parts: List[str] = []
    if recommendation_text:
      rationale_parts.append(recommendation_text.strip())
    if summary_text:
      rationale_parts.append(summary_text.strip())
    rationale = "\n".join(rationale_parts)[:600] if rationale_parts else None

    risk_level = doc.get("risk_level") or "中等"
    risk_note = f"报告风险等级：{risk_level}。请结合自身风险承受能力谨慎决策。"

    quantity_raw = pos.get("quantity")
    quantity = float(quantity_raw) if isinstance(quantity_raw, (int, float)) else 0.0
    mark_price = pos.get("mark_price")
    position_value_raw = pos.get("position_value")
    position_value = (
      float(position_value_raw)
      if isinstance(position_value_raw, (int, float))
      else None
    )
    quote: Optional[Dict[str, Optional[float]]] = _extract_quote_from_report_doc(doc)
    price: Optional[float] = None
    if isinstance(mark_price, (int, float)):
      price = float(mark_price)
    elif position_value is not None and quantity > 0:
      try:
        price = float(position_value) / quantity
      except Exception:
        price = None

    item = PortfolioRecommendationItem(
      ticker=symbol_str,
      name=stock_name,
      action=action,
      current_price=price,
      current_shares=quantity if has_position else None,
      current_value=position_value if has_position else None,
      quote_price=quote.get("price") if quote else None,
      quote_change=quote.get("change") if quote else None,
      quote_change_percent=quote.get("change_percent") if quote else None,
      quote_volume=quote.get("volume") if quote else None,
      target_allocation=None,
      suggested_shares=None,
      reason=rationale,
      risk=risk_note,
    )
    items.append(item)

  analysis = (
    "本次推荐基于当前 IBKR 持仓快照、可用资金与所选分析报告的摘要信息，由规则逻辑自动生成，"
    "仅提供定性操作方向提示，未对目标仓位比例及资金约束进行精确计算。"
    "所有内容仅用于个人记录和回顾，不构成任何形式的投资建议。"
  )

  payload = PortfolioRecommendationPayload(
    base_currency=base_currency,
    as_of_date=as_of_date
    if isinstance(as_of_date, str)
    else (as_of_date.strftime("%Y-%m-%d") if isinstance(as_of_date, datetime) else None),
    total_value=total_value,
    cash_before=cash_val,
    cash_after=cash_val,
    cash_allocation=None,
    cash_reason=None,
    analysis=analysis,
    sector_advice=None,
    items=items,
    used_model=None,
    mode="rule_fallback",
    price_missing_tickers=[],
  )
  return payload


def _build_payload_from_llm(
  snapshot: Dict[str, Any],
  reports: List[Dict[str, Any]],
  llm_result: Dict[str, Any],
  used_model_name: Optional[str] = None,
) -> PortfolioRecommendationPayload:
  positions = snapshot.get("positions", []) or []
  base_currency = snapshot.get("base_currency")
  cash = snapshot.get("cash")
  as_of_date = snapshot.get("as_of_date")

  # 计算允许出现在推荐结果中的股票代码集合：组合持仓 + 报告股票
  allowed_symbols = set()
  for p in positions:
    sym = p.get("symbol")
    if sym:
      allowed_symbols.add(str(sym))
  try:
    by_symbol = _ensure_unique_symbol_map(reports)
    for sym in by_symbol.keys():
      allowed_symbols.add(str(sym))
  except HTTPException:
    # 上游已做过校验；这里若异常，直接忽略，继续仅基于持仓构建 allowed set
    pass

  analysis = llm_result.get("analysis")
  sector_advice = llm_result.get("sector_advice")

  raw_suggestions = llm_result.get("suggestions") or []
  if not isinstance(raw_suggestions, list):
    logger.warning("LLM 输出中的 suggestions 字段不是列表，将回退规则版推荐。")
    raise ValueError("LLM suggestions 字段不合法")

  # 解析并过滤 suggestions
  parsed_suggestions: List[Dict[str, Any]] = []
  for item in raw_suggestions:
    if not isinstance(item, dict):
      continue

    ticker = item.get("ticker") or item.get("stock_symbol")
    if not ticker:
      continue
    ticker_str = str(ticker)
    if ticker_str not in allowed_symbols:
      logger.warning(
        "LLM 输出包含上下文中不存在的股票代码，将丢弃该条目: %s",
        ticker_str,
      )
      continue

    action_raw = item.get("action") or "hold"
    action = str(action_raw)
    target_alloc_raw = item.get("target_allocation") or item.get("target_position_percent")
    target_allocation: Optional[float]
    if isinstance(target_alloc_raw, (int, float)):
      target_allocation = float(target_alloc_raw)
    else:
      # 若未显式给出 target_allocation，且为清仓/退出类动作，则默认视为 0
      action_lower = action.lower()
      if any(k in action_lower for k in ["exit", "close", "清仓", "sell all"]):
        target_allocation = 0.0
      else:
        logger.warning(
          "LLM 建议缺少 target_allocation，将丢弃该条目: %s",
          item,
        )
        continue

    # 若为清仓/退出类动作，但 target_allocation 非 0，则强制修正为 0
    if isinstance(target_allocation, (int, float)):
      action_lower = action.lower()
      if any(k in action_lower for k in ["exit", "close", "清仓", "sell all"]) and target_allocation != 0.0:
        target_allocation = 0.0

    # 从报告中尝试获取参考价格（结构化字段或“当前价格：xxx”文本），以及其他行情信息
    report_price = None
    quote_info: Optional[Dict[str, Optional[float]]] = None
    doc = by_symbol.get(ticker_str)
    if doc:
      quote_info = _extract_quote_from_report_doc(doc) or {}
      report_price = quote_info.get("price")

    parsed_suggestions.append(
      {
        "ticker": ticker_str,
        "name": item.get("name") or item.get("stock_name"),
        "action": action,
        "target_allocation": target_allocation,
        "reason": item.get("reason") or item.get("rationale"),
        "risk": item.get("risk") or item.get("risk_note"),
        "price": report_price,
      }
    )

  if not parsed_suggestions:
    raise ValueError("LLM 未返回有效的股票建议条目")

  # 比例总和检查
  stock_alloc_sum = 0.0
  for s in parsed_suggestions:
    ta = s.get("target_allocation")
    if isinstance(ta, (int, float)) and ta > 0:
      stock_alloc_sum += float(ta)

  if stock_alloc_sum < -1e-6 or stock_alloc_sum > 1.05:
    raise ValueError(
      f"LLM 返回的股票目标仓位比例之和不合理: sum={stock_alloc_sum:.4f}，请重试或调整提示词。"
    )

  # 解析现金目标比例（优先使用 cash_allocation，其次兼容 cash_target_allocation）
  cash_allocation_raw = llm_result.get("cash_allocation")
  cash_target_raw = llm_result.get("cash_target_allocation")

  cash_allocation: Optional[float] = None
  if isinstance(cash_allocation_raw, (int, float)):
    cash_allocation = float(cash_allocation_raw)
  elif isinstance(cash_target_raw, (int, float)):
    cash_allocation = float(cash_target_raw)

  if isinstance(cash_allocation, (int, float)) and cash_allocation < 0:
    cash_allocation = None

  if isinstance(cash_allocation, (int, float)):
    if stock_alloc_sum > 1.0 - cash_allocation + 1e-6:
      raise ValueError(
        "LLM 返回的股票目标仓位之和与现金目标比例之和超过 1，请重试。"
      )

  # 若未显式给出现金比例，则自动推算
  final_cash_allocation: Optional[float]
  if isinstance(cash_allocation, (int, float)):
    final_cash_allocation = cash_allocation
  else:
    if 0.0 <= stock_alloc_sum <= 1.05:
      final_cash_allocation = max(0.0, 1.0 - stock_alloc_sum)
    else:
      final_cash_allocation = None

  cash_reason = llm_result.get("cash_reason")

  # 估算总资产
  total_position_value = 0.0
  for p in positions:
    v = p.get("position_value")
    if isinstance(v, (int, float)):
      total_position_value += float(v)
  cash_val = float(cash) if isinstance(cash, (int, float)) else 0.0
  total_value = total_position_value + cash_val

  trades_result = _compute_trades(
    total_value=total_value,
    cash=cash_val,
    positions=positions,
    suggestions=parsed_suggestions,
  )

  items: List[PortfolioRecommendationItem] = []
  for r in trades_result["stock_results"]:
    quote: Optional[Dict[str, Optional[float]]] = None
    ticker_val = r.get("ticker")
    if ticker_val is not None:
      ticker_str = str(ticker_val)
      doc = by_symbol.get(ticker_str)
      if doc:
        try:
          quote = _extract_quote_from_report_doc(doc)
        except Exception:
          quote = None

    item = PortfolioRecommendationItem(
      ticker=r["ticker"],
      name=r.get("name"),
      action=r.get("action") or "hold",
      current_price=r.get("current_price"),
      current_shares=r.get("current_shares"),
      current_value=r.get("current_value"),
      quote_price=quote.get("price") if quote else None,
      quote_change=quote.get("change") if quote else None,
      quote_change_percent=quote.get("change_percent") if quote else None,
      quote_volume=quote.get("volume") if quote else None,
      target_allocation=r.get("target_allocation"),
      suggested_shares=r.get("suggested_shares"),
      reason=r.get("reason"),
      risk=r.get("risk"),
    )
    items.append(item)

  price_missing_tickers = [
    str(r["ticker"])
    for r in trades_result["stock_results"]
    if r.get("price_missing")
  ]

  payload = PortfolioRecommendationPayload(
    base_currency=base_currency,
    as_of_date=as_of_date
    if isinstance(as_of_date, str)
    else (as_of_date.strftime("%Y-%m-%d") if isinstance(as_of_date, datetime) else None),
    total_value=trades_result["total_value"],
    cash_before=trades_result["cash_before"],
    cash_after=trades_result["cash_after"],
    cash_allocation=final_cash_allocation,
    cash_reason=str(cash_reason) if cash_reason is not None else None,
    analysis=str(analysis) if analysis is not None else None,
    sector_advice=str(sector_advice) if sector_advice is not None else None,
    items=items,
    used_model=used_model_name,
    mode="llm",
    price_missing_tickers=price_missing_tickers,
  )
  return payload


@router.post("/recommendations")
async def generate_portfolio_recommendations(
  body: PortfolioRecommendationRequest,
  current_user: dict = Depends(get_current_user),
):
  """
  基于 IBKR 持仓与已完成报告生成持仓推荐。
  """
  try:
    user_id = current_user.get("id")
    if not user_id:
      raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未获取到用户信息",
      )

    logger.info(
      "🧮 生成持仓推荐: user=%s, report_ids=%s",
      user_id,
      body.report_ids,
    )

    # region agent log
    try:
      with open(
        "/Users/wangyixiao/Desktop/Files/Projects/personal-stock-assistant/.cursor/debug-314d87.log",
        "a",
        encoding="utf-8",
      ) as f:
        f.write(
          json.dumps(
            {
              "sessionId": "314d87",
              "runId": "initial",
              "hypothesisId": "H1-H2-H4",
              "location": "app/routers/portfolio_recommendation.py:generate_portfolio_recommendations:body",
              "message": "generate_portfolio_recommendations body",
              "data": {
                "model_name": body.model_name,
                "report_ids_count": len(body.report_ids),
              },
              "timestamp": int(datetime.now().timestamp() * 1000),
            },
            ensure_ascii=False,
          )
          + "\n",
        )
    except Exception:
      pass
    # endregion agent log

    snapshot = await _get_latest_ibkr_snapshot(user_id)
    reports = await _load_reports(body.report_ids)

    # 优先尝试调用 LLM 生成推荐，失败时回退规则版逻辑
    llm_context = _build_llm_context(snapshot, reports)
    llm_result = await _call_portfolio_llm(
      llm_context,
      requested_model_name=body.model_name,
    )

    if llm_result:
      try:
        used_model_name = llm_result.pop("_used_model_name", None)
        payload = _build_payload_from_llm(snapshot, reports, llm_result, used_model_name)
      except InsufficientFundsError:
        # 资金不足等业务错误，直接抛出，由外层统一返回 400
        raise
      except Exception as e:
        logger.warning("LLM 输出解析失败或结果不合法，将回退规则版推荐: %s", e)
        payload = _build_recommendations_from_reports(snapshot, reports)
    else:
      payload = _build_recommendations_from_reports(snapshot, reports)

    return ok(payload.model_dump())
  except HTTPException:
    raise
  except ValueError as e:
    logger.warning("⚠️ 持仓推荐参数校验失败: %s", e)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
  except Exception as e:
    logger.exception("❌ 生成持仓推荐失败: %s", e)
    raise HTTPException(
      status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
      detail=f"生成持仓推荐失败: {str(e)}",
    )

