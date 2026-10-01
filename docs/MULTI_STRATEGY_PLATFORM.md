# 通用多策略运行平台

状态：统一控制面完成；公开发行包提供一个离线样例插件，不包含实盘入口
日期：2026-10-01

## 目标与边界

`promptperp.runtime` 为少量 USD-M 策略提供同一套 validate、plan、start、status、stop、
rebalance、reconcile 和 report 生命周期。它位于纯策略与既有风险/执行内核之间，不包含
Binance SDK、私有接口、凭据、邮件或进程启动能力。

控制面 `start` 只把持久 run 从 `PLANNED` 改为 `RUNNING`，不会启动后台进程或下单。
任何真实 worker 仍必须取得自己的运行租约、通过 `LiveRunApproval`、既有 `RiskEngine`
和执行状态机。公开仓库不提供实盘 worker。

## 插件契约

内置 registry 只显式注册代码库内已审核的 `threshold_momentum` 样例，不扫描目录或动态
导入任意 Python。生产或私有策略通过下游显式 registry 注入，不进入公开发行包。

每个插件公开策略版本、接口版本、参数 schema 版本、事件类型和 fingerprint。参数先迁移
再规范化；未知字段和不支持版本直接拒绝。插件只返回统一 `SignalProposal`，不能接触
账户、文件、环境变量、网络或执行端口。

## 资金与风险预算

`configs/multi_strategy.example.toml` 展示虚构配置。账户可分配权益按 strategy fraction
形成预算，并保留独立现金比例。每个策略另有：

- 总资本预算；
- 单笔最大保证金；
- 最大杠杆；
- 单笔最大计划损失；
- 最大同时持仓数。

组合层再限制全账户最大持仓数、保证金、杠杆、余额缓冲、信号时效和 symbol allowlist。
底层 SQLite 事务会在占用资源时重新检查运行状态、全局模式、run 预算和组合上限，避免
两个 worker 同时基于旧快照越限。配置变化只能在该 run 没有任何预留或持仓时 rebalance。

这层组合预算不会替代既有逐意图 `RiskEngine`；真实执行必须同时通过两层。

## 共享账户冲突政策

v1 固定采用 `EXCLUSIVE`：同一 symbol 无论策略、方向或是否看似可以净额抵消，只能由
一个活动 run 占用。原因是共享合约账户无法在交易所层隔离客户资金，同 symbol 并行会
让成交、保护单、资金费和人工操作的归属变得不可证明。

没有完整 `strategy_id/run_id` 所有权的持仓或订单一律是 foreign object。人工交易因此
不会被系统猜测吸收，而会触发阻断。未来若支持同 symbol 组合净额，必须新增独立 ADR、
归属模型和真实协议验收，不能只放宽一个配置开关。

## 所有权与恢复

允许开仓时，控制面在同一事务先建立 symbol `RESERVED`。执行层确认真实成交后才能将其
推进为 `OPEN`。任何遗留 `RESERVED` 都代表可能已下单但结果未知，会阻断全平台新开仓。
恢复只能选择：

1. 由已知订单/持仓身份继续执行恢复并确认 `OPEN`；或
2. 在权威账户快照证明该 symbol 无仓位、无订单后释放空预留。

不能为了恢复并发能力直接删除状态。平仓只允许精确减少已证明属于该 run 的数量；即使
全局模式为 `KILL_SWITCH`，这种 owned reduction 仍保留。停止有持仓的 run 只进入
`STOP_REQUESTED`，结算并释放 claim 后才能进入 `STOPPED`。

## 全局模式与对账

- `NORMAL`：允许通过全部门禁的新开仓；
- `REDUCE_ONLY`：禁止任何新开仓，保留精确 owned reduction；
- `KILL_SWITCH`：紧急禁止新开仓，同样不放弃已持有对象的安全退出能力。

对账比较控制面 claim 与账户持仓/订单所有权。失败、缺失、外来对象或未决执行会阻断
活动 run；在 `NORMAL` 时还会自动降级为 `REDUCE_ONLY`。成功对账可以清理 reconciliation
阻断，但不会自动把全局模式恢复成 `NORMAL`，必须由操作员显式决定。

## 离线操作入口

以下命令只修改本地 control-plane SQLite，不连接币安：

```bash
python -m promptperp.operations.platform_service \
  --database /tmp/promptperp-platform.sqlite3 \
  --config configs/multi_strategy.example.toml plugins

python -m promptperp.operations.platform_service \
  --database /tmp/promptperp-platform.sqlite3 \
  --config configs/multi_strategy.example.toml validate \
  --strategy-id threshold_momentum \
  --parameters '{"symbol":"BTCUSDT","margin":"100"}'
```

生产状态、账户 snapshot 和真实资金配置属于敏感运行数据，必须放在 Git 忽略的 owner-only
目录。示例配置仅含虚构数字，不能直接视为实盘风险批准。

## 运行报告

每个 run 的结算结果分别记录 realized PnL、commission、funding、最大滑点、成交数量和
异常代码。报告只聚合被证明属于该 run 的结果，不使用整个钱包变化替代归属，也不包含
订单 ID、凭据或投资人明细。
