import logging
import json
import asyncio
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
  report_ids: List[str] = Field(..., description="分析报告ID列表，最多10个")
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
    if len(unique_ids) > 10:
      raise ValueError("最多只能选择 10 份报告")
    return unique_ids


class PortfolioRecommendationItem(BaseModel):
  stock_symbol: str
  stock_name: Optional[str] = None
  action: str
  target_position_percent: Optional[float] = None
  suggested_trade_shares: Optional[float] = None
  rationale: Optional[str] = None
  risk_note: Optional[str] = None


class PortfolioRecommendationPayload(BaseModel):
  base_currency: Optional[str] = None
  cash: Optional[float] = None
  as_of_date: Optional[str] = None
  positions: List[Dict[str, Any]] = []
  overall_comment: Optional[str] = None
  evaluation_summary: Optional[str] = None
  recommendations: List[PortfolioRecommendationItem] = []
   # 本次用于生成持仓推荐的模型名称（如果是规则兜底则为空）
  used_model: Optional[str] = None


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

  # 计算总市值（优先使用 summary.total_position_value，其次按持仓累加）
  total_value = None
  try:
    summary = snapshot.get("summary") or {}
    tv = summary.get("total_position_value")
    if isinstance(tv, (int, float)):
      total_value = float(tv)
  except Exception:
    total_value = None
  if total_value is None:
    acc = 0.0
    for p in positions:
      v = p.get("position_value")
      if isinstance(v, (int, float)):
        acc += float(v)
    total_value = acc if acc > 0 else None

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
      "total_position_value": total_value,
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
  """
  llm_config = _select_llm_config(requested_model_name=requested_model_name)

  provider_raw = (llm_config.provider or "custom_openai").lower()
  if provider_raw in OPENAI_COMPATIBLE_PROVIDERS:
    provider_for_adapter = provider_raw
  else:
    # 其它提供商统一走 custom_openai，由配置的 api_base + 环境变量密钥控制
    provider_for_adapter = "custom_openai"

  temperature = llm_config.temperature or 0.3
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
    "你是一名严谨的投资组合分析助手，只能基于给定的 JSON 上下文进行推理，"
    "不得引入任何外部行情、新闻或主观判断。你的职责是帮助用户从“记录与复盘”角度，"
    "整理当前 IBKR 持仓与若干股票分析报告所隐含的仓位结构与风险点，"
    "输出结构化的组合评价和逐股操作建议。所有结论仅供个人研究使用，不构成投资建议。\n\n"
    "上下文 JSON 主要包含两部分：\n"
    "1）portfolio：当前投资组合快照，包含 base_currency（基准货币）、cash（可用资金）、"
    "total_position_value（持仓总市值，如可用）以及 positions（逐笔持仓，含 symbol、quantity、position_value、avg_cost、unrealized_pnl 等字段）。\n"
    "2）reports：若干已完成的单股分析报告摘要。每一项包含：stock_symbol、stock_name、"
    "has_position（当前是否有持仓）、position（若有持仓则给出数量、市值、成本、浮盈亏等）、"
    "summary/recommendation/final_decision_excerpt 等文本要点。\n\n"
    "你需要在组合层面和个股层面进行有条理的总结，特别要区分：\n"
    "A）已有持仓的股票：在仓位调整、风险控制和资金使用效率上给出清晰的操作建议；\n"
    "B）当前无持仓但有报告的股票：评估是否值得建立新仓位，或明确指出应当暂不参与。\n"
    "在任何情况下，都必须遵守风险中性、表达克制的原则，不使用“稳赚”“必然”“保证”等极端措辞。"
  )

  schema_description = {
    "overall_comment": "字符串，对当前组合结构、集中度、现金水平及主要风险点的整体评价，可为空",
    "evaluation_summary": "字符串，从资金使用效率、风险暴露和执行节奏角度进行的补充评估，可为空",
    "items": [
      {
        "stock_symbol": "字符串，股票代码，必须是 portfolio.positions 或 reports 中出现过的代码，不得虚构",
        "stock_name": "字符串，股票名称，可选",
        "action": "字符串，组合层面的操作指令，必须是 increase/decrease/exit/hold/avoid 之一",
        "suggested_trade_shares": "数字，建议买入或卖出的股数，正数表示绝对数量，可选；对无持仓且建议建仓的标的，建议给出正数；如无明确数量则留空",
        "target_position_percent": "数字，目标在整体组合中的仓位比例 (0-1 之间)，可选；仅在能够给出相对合理目标仓位时填写，否则留空",
        "reason": "字符串，对该标的操作建议的核心依据和逻辑说明（结合报告要点与当前持仓情况），可选",
        "risk_note": "字符串，与该操作相关的主要风险提示（包括估值、流动性、单一标的集中度、宏观/政策等），可选",
      }
    ],
  }

  user_prompt = (
    "下面是当前 IBKR 投资组合快照以及若干已完成股票分析报告的 JSON 上下文。"
    "请基于该上下文，完成以下任务：\n"
    "1）在 overall_comment 中，用专业而克制的语言，概括当前组合的总体特征（如集中度、风险暴露方向、现金水平等）及需要关注的风险点；"
    "整体长度不少于 200 字，并至少覆盖以下要点：\n"
    "   - 组合前几大持仓及其合计权重，对整体波动和集中度的影响；\n"
    "   - 按主要市场/币种和行业的大致风险暴露（例如美股科技、港股互联网、A股白酒等）；\n"
    "   - 当前现金比例及潜在流动性风险；\n"
    "   - 给出 2–3 条“组合层面”的调整方向建议，但须明确仅为复盘参考，不构成投资建议。\n"
    "2）在 evaluation_summary 中（可选），从资金使用效率、风险收益匹配度、交易节奏等角度做策略性评估；"
    "如输出，应分为 2–3 个自然段，每段围绕一个清晰小主题给出 2–4 句建议，同样避免绝对化表述；\n"
    "3）在 items 列表中，针对每一只在 reports 中出现过的股票给出操作建议：\n"
    "   - 对 has_position 为 true 的股票，可以使用 increase/decrease/exit/hold，必要时给出 suggested_trade_shares 或 target_position_percent；\n"
    "   - 对 has_position 为 false 的股票，必须在 increase（建议建立或加大仓位）与 avoid（暂不参与）之间做出判断，并尽量给出一个保守的建议仓位或股数；\n"
    "   - 所有 stock_symbol 必须来自组合持仓或报告列表，不得出现上下文之外的股票代码。\n\n"
    "请严格按照上面给出的 JSON Schema 输出一个 JSON 对象，不要包含任何额外说明文字、自然语言段落或 Markdown，只能输出 JSON。\n\n"
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


def _build_recommendations_from_reports(
  snapshot: Dict[str, Any],
  reports: List[Dict[str, Any]],
) -> PortfolioRecommendationPayload:
  positions = snapshot.get("positions", [])
  base_currency = snapshot.get("base_currency")
  cash = snapshot.get("cash")
  as_of_date = snapshot.get("as_of_date")

  by_symbol = _ensure_unique_symbol_map(reports)

  position_map: Dict[str, Dict[str, Any]] = {}
  for p in positions:
    sym = p.get("symbol")
    if sym:
      position_map[sym] = p

  recommendations: List[PortfolioRecommendationItem] = []

  for symbol, doc in by_symbol.items():
    stock_name = doc.get("stock_name") or symbol
    recommendation_text = str(doc.get("recommendation") or "")
    summary_text = str(doc.get("summary") or "")
    key_points = doc.get("key_points") or []
    key_points_text = "\n".join([str(k) for k in key_points if k])[:400]

    text = f"{recommendation_text}\n{summary_text}\n{key_points_text}".lower()

    has_position = symbol in position_map and bool(
      position_map[symbol].get("quantity"),
    )

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

    item = PortfolioRecommendationItem(
      stock_symbol=symbol,
      stock_name=stock_name,
      action=action,
      target_position_percent=None,
      suggested_trade_shares=None,
      rationale=rationale,
      risk_note=risk_note,
    )
    recommendations.append(item)

  overall_comment = (
    "本次推荐基于当前 IBKR 持仓快照、可用资金与所选分析报告的摘要信息自动生成，"
    "仅用于个人记录和回顾，不构成任何形式的投资建议。"
  )

  payload = PortfolioRecommendationPayload(
    base_currency=base_currency,
    cash=cash,
    as_of_date=as_of_date
    if isinstance(as_of_date, str)
    else (as_of_date.strftime("%Y-%m-%d") if isinstance(as_of_date, datetime) else None),
    positions=positions,
    overall_comment=overall_comment,
    evaluation_summary=None,
    recommendations=recommendations,
    used_model=None,
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

  overall_comment = llm_result.get("overall_comment")
  evaluation_summary = llm_result.get("evaluation_summary")

  raw_items = llm_result.get("items") or []
  if not isinstance(raw_items, list):
    logger.warning("LLM 输出中的 items 字段不是列表，将回退规则版推荐。")
    raise ValueError("LLM items 字段不合法")

  recommendations: List[PortfolioRecommendationItem] = []
  for item in raw_items:
    if not isinstance(item, dict):
      continue
    symbol = item.get("stock_symbol")
    action = item.get("action")
    if not symbol or not action:
      continue

    # 过滤掉上下文中不存在的股票代码，避免模型胡写标的
    symbol_str = str(symbol)
    if symbol_str not in allowed_symbols:
      logger.warning(
        "LLM 输出包含上下文中不存在的股票代码，将丢弃该条目: %s",
        symbol_str,
      )
      continue

    stock_name = item.get("stock_name")
    target_position_percent = item.get("target_position_percent")
    suggested_trade_shares = item.get("suggested_trade_shares")
    reason = item.get("reason") or item.get("rationale")
    risk_note = item.get("risk_note")

    try:
      rec = PortfolioRecommendationItem(
        stock_symbol=str(symbol),
        stock_name=str(stock_name) if stock_name is not None else None,
        action=str(action),
        target_position_percent=float(target_position_percent)
        if isinstance(target_position_percent, (int, float))
        else None,
        suggested_trade_shares=float(suggested_trade_shares)
        if isinstance(suggested_trade_shares, (int, float))
        else None,
        rationale=str(reason) if reason is not None else None,
        risk_note=str(risk_note) if risk_note is not None else None,
      )
      recommendations.append(rec)
    except Exception as e:
      logger.warning("解析 LLM 推荐条目失败，将跳过该条目: %s; 错误: %s", item, e)

  if not recommendations:
    raise ValueError("LLM 未返回有效的推荐条目")

  payload = PortfolioRecommendationPayload(
    base_currency=base_currency,
    cash=cash,
    as_of_date=as_of_date
    if isinstance(as_of_date, str)
    else (as_of_date.strftime("%Y-%m-%d") if isinstance(as_of_date, datetime) else None),
    positions=positions,
    overall_comment=overall_comment,
    evaluation_summary=evaluation_summary,
    recommendations=recommendations,
    used_model=used_model_name,
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
      except Exception as e:
        logger.warning("LLM 输出解析失败，将回退规则版推荐: %s", e)
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

