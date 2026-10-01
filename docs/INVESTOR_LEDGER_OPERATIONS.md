# 投资人账本 MVP 运维说明

状态：C0-C7 代码与离线运行验收完成；生产 timers 未启用
日期：2026-10-01

## 当前能力边界

当前实现提供 SQLite 复式账本、份额净值、资金流门禁、成交结算归属、对账快照、持久化
报表调度、显式批准的 SMTP 投递和只读 IMAP 邮件查询。交易所事件读取层只有私有只读
能力，不下单；生产 timers 默认不安装、不启用。

`user0 = 1500 USDT` 只存在于离线测试和 `examples/offline_accounting.py` 的临时数据库中，没有写入生产状态，也没有改变当前策略滚仓金额。

## 一次性历史迁移

生产身份和金额只能放在 Git 忽略的 owner-only 配置中，例如
`log/investor_bootstrap.toml`。先运行不带 `--apply` 的验证，再在空仓、无挂单、
交易所权益已冻结且业务账本为空时显式应用：

```bash
python -m promptperp.operations.investor_bootstrap \
  --config log/investor_bootstrap.toml

python -m promptperp.operations.investor_bootstrap \
  --config log/investor_bootstrap.toml \
  --database log/investor_accounting.sqlite3 \
  --apply
```

dry-run 和应用配置都必须是 `0600`，迁移整体在一个数据库事务中完成并可幂等重放。
迁移只允许用于没有投资人、策略池和会计事务的业务账本。操作员确认的历史
月报保存为导入快照，不冒充交易所对账；切账点会生成正式 reconciliation 和
估值快照。禁止提交配置、SQLite/WAL、导出、报表或备份。

## 文件和权限

- 生产数据库、WAL 和备份必须放在仓库外或 Git 忽略的运行状态目录。
- 状态目录建议 `0700`，SQLite 主文件和备份为 `0600`。
- `accounting_service` 是业务记账、估值、对账和报表生成的唯一写者。
- `email_query_provider` 使用只读挂载的会计数据库；它自己的收件去重库只保存 message ID、发件地址摘要及回复投递状态。
- `email_outbox_worker` 只写邮件 outbox 投递状态，不提供任意会计写接口；它必须使用独立 mail 身份和不含交易配置的凭据文件。
- 禁止把 SQLite 文件放在 NFS 或其他网络共享文件系统上。

## 安全入口

以下入口均不连接币安或邮件服务：

```bash
python -m promptperp.operations.accounting_service --database /state/accounting.sqlite3 init
python -m promptperp.operations.accounting_service --database /state/accounting.sqlite3 health
python -m promptperp.operations.accounting_service --database /state/accounting.sqlite3 show-investor --investor-id user0
python -m promptperp.operations.accounting_service --database /state/accounting.sqlite3 backup --destination /backup/accounting.sqlite3
python -m promptperp.operations.accounting_service --database /state/accounting.sqlite3 restore-drill --destination /tmp/recovered-accounting.sqlite3
python -m promptperp.operations.accounting_service --database /state/accounting.sqlite3 monitor --backup-manifest /backup/accounting.sqlite3.manifest.json
python -m promptperp.operations.accounting_service --database /state/accounting.sqlite3 run-scheduler
```

`generate-statement` 和 `run-scheduler` 只把不可变报表放入 outbox，并输出 `queued_not_sent`；它们不会发信。`email_query_worker` 是不连接 provider 的纯本地白名单命令处理器。

### 显式发送一封 outbox 邮件

先应用缺失的本地 schema migration 并检查待发送数量；该命令不会连接 SMTP：

```bash
python -m promptperp.operations.email_outbox_worker \
  --database log/investor_accounting.sqlite3
```

真实投递必须指定已入队的 message ID，或使用 `--next` 每次领取最早的一封；同时要求
当前干净提交、最长一小时的批准窗口并显式添加 `--execute`。未提供 delivery ID 时，
worker 由 message ID 和批准起点生成确定性身份。收件人不能从命令行传入，只能来自
账本内仍然有效的 verified email。环境文件必须是 owner-only 权限，且不能包含交易配置。

消息在网络调用前原子进入 `SENDING`。只有 SMTP 正常返回并成功落库后才进入 `SENT`；
任何异常都视为投递结果不确定并保持 `SENDING`，不得自动重试，必须由操作员先在邮箱
provider 侧核验。该入口不读取交易密钥、不启动策略，也不批量发送。自动报告生成和
投递是两个独立 oneshot；生成失败不会触发发信，投递失败不会影响交易。

创建投资人、资金流、交易结算和对账目前通过类型化 Python service API 完成，刻意不提供便捷的管理员 CLI，避免未经核验的人工命令直接改变资金权益。

## 启动与健康检查

1. 在无实盘凭据的环境执行 `init`；重复执行会校验并应用仅前向 schema migration。
2. 启动任何 writer 前确认只有一个 accounting service，并锁定运行目录权限。
3. 执行 `health`；它同时运行 SQLite `integrity_check` 和完整账本哈希链重放。
4. 健康检查失败时停止记账、估值和报表生成，不得自动修复或重建余额。
5. 邮件查询数据库必须以 SQLite read-only URI/只读文件挂载打开。

## 备份与恢复

运行中备份只能使用提供的 SQLite online backup 操作；不能只复制正在使用的主文件。
命令会立即恢复到隔离副本，核对完整性、账本链和运行指纹，并生成带哈希的 `0600`
manifest。生产备份目录必须位于加密卷。恢复步骤：

1. 停止会计 writer，但不修改交易状态。
2. 把备份恢复到新的隔离路径，不覆盖原文件。
3. 执行 `health`，核对 schema、SQLite 完整性、分录平衡和哈希链。
4. 用已知 reconciliation ID 和投资人合计权益进行人工比对。
5. 只有原文件保留且恢复证据通过后，操作员才能批准切换路径。

## 实盘和邮件晋级前置条件

- C3 已由两个连续主网完整周期验证真实 fill、commission、funding、过账与最终权益对账。详见 [A9 验收](acceptance/A9_REAL_PROTOCOL_ACCEPTANCE.md)。
- C5 自动调度和 SMTP provider 已实现；真实发信仍需要单独批准 provider、已验证收件人、模板和次数，outbox 失败不能影响交易。
- C6 IMAP provider 已实现只读抓取、安全丢弃和隔离回复状态；真实自动回复尚未启用。
- C7 systemd 模板位于 `deploy/systemd/`，安装身份、ACL、加密卷、告警和启用 timers 均属于独立上线动作。
- 让风险引擎读取 `allocatable_equity`、启用真实定时邮件或迁移生产账本，都需要独立上线审批。
