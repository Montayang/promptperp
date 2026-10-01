# PromptPerp

[English](README.md) | **简体中文**

面向币安 USDⓈ-M 永续合约策略的 AI 原生、安全门控基础设施。

PromptPerp 将结构化的策略想法转换为可复现的候选策略包，在离线环境中评估，
绑定操作员的明确批准，然后让获得批准的意图依次通过组合、风险、所有权和恢复控制。
本仓库是独立开源项目，与 Binance 没有隶属或背书关系。

> [!WARNING]
> 永续合约可能导致快速且全部的本金损失。PromptPerp 是实验性软件，不构成投资建议，
> 也尚未接受独立安全审计。本仓库中的任何内容都不代表一次实盘运行已获授权。

## 包含的能力

- 带版本的 `StrategySpec` 契约和基于 Decimal 的确定性解释器。
- 离线评估、内容寻址策略包和篡改检测。
- 一次性、会过期的操作员批准账本。
- 操作系统级沙箱，且不存在进程内降级执行。
- 带资金预算和交易标的所有权的多策略控制平面。
- 失败关闭的币安 USDⓈ-M 适配器、执行恢复和保护单所有权。
- 组合级和单意图级风险门禁、终止开关和降级模式。
- 可选的共用账户投资人账本和报告子系统。
- 离线部署、晋级、备份、回滚和健康检查基础能力。
- 一个刻意保持简单的示例策略：`threshold_momentum`。

私有生产策略不属于本仓库。公开包不附带实盘策略服务，也不附带自主 LLM。
外部 AI Agent 可以把自然语言需求转换成文档定义的 `StrategySpec`；PromptPerp
始终将该输出视为不可信数据，绝不会让它自行取得交易权限。

## 安全模型

```text
自然语言想法
        ↓ 外部 AI Agent
不可信 StrategySpec
        ↓ 模式与语义验证
离线评估 + 沙箱
        ↓ 不可变策略包
明确、限时、一次性批准
        ↓ 资金分配 + 组合 + 风险门禁
具有所有权的执行状态机
        ↓ 针对具体部署的实盘集成
币安 USDⓈ-M 合约
```

公开版快速入门会停在批准层和交易所层之前。实盘运行需要针对具体部署实现的工作进程、
私有凭据、权威账户对账和操作员的另一次明确决定。订单状态不确定、数据过期、外来持仓、
所有权不明确或保护失败都会阻止新增风险。

## 零基础入门

如果你不会编程，或从未管理过 Linux 服务器，请先阅读
[零基础使用教程](docs/BEGINNER_GUIDE.zh-CN.md)。教程涵盖服务器准备、安装、离线验证和
币安 API Key 的安全准备，并会明确说明当前 Alpha 版本尚不是一键实盘产品。

经过测试的开发环境使用 Python 3.12：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip install --no-deps -e .
./scripts/quality.sh
```

只运行仓库附带的离线演示：

```bash
python examples/offline_agent_pipeline.py
python examples/offline_platform.py
```

这两个命令都不会读取 `.env`、连接币安、发送邮件或允许执行交易。

## 项目状态

PromptPerp 目前是 Alpha 版本。公开仓库提供的是引擎与安全边界，不是一套保证盈利的系统。

| 能力 | 公开版状态 |
|---|---|
| StrategySpec 与确定性信号引擎 | 支持离线使用 |
| AI 候选策略验证与打包 | 支持离线使用 |
| 操作员批准账本 | 支持离线使用 |
| 示例策略 | 仅限离线 |
| 币安适配器与可恢复执行 | 提供库 API，需要自行集成 |
| 通用实盘策略守护进程 | 未提供 |
| AI 自主批准或交易 | 明确禁止 |

两个有明确边界的真实协议周期所产生的非敏感结果保留在
[A9 验收记录](docs/acceptance/A9_REAL_PROTOCOL_ACCEPTANCE.md)中。它们只是兼容性证据，
不是收益声明，也不是交易建议。

## 用户文档

- [用户文档索引与双语规则](docs/README.zh-CN.md)
- [零基础使用教程](docs/BEGINNER_GUIDE.zh-CN.md) — 服务器、安装、离线使用和币安 API 安全
- [安全政策](SECURITY.zh-CN.md)
- [参与贡献](CONTRIBUTING.zh-CN.md)

架构、风险、执行恢复、部署和验收材料是开发者与维护者参考资料，并不是普通用户完成
离线入门的必读项。这些资料会按照双语文档政策逐步整理，不能代替零基础教程中的安全边界。

## 参与贡献

欢迎提交安全性、可靠性、文档和策略接口方面的改进。提交 Pull Request 前请阅读
[参与贡献](CONTRIBUTING.zh-CN.md)。测试必须保持离线且不包含凭据。安全漏洞应按照
[安全政策](SECURITY.zh-CN.md)私下报告。

## 许可证

MIT，参见 [LICENSE](LICENSE)。
