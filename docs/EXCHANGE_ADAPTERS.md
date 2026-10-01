# 交易所领域边界

`promptperp.domain` 和 `promptperp.exchange` 是受支持的交易内核边界。策略不得导入币安 SDK、读取凭据、处理 SDK 原始对象或调用旧客户端。

## 稳定类型

- `SymbolRules`：逐合约数量步长、最低数量、价格 tick 和最低名义价值，全部使用 `Decimal`。
- `Order`：订单身份、方向、仓位方向、状态、请求量、成交量和均价。
- `Fill`：成交身份、数量、价格和手续费。
- `FuturesPosition`：非零合约仓位。
- `AccountBalance`：钱包余额和可用余额。

类型在构造时检查安全不变量。无效数量、缺失身份和不可能的成交量不会进入策略或执行层。

## 接口分离

`promptperp.exchange.interfaces` 定义三个最小协议：

- `MarketDataGateway` 只提供行情和合约规则；
- `AccountGateway` 只提供仓位、余额和持仓模式读取；
- `ExecutionGateway` 只允许按 `symbol + order_id` 查询和撤销订单，以及携带确定性 `client_order_id` 下单。

接口没有全账户清仓或全账户撤单方法。`BinanceFuturesAdapter` 通过构造参数注入 SDK 的 `rest_api`，自身不创建 SDK、不读取环境变量，也不在构造时访问网络。

## 失败语义

| 异常 | 含义 | 调用方动作 |
|---|---|---|
| `RequestRejected` | 交易所明确拒绝，或本地请求不合法 | 修正意图，不盲目重试 |
| `RequestUnknown` | 请求结果或账户状态无法安全确定 | 停止新开仓并对账 |
| `ResponseShapeError` | SDK 响应无法转换；属于未知状态 | 阻断并检查 SDK 契约 |
| `ReconciliationFailed` | 本地记录与交易所状态不一致 | 阻断并人工/自动对账 |
| `ProtectionFailed` | 无法确认保护单完整 | 阻断并进入安全恢复 |
| `ForeignPositionDetected` | 无法证明仓位属于当前 run | 不得修改该仓位 |

查询超时绝不转换成空仓、空订单或零价格。写请求超时表示结果未知；执行层必须先按幂等标识查询交易所状态，不能直接重试。

## SDK 契约

适配器兼容 SDK 的以下边界差异：

- 响应可以是字典，或带 `.data()` / `.data` 的响应对象；
- 数据行可以是字典或带 `.to_dict()` 的 SDK 模型；
- 已知字段兼容 camelCase 与 snake_case；
- 缺字段、非法枚举、非有限数值和不可能的订单数量均产生 `ResponseShapeError`。

固定离线响应位于 `tests/test_binance_futures_adapter.py`。SDK 升级后必须先运行这些契约测试；不得为了适配新响应而恢复空值默认或宽泛吞错。

## 合约规则

数量和价格必须先通过目标交易对的 `SymbolRules` 量化。不得使用客户端级统一精度，也不得用二进制浮点完成下单量计算。规则按 symbol 缓存于单个适配器实例；新运行实例会重新读取规则。

公开发行包不包含历史脚本客户端或私有策略。下游集成不得绕过这些领域接口直接把 SDK
对象交给策略代码。
