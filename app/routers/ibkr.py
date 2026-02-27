"""
IBKR 持仓分析 API：从 Flex Web Service 拉取持仓快照并入库，供前端展示。
"""
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.database import get_mongo_db
from app.core.response import ok
from app.routers.auth_db import get_current_user
from app.services.ibkr_flex_service import fetch_ibkr_positions_snapshot

router = APIRouter(prefix="/ibkr", tags=["IBKR持仓"])
logger = logging.getLogger("webapi")

COLLECTION = "ibkr_positions"


@router.get("/positions/latest")
async def get_latest_positions(
    current_user: dict = Depends(get_current_user),
):
    """
    获取当前用户最近一次 IBKR 持仓快照。
    若无快照则返回空列表及提示信息。
    """
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未获取到用户信息")

    db = get_mongo_db()
    doc = await db[COLLECTION].find_one(
        {"user_id": user_id},
        sort=[("as_of_date", -1), ("created_at", -1)],
        projection={"_id": 0},
    )

    if not doc:
        return ok(
            data={
                "as_of_date": None,
                "base_currency": None,
                "summary": None,
                "positions": [],
                "message": "尚未从 IBKR 同步持仓，请点击刷新获取。",
            },
            message="ok",
        )

    payload = {
        "as_of_date": doc.get("as_of_date"),
        "base_currency": doc.get("base_currency"),
        "summary": doc.get("summary"),
        "positions": doc.get("positions", []),
    }
    return ok(data=payload)


@router.post("/positions/refresh")
async def refresh_positions(
    current_user: dict = Depends(get_current_user),
):
    """
    向 IBKR Flex 请求最新报表，解析持仓后写入数据库并返回最新快照。
    需在 .env 中配置 IBKR_FLEX_TOKEN 与 IBKR_FLEX_QUERY_ID。
    """
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未获取到用户信息")

    try:
        snapshot, trades = await asyncio.to_thread(fetch_ibkr_positions_snapshot)
    except RuntimeError as e:
        msg = str(e)
        logger.warning("IBKR 刷新失败（配置或权限）: %s", msg)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=msg,
        )
    except TimeoutError as e:
        logger.warning("IBKR 报表拉取超时: %s", e)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="IBKR 报表生成超时，请稍后重试。",
        )
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout) as e:
        logger.warning("IBKR 连接超时: %s", e)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="连接 IBKR 超时。若在国内或受限网络，请在 .env 中配置 HTTPS_PROXY 后重启后端再试。",
        )
    except requests.exceptions.RequestException as e:
        logger.exception("IBKR 请求异常: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"访问 IBKR 失败: {str(e)}。若需代理，请在 .env 中配置 HTTPS_PROXY。",
        )
    except Exception as e:
        logger.exception("IBKR 刷新异常: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"拉取 IBKR 持仓失败: {str(e)}",
        )

    now = datetime.utcnow().isoformat()
    doc: Dict[str, Any] = {
        "user_id": user_id,
        "as_of_date": snapshot.get("as_of_date"),
        "base_currency": snapshot.get("base_currency"),
        "positions": snapshot.get("positions", []),
        "summary": snapshot.get("summary"),
        "created_at": now,
        "updated_at": now,
    }
    db = get_mongo_db()
    await db[COLLECTION].insert_one(doc)

    # 交易记录入库：使用幂等 upsert 避免重复
    if trades:
        trades_coll = db["ibkr_trades"]
        # 基本索引：按用户与交易日期倒序查询
        await trades_coll.create_index([("user_id", 1), ("trade_date", -1)])
        for t in trades:
            # 若解析不到股票代码或方向，则跳过
            symbol = t.get("symbol")
            side = t.get("side")
            trade_date = t.get("trade_date")
            if not symbol or not side or not trade_date:
                continue

            filter_doc = {
                "user_id": user_id,
                "symbol": symbol,
                "side": side,
                "trade_date": trade_date,
                "quantity": t.get("quantity"),
                "price": t.get("price"),
            }
            trade_doc: Dict[str, Any] = {
                **t,
                "user_id": user_id,
                "created_at": now,
                "source": "ibkr_flex",
            }
            await trades_coll.update_one(filter_doc, {"$setOnInsert": trade_doc}, upsert=True)

    payload = {
        "as_of_date": doc["as_of_date"],
        "base_currency": doc["base_currency"],
        "summary": doc["summary"],
        "positions": doc["positions"],
    }
    return ok(data=payload, message="已从 IBKR 同步最新持仓。")


@router.get("/trades")
async def list_trades(
    symbol: str | None = Query(default=None, description="可选，按股票代码过滤"),
    start_date: str | None = Query(default=None, description="可选，起始日期，格式 YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="可选，结束日期，格式 YYYY-MM-DD"),
    limit: int = Query(default=50, ge=1, le=200, description="返回条数上限"),
    offset: int = Query(default=0, ge=0, description="偏移量，用于分页"),
    current_user: dict = Depends(get_current_user),
):
    """
    查询 IBKR 历史成交记录。
    默认按 trade_date 倒序返回当前用户最近的成交。
    """
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未获取到用户信息")

    db = get_mongo_db()
    coll = db["ibkr_trades"]

    query: Dict[str, Any] = {"user_id": user_id}
    if symbol:
        query["symbol"] = symbol

    date_filter: Dict[str, Any] = {}
    if start_date:
        date_filter["$gte"] = start_date
    if end_date:
        date_filter["$lte"] = end_date
    # 始终过滤掉 Flex 中的跨期汇总行（trade_date == "MULTI"）
    if date_filter:
        date_filter["$ne"] = "MULTI"
        query["trade_date"] = date_filter
    else:
        query["trade_date"] = {"$ne": "MULTI"}

    # 只返回已成交：排除「卖出且 amount 为空或 0」的未成交记录（含历史误入库的）
    query["$or"] = [
        {"side": {"$ne": "SELL"}},
        {"amount": {"$exists": True, "$nin": [None, 0]}},
    ]

    total = await coll.count_documents(query)
    cursor = (
        coll.find(query, {"_id": 0})
        .sort([("trade_date", -1), ("created_at", -1)])
        .skip(offset)
        .limit(limit)
    )
    items = await cursor.to_list(None)

    # 为前端展示统一处理数量为正数，方向由 side 字段表达
    for item in items:
        q = item.get("quantity")
        if isinstance(q, (int, float)):
            item["quantity"] = abs(q)

    return ok(
        data={
            "trades": items,
            "total": total,
        }
    )
