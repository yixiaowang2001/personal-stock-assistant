#!/usr/bin/env python3
"""
测试 IBKR 历史成交接口 /api/ibkr/trades：
- 校验在给定筛选条件下，realized_pnl_total 等于所有卖出成交 realized_pnl 之和。
"""
import os
import sys
from typing import Any, Dict, List

import asyncio
import pytest
from httpx import AsyncClient

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.main import app  # noqa: E402
from app.core.database import get_mongo_db  # noqa: E402


@pytest.mark.asyncio
async def test_list_trades_realized_pnl_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    构造若干条成交记录，验证 realized_pnl_total 只统计有效卖出成交的已实现盈亏之和。
    """

    class FakeCollection:
        def __init__(self, docs: List[Dict[str, Any]]) -> None:
            self._docs = docs

        async def count_documents(self, query: Dict[str, Any]) -> int:
            return len(self._filter(query))

        def find(self, query: Dict[str, Any], projection: Dict[str, int]):
            items = [self._project(doc, projection) for doc in self._filter(query)]

            class Cursor:
                def __init__(self, items: List[Dict[str, Any]]) -> None:
                    self._items = items

                def sort(self, *_args, **_kwargs):
                    return self

                def skip(self, _n: int):
                    return self

                def limit(self, _n: int):
                    return self

                async def to_list(self, _length: Any):
                    return list(self._items)

            return Cursor(items)

        def aggregate(self, pipeline):
            match_stage = next((p for p in pipeline if "$match" in p), {"$match": {}})
            match_query = match_stage["$match"]
            matched = self._filter(match_query)
            total = sum(d.get("realized_pnl", 0) or 0 for d in matched)
            docs = [{"_id": None, "total": total}] if matched else []

            class AggCursor:
                def __init__(self, docs: List[Dict[str, Any]]) -> None:
                    self._docs = docs

                async def to_list(self, _length: Any):
                    return list(self._docs)

            return AggCursor(docs)

        def _project(self, doc: Dict[str, Any], projection: Dict[str, int]) -> Dict[str, Any]:
            if not projection:
                return dict(doc)
            result: Dict[str, Any] = {}
            for key, include in projection.items():
                if include and key in doc:
                    result[key] = doc[key]
            return result

        def _filter(self, query: Dict[str, Any]) -> List[Dict[str, Any]]:
            def match(doc: Dict[str, Any], q: Dict[str, Any]) -> bool:
                for k, v in q.items():
                    if k == "$or":
                        if not any(match(doc, sub) for sub in v):
                            return False
                    elif isinstance(v, dict):
                        if "$ne" in v:
                            if doc.get(k) == v["$ne"]:
                                return False
                        if "$gte" in v:
                            if doc.get(k) is None or doc.get(k) < v["$gte"]:
                                return False
                        if "$lte" in v:
                            if doc.get(k) is None or doc.get(k) > v["$lte"]:
                                return False
                        if "$exists" in v:
                            exists = k in doc
                            if v["$exists"] and not exists:
                                return False
                        if "$nin" in v:
                            if doc.get(k) in v["$nin"]:
                                return False
                    else:
                        if doc.get(k) != v:
                            return False
                return True

            return [d for d in self._docs if match(d, query)]

    class FakeDB:
        def __init__(self, trades: List[Dict[str, Any]]) -> None:
            self._coll = FakeCollection(trades)

        def __getitem__(self, name: str):
            if name == "ibkr_trades":
                return self._coll
            return self._coll

    user_id = "test-user"
    trades = [
        # 买入，不计入 realized_pnl_total
        {
            "user_id": user_id,
            "symbol": "AAPL",
            "side": "BUY",
            "trade_date": "2026-02-18",
            "quantity": 1,
            "price": 263.85,
            "amount": 263.85,
            "realized_pnl": None,
            "created_at": "2026-02-24T00:00:00",
        },
        # 卖出，有 realized_pnl，计入合计
        {
            "user_id": user_id,
            "symbol": "AAPL",
            "side": "SELL",
            "trade_date": "2026-02-24",
            "quantity": -1,
            "price": 270.46,
            "amount": -270.46,
            "realized_pnl": 6.61,
            "created_at": "2026-02-24T00:00:01",
        },
        # 卖出但 amount 为 0，视为未成交，不应计入接口 nor 合计
        {
            "user_id": user_id,
            "symbol": "AAPL",
            "side": "SELL",
            "trade_date": "2026-02-24",
            "quantity": -1,
            "price": 264.85,
            "amount": 0,
            "realized_pnl": 1.23,
            "created_at": "2026-02-24T00:00:02",
        },
    ]

    async def fake_get_mongo_db():
        return FakeDB(trades)

    def fake_get_current_user():
        return {"id": user_id}

    # 覆盖依赖
    from app.routers import ibkr as ibkr_router  # noqa: E402

    app.dependency_overrides[get_mongo_db] = fake_get_mongo_db
    app.dependency_overrides[ibkr_router.get_current_user] = fake_get_current_user

    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.get("/api/ibkr/trades")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["success"] is True
    data = payload["data"]

    # 列表中应包含 2 条记录（BUY + 有成交的 SELL），数量为正数
    assert data["total"] == 2
    assert len(data["trades"]) == 2
    quantities = {t["side"]: t["quantity"] for t in data["trades"]}
    assert quantities["BUY"] == 1
    assert quantities["SELL"] == 1

    # realized_pnl_total 只统计有效卖出成交的 realized_pnl
    assert pytest.approx(data["realized_pnl_total"], rel=1e-6) == 6.61

