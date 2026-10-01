# 执行状态、所有权与恢复

A3 执行内核把每笔交易视为由事件驱动、可重放的状态机。它不启动策略，也不直接创建币安客户端；所有外部交易能力通过 A2 的最小执行接口注入。

标准协调器覆盖开仓、保护、确定性主动平仓、部分平仓对账、交易所托管保护单触发和
归因结算。相关故障注入由 `tests/test_execution_recovery.py` 持续验证。下游实盘入口只有
在明确接入协调器后才能声明具备这些能力。

同一 run 的多笔交易通过内容寻址的 `ExecutionRegistry` 管理，每个 intent 使用独立
事件链；任何未结算 intent 都阻止创建下一笔。

## 状态机

合法主路径为：

```text
INTENT -> SUBMITTED -> ACKNOWLEDGED -> PARTIALLY_FILLED -> FILLED
       -> PROTECTING -> PROTECTED -> CLOSING -> CLOSED -> SETTLED
```

`PARTIALLY_FILLED` 可以记录后续部分成交进展。任何非终态都阻止同一 run 开启新意图；无法安全继续时进入 `BLOCKED`，该状态没有自动离开路径。

每次迁移必须给出原因，只允许更新白名单字段。快照包含策略、run、intent、确定性 client order ID、交易所普通/保护订单 ID、成交数量和均价，以及结算收益、手续费和资金费。

## 追加式事件账本

`EventLedger` 使用版本化 JSON Lines。每条事件包含连续序号、前一记录哈希和自身哈希；回放会拒绝：

- 截断或非法 JSON；
- 未支持的 schema；
- 序号缺口；
- 哈希链断裂或内容篡改；
- 所有权在中途变化；
- 非法状态迁移或不支持字段。

单条记录以追加方式写入并执行 `fsync`。账本路径必须位于独立运行状态目录，不能提交到 Git。

## 单写者租约

所有状态写入强制要求已获取且 `strategy_id/run_id` 匹配的 `RunLease`。没有租约的状态机只能只读回放。租约使用操作系统文件锁：

- 第二个进程不能同时获取同一 run；
- 进程退出后内核释放锁，恢复进程可重新获取；
- 心跳元数据用于观测，但不能仅凭时间戳抢占仍被持有的锁；
- 释放租约不删除账本、仓位所有权或保护身份。

## 幂等提交与恢复

entry、stop 和 take-profit 的 client order ID 由 strategy/run/intent/role 确定性生成，符合币安长度和字符限制。

entry 写请求超时后，状态保持 `SUBMITTED`。恢复流程只能先用同一个 client order ID 查询：

- 查到订单：校验 symbol、方向、仓位方向、数量和 client ID，再推进状态；
- 部分成交：记录数量并阻止新开仓；
- 查询仍未知：抛出对账失败，禁止直接重试；
- 返回对象不匹配：视为所有权/对账失败，不采用也不修改。

## 保护确认

进入 `PROTECTING` 前先持久化 stop/take-profit client order ID。提交返回或超时都不代表保护已完成。
只有分别查询到两个保护单，并记录其交易所算法单 ID 后，才能进入 `PROTECTED`。

若进程在提交后崩溃，重启会直接查询确定性 ID，不重复挂保护单。缺任一保护单或查询失败均为阻断错误。

## 外来对象

`OwnershipReconciler` 只承认本地事件账本中已知的订单 ID。仓位所有权按同一 symbol/position-side 的自有意图成交量汇总，并与交易所仓位精确比较。

未知订单、未知仓位或数量差异均为阻断错误。执行接口没有全账户撤单或清仓能力，因此恢复逻辑不能修改外来对象。

## 平仓、保护清理与结算

主动平仓在交易所写入前进入 `CLOSING` 并持久化确定性的 EXIT client ID；结果
未知时只查询该 ID。交易所保护单触发时，系统必须把算法单 ID 解析到真实成交
订单 ID，并校验完整平仓数量后进入 `CLOSED`。

进入 `CLOSED` 后，系统只按本 intent 的 STOP/TAKE_PROFIT client ID 取消仍活动
的条件单。禁止按 symbol 全量撤单。清理确认作为同一状态的追加事件写入账本；
没有该确认不得进入 `SETTLED`。

结算只接受影子事件库中已核验、未隔离且带同一 intent 归属的成交和资金费。
realized PnL、commission 与 funding 分字段持久化，净额还必须与钱包余额变化一致。
结算或本地状态保存中断后，恢复流程使用账本中的持久 close 时间和已结算金额，
不得重复下单或按新的时间边界重复归集。
