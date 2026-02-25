# 变更记录 (Changes)

本文档记录对项目代码的修改，便于回溯与协作。

---

## 2026-02-25

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
