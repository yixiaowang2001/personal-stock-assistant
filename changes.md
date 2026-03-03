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

### IBKR 历史操作：卖出盈亏与已实现盈亏汇总

**文件**:

- 后端:
  - `app/services/ibkr_flex_service.py`
  - `app/routers/ibkr.py`
- 前端:
  - `frontend/src/api/ibkr.ts`
  - `frontend/src/views/Positions/index.vue`
- 测试:
  - `tests/test_ibkr_trades.py`
  - `tests/test_ibkr_trades_endpoint.py`

**原因**:

- 部分 IBKR Flex 报表的 Trades 段不包含 Realized P&L 列，之前实现无法直接从 CSV 中读取单笔卖出的已实现盈亏，导致前端“卖出盈亏”列为空。
- 在持仓分析页的“历史操作”卡片中，希望在表格最后一列展示每笔有效卖出的盈亏，同时在卡片头部展示当前筛选条件下所有卖出成交的已实现盈亏合计，便于整体回顾已实现收益情况。

**改动**:

1. **成交解析：缺失 Realized P&L 列时使用 FIFO 规则回填卖出盈亏**（后端）
   - 在 `parse_trades_from_csv` 内部新增 `_backfill_realized_pnl_fifo(trades)`：
     - 以 `(symbol, currency_primary)` 为键维护 FIFO 买入仓位队列。
     - 对于 `BUY` 且数量为正的成交，将 `qty, price` 作为一笔仓位入队。
     - 对于 `SELL` 且数量为负的成交，按照 FIFO 从历史买入仓位中逐笔匹配数量，使用公式 `(卖出价 - 买入价) × 配对数量` 计算单笔卖出成交的 `realized_pnl`，并在原报表未提供该字段时进行补全。
   - 若 Flex 报表本身已经提供 Realized P&L 列，则优先使用报表中的数值，不覆盖原始数据。

2. **IBKR 历史成交入库：支持刷新后更新 realized_pnl**（后端）
   - 在 `app/routers/ibkr.py` 的 `/api/ibkr/positions/refresh` 中：
     - 调整对 `ibkr_trades` 集合的 upsert 行为：使用
       - `$set` 更新解析得到的字段（包括可能新补全的 `realized_pnl` 等），
       - `$setOnInsert` 仅在首次插入时写入 `created_at`。
     - 这样在 Flex 报表结构调整或我们补充计算逻辑后，多次刷新可以为既有成交记录补上或纠正 `realized_pnl`，而不会重复插入。

3. **IBKR 历史成交查询：返回已实现盈亏总和**（后端）
   - 在 `GET /api/ibkr/trades` (`list_trades`) 中：
     - 保持原有按 `user_id`、`symbol`、日期区间、排除 `trade_date == "MULTI"` 以及“只返回已成交记录”的查询逻辑不变。
     - 新增一段聚合管道，基于相同筛选条件进一步过滤 `side == 'SELL'` 且 `amount` 非空、非 0、`realized_pnl` 存在的记录，使用 `$group` 求和 `realized_pnl`，得到当前筛选条件下所有有效卖出成交的已实现盈亏合计 `realized_pnl_total`。
     - 在接口响应中扩展返回结构为：`{ trades, total, realized_pnl_total }`，原有 `trades` 与 `total` 字段保持兼容。

4. **前端 API 封装与类型更新**（前端）
   - 在 `frontend/src/api/ibkr.ts` 中：
     - 将 `ibkrApi.getTrades` 的返回类型更新为 `ApiClient.get<{ trades: IbkrTrade[]; total: number; realized_pnl_total?: number }>`，保留现有 `IbkrTrade` 类型不变（其中已包含行级 `realized_pnl` 字段）。

5. **持仓分析页 UI：卖出盈亏列与“已实现盈亏”汇总展示**（前端）
   - 在 `frontend/src/views/Positions/index.vue` 的“历史操作”卡片中：
     - 已有的“卖出盈亏”列改为严格基于 `row.side === 'SELL' && row.realized_pnl != null` 显示数值，金额采用统一的 `currencySymbol + formatAmount(realized_pnl)`，并根据正负值使用绿色或红色显示。
     - 新增响应式变量 `realizedPnlTotal`，在 `fetchTrades()` 中从接口的 `res.data.realized_pnl_total` 读取并存储（无值时回退为 `0`）。
     - 在卡片头部标题“历史操作（共 X 笔）”右侧新增“已实现盈亏：<金额>”展示：
       - 使用同样的金额格式与配色规则，代表当前筛选条件（代码 + 日期）的所有 SELL 成交的已实现盈亏合计。

