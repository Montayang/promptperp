# C7 投资人账本生产运行验收

状态：PASS（代码与离线部署验收）；生产 timers 未启用
日期：2026-10-01

## 运行边界

- accounting scheduler、backup、monitor、email query 和 email outbox 均为有界 oneshot；
- scheduler、query 和 outbox 使用独占文件租约，重复启动不会产生双写或双发；
- systemd 模板分离 accounting、query 和 mail OS 身份，并限制文件与网络权限；
- query 身份只读访问主账本，只能写自己的去重/回复状态；
- 所有 mail worker 使用独立 owner-only 凭据文件并拒绝交易配置；
- 模板、文档和测试不包含真实投资人、账户、邮箱或凭据。

## 监控与失败关闭

`accounting_service monitor` 同时检查 SQLite 完整性、账本哈希链、最近对账时效、outbox
积压及 `SENDING` 不确定状态、未初始化报告计划、隔离事件、未解析订单以及已验证备份。
任一异常返回 `BLOCKED` 和退出码 2，供主机告警接管；不会自动修复、重试邮件或改动交易。

报表生成额外拒绝未来或超过 15 分钟的 reconciliation snapshot，所以陈旧数据不会被
描述为当前净值。交易长时间无结算时必须先由获批的对账生产者刷新快照。

## 备份与恢复演练

备份使用 SQLite online backup，在生成后立即对隔离副本执行 integrity check、完整账本
重放和非财务运行指纹比对。manifest 保存 SHA-256、大小、时间、schema 和数据库指纹，
文件权限为 `0600`。恢复演练只写新路径，绝不覆盖源数据库。测试覆盖成功恢复、过期
manifest 和篡改备份失败关闭。生产备份目录必须位于主机加密卷。

## 部署材料与保留审批

部署模板位于 `deploy/systemd/`。它们没有被安装或启动。以下行为仍需独立操作员批准：

1. 创建生产 OS 身份、目录、ACL 与加密备份卷；
2. 安装并启用任何 timer；
3. 配置主机告警接收端；
4. 提供真实邮件凭据和每次最长一小时的发送/回复批准；
5. 让策略风险额度读取投资人账本。
