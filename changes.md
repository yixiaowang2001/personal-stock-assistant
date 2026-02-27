# 变更记录 (Changes)

本文档记录对项目代码的修改，便于回溯与协作。

---

### Alpha Vantage 连接测试改用免费接口

**文件**: `app/services/config_service.py`

**原因**: 原测试使用 `TIME_SERIES_INTRADAY`（5 分钟日内）接口，该接口在 Alpha Vantage 属于 **Premium 付费接口**。免费 API Key 调用后只会返回 “This is a premium endpoint” 的提示，无法得到行情数据，导致前台“测试连接”始终显示失败（HTTP 200 但业务判定失败）。

**改动**:

1. 将测试请求的 `function` 从 `TIME_SERIES_INTRADAY` 改为 **`GLOBAL_QUOTE`**（免费接口）。
2. 移除参数 `interval`（仅 INTRADAY 需要）。
3. 将成功判定条件从 `"Time Series (5min)" in data or "Meta Data" in data` 改为 **`"Global Quote" in data`**。
4. 在注释中说明使用 GLOBAL_QUOTE 的原因（避免免费用户误判为配置失败）。

**影响**: 使用免费 Alpha Vantage API Key 的用户在前台点击“测试”时，可以正常通过连接测试。付费用户不受影响。

**是否需要重启**: 需要。修改的是 Python 后端逻辑，需重启后端服务（如 `python -m app` 或 uvicorn）后新逻辑才会生效。

### 增加yfinance数据源

### 新增 IBKR 持仓分析：历史操作与可用资金展示

**文件**:

- 后端:
  - `app/services/ibkr_flex_service.py`
  - `app/routers/ibkr.py`
- 前端:
  - `frontend/src/api/ibkr.ts`
  - `frontend/src/views/Positions/index.vue`

**原因**:

- 之前“持仓分析”页面只能看到当前仓位，无法基于 IBKR Flex 报表还原历史买卖操作。
- IBKR Flex 报表中 Cash Report 段已包含期末现金与期末结算现金，但后端未解析、前端也无法展示“可用资金”，用户只能到 IBKR 客户端查。
- 部分 Flex 汇总行（如 `SYMBOL_SUMMARY`、`CLOSED_LOT`）和跨期行（`TradeDate = MULTI`）混入历史记录列表，容易干扰阅读。

**改动**:

1. **IBKR Flex 报表解析扩展**（后端）
   - 在 `ibkr_flex_service.py` 中新增 `parse_trades_from_csv(csv_text)`：
     - 第二次扫描 Flex CSV，识别 Trades 段表头（包含 `AssetClass, Symbol, Quantity, TradePrice, TradeMoney, Buy/Sell` 等列）。
     - 解析为统一的交易对象：`symbol, description, asset_class, currency_primary, side(BUY/SELL), quantity, price, amount, trade_date, report_date, exchange`。
     - 过滤掉数量为 0 的记录，以及 `LevelOfDetail` 为 `ASSET_SUMMARY`、`SYMBOL_SUMMARY`、`CLOSED_LOT` 等汇总/批次行，只保留真实成交明细。
   - 扩展 `parse_positions_from_csv`：
     - 第二遍扫描 CSV，识别 Cash Report 段表头（`CurrencyPrimary, EndingCash, EndingSettledCash`）。
     - 对基准货币行汇总 `EndingCash`、`EndingSettledCash`，注入到 `snapshot["summary"].ending_cash` / `ending_settled_cash`。
   - 调整 `fetch_ibkr_positions_snapshot()`：
     - 从 Flex Web Service 获取报表后，同时调用 `parse_positions_from_csv` 和 `parse_trades_from_csv`，返回 `(snapshot, trades)`。

2. **IBKR 历史成交入库与查询 API**（后端）
   - 在 `ibkr.py` 的 `/api/ibkr/positions/refresh` 路由中：
     - 接收 `snapshot, trades`，仍按原逻辑写入 `ibkr_positions` 集合。
     - 新增 `ibkr_trades` 集合，入库字段包括：`user_id, symbol, side, trade_date, quantity, price, amount, realized_pnl, currency_primary, report_date, exchange, created_at, source`。
     - 使用 `(user_id, symbol, side, trade_date, quantity, price)` 作为幂等键，通过 `update_one(..., upsert=True)` 避免刷新多次插入重复成交。
     - 为 `ibkr_trades` 创建索引 `("user_id", 1), ("trade_date", -1)`，加速按用户与时间倒序查询。
   - 新增 `GET /api/ibkr/trades` 接口：
     - 支持参数：`symbol?`, `start_date?`, `end_date?`, `limit`, `offset`。
     - 默认按 `trade_date` 倒序返回当前用户最近成交。
     - 无论是否按日期过滤，都会强制排除 `trade_date == "MULTI"` 的跨期汇总记录。
     - 返回结构统一为：`{ trades: [...], total: number }`，其中 `quantity` 字段已统一为绝对值，方向由 `side` 字段表示。

3. **前端 API 封装与类型定义**（前端）
   - 在 `frontend/src/api/ibkr.ts` 中：
     - 新增 `IbkrTrade` 类型：`trade_date, symbol, description, asset_class, currency_primary, side, quantity, price, amount, realized_pnl, report_date, exchange`。
     - 在 `IbkrSummary` 中加入可选字段：`ending_cash`, `ending_settled_cash`。
     - 新增 `ibkrApi.getTrades(params)`，封装 `GET /api/ibkr/trades`，返回 `{ trades, total }`。

4. **持仓页 UI：可用资金 & 历史操作卡片**（前端）
   - 在 `frontend/src/views/Positions/index.vue`：
     - 在“持仓摘要”中新增一行“可用资金（期末结算现金）”：
       - 计算规则：优先使用 `summary.ending_settled_cash`，若为空则使用 `summary.ending_cash`，均为空则显示 `-`。
       - 金额前加上基准货币符号（USD: `$`，HKD: `HK$`，CNY: `¥`）。
     - 在“持仓列表”卡片下方新增“历史操作”卡片：
       - 顶部提供一个按股票代码筛选的输入框与“查询”按钮。
       - 表格列包括：日期（`trade_date`）、代码（可点击跳转个股详情）、名称、方向（买入/卖出颜色区分）、数量、价格、金额。
       - 使用分页控件 `el-pagination` 支持翻页（默认每页 20 条）。
       - 页面加载时，`onMounted` 同时调用 `fetchLatest()`（持仓快照）和 `fetchTrades()`（历史操作）。

5. **移除调试用 CSV 落盘逻辑**
   - 临时为调试 Flex 报表结构，在 `fetch_ibkr_positions_snapshot()` 中曾将原始报表写入项目根目录 `ibkr_flex_statement.csv`。
   - 根据安全与隐私考虑，现已删除相关写盘代码，后端仅在内存中解析 Flex 报表，不再在文件系统中暴露原始 CSV。

**影响**:

- 前端“持仓分析”页现在可以：
  - 在顶部摘要中看到基于 IBKR 报表的“可用资金（期末结算现金）”，与 IBKR 客户端保持一致。
  - 在新的“历史操作”卡片中查看按时间排序的真实买入/卖出记录，并按代码筛选，排除了 Flex 报表中的各种汇总行和 `MULTI` 跨期行。
- 后端在每次成功刷新 IBKR 持仓时，会自动将最新的成交记录写入 `ibkr_trades` 集合，供后续查询使用。

**是否需要重启**:

- 需要。改动包含后端 Python 逻辑与路由，需要重启 FastAPI/uvicorn。
- 前端修改需重新构建或重新启动前端开发服务器后生效。