6. **测试覆盖**（后端）
   - 在 `tests/test_ibkr_trades.py` 中：
     - 新增场景 `test_parse_trades_from_csv_without_realized_pnl_column_fifo_backfill`：
       - 构造不含 Realized P&L 列的最小 Trades CSV，仅包含 `BUY 1 @ 263.85` 与 `SELL -1 @ 270.46`，验证解析结果中卖出行的 `realized_pnl` 为 `(270.46 - 263.85) * 1 = 6.61`，而买入行仍为 `None`。
   - 新增 `tests/test_ibkr_trades_endpoint.py`：
     - 使用伪造的 `ibkr_trades` 集合与当前用户依赖，直接请求 `/api/ibkr/trades`：
       - 构造若干 BUY / SELL 记录，其中只有部分 SELL 记录有 `realized_pnl` 且 `amount != 0`。
       - 验证响应中的 `trades` 条数与数量正负处理符合预期，且 `realized_pnl_total` 等于所有有效卖出成交 `realized_pnl` 的合计。

**影响**:

- 对于原本 Flex 报表 Trades 段不含 Realized P&L 列的账户，刷新 IBKR 持仓后，现在会自动按 FIFO 规则计算并补全每笔卖出成交的已实现盈亏，前端“卖出盈亏”列不再为空。
- 在“持仓分析”页的“历史操作”卡片中，用户不仅可以看到每一笔卖出的盈亏，还可以在卡片头部直观看到当前筛选条件下的“已实现盈亏”总额，便于评估已锁定收益或亏损。
- 旧版前端若仅依赖 `trades` 与 `total` 字段，不会受到接口扩展的影响；新版前端会额外利用 `realized_pnl_total` 提供更丰富的汇总信息。

**是否需要重启**:

- 需要。包含后端 Python 解析逻辑与路由的调整，需要重启 FastAPI/uvicorn 后新逻辑才会生效。
- 前端变更需要重新构建或重启前端开发服务器，浏览器需刷新后才能看到新的“卖出盈亏”和“已实现盈亏”展示。

---

### 持仓推荐：支持模型选择与更专业的组合建议 Prompt

**文件**:

- 后端:
  - `app/routers/portfolio_recommendation.py`
- 前端:
  - `frontend/src/api/portfolio.ts`
  - `frontend/src/views/Positions/index.vue`

**原因**:

- 持仓推荐功能原本固定使用统一的大模型配置，无法像单股分析一样按需切换模型。
- 对“当前无持仓但有报告的股票”的处理依赖规则兜底，Prompt 描述不够清晰，LLM 输出在买入/回避判断和说明的专业度、细致程度上略显不足。
- 前端“组合层面说明”和“模型综合评估”的展示采用提示框样式，文案命名也不够贴近实际含义。

**改动**:

1. **后端：模型选择与使用模型记录**
   - 在 `PortfolioRecommendationRequest` 中新增可选字段 `model_name`，允许前端在调用 `/api/portfolio/recommendations` 时指定本次推荐使用的 `model_name`。
   - 将 `_select_llm_config()` 改为 `_select_llm_config(requested_model_name: Optional[str])`：
     - 若传入 `requested_model_name`，在已启用的 LLM 配置中按 `model_name` 精确匹配；若未找到则回退到系统默认 quick 模型设置；再不行则使用第一个启用模型。
   - `_call_portfolio_llm` 增加 `requested_model_name` 参数，内部用 `_select_llm_config(requested_model_name=...)` 选择模型，并在解析后的 JSON 结果中注入 `_used_model_name` 字段以记录本次实际使用的模型。
   - `PortfolioRecommendationPayload` 新增字段 `used_model: Optional[str]`，用于向前端返回“本次持仓推荐使用的模型名称”。
   - 在路由 `generate_portfolio_recommendations` 中：
     - 调用 `_call_portfolio_llm(llm_context, requested_model_name=body.model_name)`。
     - 若 LLM 返回成功，从结果中 `pop("_used_model_name")`，并将其传入 `_build_payload_from_llm(..., used_model_name)`；若回退到规则兜底逻辑，则 `used_model` 为 `None`。

2. **后端：Prompt 专业化与输出约束增强**
   - 在 `system_prompt` 中重新描述角色与上下文：
     - 明确模型只基于 IBKR 持仓快照与若干报告 JSON 上下文进行推理，不得引入外部行情或主观判断。
     - 强调需要同时从组合层面和个股层面给出结构化建议，区分“已持仓股票”和“无持仓但有报告的股票”。
     - 要求语言专业、克制，避免使用“稳赚/必然/保证”等绝对化表达。
   - 在 `user_prompt` 中对输出结构和长度给出更严格约束：
     - `overall_comment`：要求不少于约 200 字，需覆盖前几大持仓及集中度、按市场/行业的风险暴露、现金比例与流动性风险，并给出 2–3 条组合层面的调整方向建议（明确说明仅为复盘参考）。
     - `evaluation_summary`：如输出，应分为 2–3 个自然段，每段围绕如“风险暴露/资金使用效率/执行节奏”等小主题给出 2–4 句建议，同样避免绝对化表述。
     - 对 `items` 列表进一步明确：
       - `has_position = true` 的标的可给出 `increase/decrease/exit/hold` 并配合 `suggested_trade_shares` 或 `target_position_percent`。
       - `has_position = false` 的标的必须在 `increase`（建议新建或放大仓位）与 `avoid`（暂不参与）之间做出判断，并“尽量给出保守的建议仓位或股数”。
       - 所有 `stock_symbol` 必须来自组合持仓或报告股票列表，禁止出现上下文外的代码。
   - 在 `_build_payload_from_llm` 中：
     - 通过合并当前 IBKR 持仓与报告股票生成 `allowed_symbols` 集合，仅保留 `stock_symbol` 落在该集合内的推荐条目，过滤掉 LLM 可能虚构的标的。

3. **后端：保持规则兜底逻辑覆盖无持仓股票**
   - `_build_recommendations_from_reports` 仍然对所有报告股票生成推荐条目，无论当前是否有 IBKR 持仓：
     - 若报告文本包含“买入/增持/加仓/buy/add”则 `action = 'increase'`，在无持仓场景下含义为“建议建立新仓位”。
     - 若仅出现“卖出/减持/降低仓位等”且当前无持仓，则 `action = 'avoid'`，表示“不建议建仓”。

4. **前端：API 类型与模型选择下拉**
   - 在 `frontend/src/api/portfolio.ts` 中：
     - 将 `portfolioApi.generateRecommendations` 签名改为 `async generateRecommendations(reportIds: string[], modelName?: string)`，请求体同时携带 `report_ids` 与可选 `model_name`。
     - 为 `PortfolioRecommendationData` 增加可选字段 `used_model?: string | null`，用于接收后端返回的实际使用模型名称。
   - 在 `frontend/src/views/Positions/index.vue` 的“持仓推荐” Tab 中：
     - 引入 `configApi`，新增 `availableModels / selectedModel / modelsLoaded` 三个响应式变量。
     - 在首次切换到 `activeTab === 'recommend'` 时，通过 `configApi.getDefaultModels()` 和 `configApi.getLLMConfigs()` 加载系统默认 quick 模型与所有启用的 LLM 配置，并填充模型下拉选项。
     - 在“选择报告”卡片右上角增加 `el-select` 模型选择器，展示 `model_display_name || model_name`，右侧附带能力等级 Tag 与 provider 名称，体验风格与单股分析页保持一致。
     - 在调用 `portfolioApi.generateRecommendations` 时附带当前选中的 `selectedModel`，并在持仓推荐结果卡片副标题处尾部显示 `使用模型：{{ recommendationResult.used_model }}`（当后端返回该字段时）。

5. **前端：持仓推荐结果展示与文案调整**
   - 将持仓推荐结果卡片标题从“持仓推荐结果”改为“持仓推荐”，与功能定位更一致。
   - 将评估卡片标题从“模型综合评估”改为“综合评估建议”，弱化对具体模型的强调，更突出内容本身。
   - 调整“组合层面说明”展示方式：
     - 移除原有的 `el-alert` 信息框，改为普通标题 + 段落文本：
       - 标题：`组合层面说明`（`overall-title`）
       - 内容：`overall_comment`（`overall-text`），并使用 `white-space: pre-wrap` 支持多段落展示。
   - 对“综合评估建议”文本同样使用 `white-space: pre-wrap`，保证长文本和换行结构在前端完整、易读地呈现。

**影响**:

- 前端用户在“持仓推荐”页现在可以：
  - 通过模型下拉框显式选择本次持仓推荐使用的 LLM 模型（例如从 qwen-turbo 切换到 qwen3-max）。
  - 在结果卡片中看到“使用模型：XXX”，且该名称与实际选用模型一致，避免与系统默认模型混淆。
  - 以普通段落的形式阅读更长、更结构化的“组合层面说明”和“综合评估建议”，而不是零散的提示框文字。
- 后端 LLM 调用在标的范围、输出结构和文本长度上的约束更明确：
  - 对无持仓但有报告的股票，会更稳定地给出“建议建仓（increase）/暂不参与（avoid）”判断和相应理由。
  - 组合层面和策略层面的说明文本长度与覆盖要点更加接近真实投研复盘场景。
- 现有依赖 `/api/portfolio/recommendations` 的调用若不传 `model_name`，仍保持完全兼容，将自动使用系统配置的默认 quick 模型。

**是否需要重启**:

- 需要。后端 Python 路由与 LLM 调用逻辑有修改，需要重启 FastAPI/uvicorn 后生效。
- 前端变更需要重新构建或重启前端开发服务器，浏览器刷新后方可看到模型下拉选择和新的文案展示效果。