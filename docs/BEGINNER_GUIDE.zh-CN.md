# PromptPerp 零基础使用教程

[English](BEGINNER_GUIDE.md) | **简体中文**

本教程写给从未编程、也没有管理过 Linux 服务器的使用者。你可以逐段复制命令。如果结果与
教程描述不一致，请立即停下，不要靠猜测处理交易系统错误。

> [!CAUTION]
> PromptPerp `0.1.x` 是 Alpha 阶段的框架，不是一键交易机器人。公开仓库可以安装、验证
> 策略并运行离线演示，但**没有**附带通用实盘策略服务。准备币安 API Key 不代表
> PromptPerp、AI Agent 或任何其他人已经获得交易授权。

## 1. 学完后你能做到什么

完成本教程后，你将拥有：

1. 一台小型 Linux 服务器；
2. 一个可以正常工作的 PromptPerp 安装环境；
3. 一次绝不可能下单的成功离线演示；
4. 对 AI Agent 如何生成“不可信策略候选”的正确理解；
5. 为将来单独审查的集成安全准备好的币安合约 API Key。

你**不会**因此直接得到正在运行的实盘策略。实盘还需要针对具体部署开发工作进程、取得
Testnet 证据、完成账户对账、设置风险限制，并由操作员另行明确批准。这些步骤不能用“把
API Secret 粘贴进文件”来替代。

## 2. 花钱以前先准备什么

你需要：

- 一台带终端程序的电脑；
- 只有在准备贡献代码时才需要 GitHub 账户；
- 一台具有固定公网 IP 的云服务器；
- 在考虑 Testnet 或实盘以前，逐条读完安全警告的时间；
- 如果将来接入交易所，需要一个在你所在地合法可用的币安账户。

币安产品和权限会因国家、地区及账户而异。你必须自行确认当地法规、币安准入条件和当前
币安界面。本项目不能替你开户、完成身份认证或接受币安条款。

## 3. 选择服务器

用于学习和离线评估时，可以从下面的配置开始：

| 项目 | 新手建议 |
|---|---|
| 操作系统 | Ubuntu Server 24.04 LTS，64 位 |
| CPU | 2 个虚拟 CPU |
| 内存 | 4 GB |
| 硬盘 | 25 GB 或更大的 SSD |
| 网络 | 固定公网 IPv4 地址 |
| 登录方式 | SSH 密钥，而不是只用密码 |

这是学习环境的起点，不是生产容量保证。请选择信誉良好、且在你所在地合法可用的服务商。
不要购买所谓“交易机器人镜像”，也不要使用预装来源不明软件的服务器。

创建服务器时：

1. 选择全新的 Ubuntu 24.04 LTS 镜像；
2. 在服务商控制台添加你自己的 SSH 公钥；
3. 记下服务器 IP，但不要把它发布到 Issue 或截图中；
4. 只有了解备份是否加密后，才开启服务商备份；
5. 不要把币安密钥写进 cloud-init、服务商备注或客服消息。

## 4. 连接服务器并做基础保护

macOS/Linux 打开“终端”，Windows 打开 PowerShell。把 `SERVER_IP` 换成服务商显示的
服务器地址：

```bash
ssh ubuntu@SERVER_IP
```

部分服务商使用 `root` 等其他用户名，请以服务商官方 SSH 说明为准。连接成功后更新服务器
并安装所需工具：

```bash
sudo apt-get update
sudo apt-get upgrade -y
sudo apt-get install -y git python3 python3-venv python3-pip bubblewrap util-linux ca-certificates curl ufw
```

在不阻断 SSH 的前提下开启防火墙：

```bash
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status
```

最后应显示防火墙已启用，且允许 OpenSSH。PromptPerp 离线示例不需要网页、数据库或远程
桌面端口，不要为它们开放端口。

检查 Python：

```bash
python3 --version
```

Ubuntu 24.04 通常显示 Python 3.12。PromptPerp 支持 Python 3.10 至 3.12，主要质量环境
使用 3.12。

## 5. 下载并安装 PromptPerp

每次复制并运行一段：

```bash
git clone https://github.com/Montayang/promptperp.git
cd promptperp
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip install --no-deps -e .
```

运行 `. .venv/bin/activate` 后，命令提示符通常会以 `(.venv)` 开头。每次重新通过 SSH
登录后，都要重新激活环境：

```bash
cd promptperp
. .venv/bin/activate
```

不要用 `sudo` 运行项目，也不要把 API Key 粘贴进安装命令。

## 6. 运行无害的离线示例

先运行最简单的示例：

```bash
python examples/offline_platform.py
```

输出应类似：

```json
{"allowed": true, "execution_permitted": false, "mode": "offline", "strategy_id": "threshold_momentum", "symbol": "BTCUSDT"}
```

最重要的是 `"mode": "offline"` 和 `"execution_permitted": false`。这个演示使用虚构价格，
不可能提交订单。

然后运行仓库质量检查：

```bash
./scripts/quality.sh
```

格式、代码检查、类型检查和测试都应通过。如果主机禁止 Linux 用户命名空间，真实沙箱验收
可能显示不可用或跳过。这代表主机缺少安全能力，不代表可以关闭隔离。修好主机配置前，
该主机不能用于 Agent 流水线。

## 7. “用自然语言让 AI 工作”是什么意思

PromptPerp 本身不附带 AI 模型。你可以让能够读取本仓库的外部编程 Agent 协助工作。
安全流程是：

1. 你用普通语言描述策略；
2. Agent 生成声明式 `StrategySpec` 候选；
3. PromptPerp 在离线环境验证和评估；
4. 你检查策略、假设和风险限制；
5. 另一个限时批准可以授权某一个不可变策略包；
6. 只有单独开发并审查过的实盘集成才能接触币安。

可以把下面这段话交给 Agent：

```text
请先完整阅读 README.zh-CN.md、docs/BEGINNER_GUIDE.zh-CN.md、
docs/STRATEGY_INTERFACE.md、docs/RISK_ENGINE.md 和
docs/AGENT_STRATEGY_PIPELINE.md。把下面的想法转换成 PromptPerp StrategySpec
候选。只允许离线工作；不要使用凭据、调用币安、批准执行或声称能够盈利。运行离线验证
以前，先用普通语言解释每一条规则和风险假设：

<在这里描述你的策略>
```

生成内容始终是不可信的。“把它实盘运行”这样的句子不能赋予 Agent 交易权限，不能绕过
对账，也不能放宽风险限制。

## 8. 安全准备币安合约账户

前面的离线步骤完全不需要币安账户或 API Key。只有在准备未来的 Testnet 或已审查集成时，
才继续本节。

1. 自己输入币安官方网址，不要点击广告、私信或搜索广告中的 API 链接；
2. 使用独立密码、多因素认证以及账户提供的防钓鱼功能保护币安账户；
3. 按照所在地要求完成账户及 USDⓈ-M 合约开通流程；
4. 阅读最新的官方
   [USDⓈ-M 合约 API 文档](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info)；
5. 集成时优先使用官方
   [Binance Futures Testnet](https://testnet.binancefuture.com/)，Testnet 与生产环境密钥不同。

币安界面和准入要求可能变化。如果按钮或权限和教程不一样，请停止操作并以币安最新官方
文档为准，不要选择一个“看起来差不多”的选项。

## 9. 创建受限 API Key

币安网站当前通常从账户/个人菜单进入 **API Management（API 管理）**。不同地区的名称
可能不同。只为这一次部署创建专用 Key，不要复用其他机器人或服务的 Key。如果界面与
下文不同，请使用币安最新的
[官方 API Key 教程](https://www.binance.com/en/support/faq/how-to-create-api-keys-on-binance-360002502072)。

币安要求设置权限时：

1. 使用清晰名称，例如 `promptperp-testnet` 或 `promptperp-production`；
2. 完成币安的安全验证；
3. 只允许已审查集成真正需要的合约交易权限；
4. **绝不能开启提现权限**；
5. 把 Key 限制到服务器的固定公网 IP；
6. Testnet 与生产环境分别使用不同 Key；
7. Secret 通常只显示一次，请保存到获准的密码管理器或秘密存储中。

可以用下面的命令查看服务器出口 IPv4，并与云服务商控制台进行核对：

```bash
curl -4 https://api.ipify.org
```

两边必须一致，才可以把这个地址加入 API IP 白名单。动态家庭网络 IP 或出口 IP 会变化的
服务器不适合这种配置。

> [!WARNING]
> 绝不能把 API Key 或 Secret 粘贴到 GitHub、Issue、策略文件、聊天、邮件、截图、Shell
> 历史或命令行参数中，也不能发送给 AI Agent。如果 Secret 泄露，请立即在币安禁用或
> 删除该 Key，调查泄露原因后重新创建。

## 10. 凭据应该放在哪里

公开 Alpha 没有通用实盘工作进程，所以本教程不会出现“把 Key 粘贴到这里”这一步。这是
有意设计的安全边界。

经过审查的下游部署必须把凭据保存在 Git 仓库外，并放进只有对应操作系统身份能读取的
服务专用文件。文档定义的生产布局使用 `/etc/promptperp/credentials`，目录权限为 `0700`，
秘密文件权限为 `0600`。不要为了运行离线示例而创建生产目录；离线示例不需要任何凭据。

不要把 Key 写进仓库里的 `.env`。`.gitignore` 只能降低误提交概率，并不是安全边界。

## 11. 实盘以前必须经过的路径

安全晋级顺序是：

```text
离线验证
        ↓
Binance Futures Testnet 集成
        ↓
停止、订单不确定、恢复测试
        ↓
账户对账与所有权审查
        ↓
小额度、限时生产批准
        ↓
受监督实盘运行
```

不能跳过任何一层。发出第一笔真实订单以前，部署至少必须证明：

- 已有经过审查的实盘工作进程，并且停止方法可靠；
- 准确的 symbol、杠杆、保证金和亏损限制已绑定到批准；
- 当前余额、持仓和挂单已经完成对账；
- 外来或手动持仓会阻止自动化，除非已明确分配所有权；
- 订单超时会被视为状态不确定，而不是盲目重试；
- 保护单与重启恢复已经测试；
- 日志会隐藏秘密，操作员可以收到告警；
- API 批准有截止时间，并且可以撤销。

事实不明确时 PromptPerp 会失败关闭。为了让报错消失而删除检查，会让系统变得更危险，
不属于有效修复。

## 12. 更新和求助

更新以前应确认没有任何下游实盘服务正在运行。对当前离线 Alpha，可以执行：

```bash
cd promptperp
. .venv/bin/activate
git status
git pull --ff-only
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip install --no-deps -e .
./scripts/quality.sh
```

如果 `git status` 显示本地改动，请停下并求助，不要自行删除。

文档错误和离线 Bug 可以提交公开 GitHub Issue。绝不能附带凭据、账户标识、余额、订单、
投资人信息、服务器 IP 或含有私有数据的日志。安全问题应按照
[安全政策](../SECURITY.zh-CN.md)私下报告。

## 13. 常见问题

**学完教程后能直接运行盈利策略吗？**

不能。附带策略只是离线接口示例，不是收益声明。

**创建 API Key 后，公开版就能实盘了吗？**

不能。公开版有意不提供通用实盘守护进程。

**AI Agent 能批准自己生成的策略吗？**

不能。生成内容不可信，批准必须由外部明确给出，范围有限、会过期且彼此分离。

**可以用自己的电脑代替服务器吗？**

离线学习可以，只要具有兼容的 Linux 环境。受监督实盘服务需要稳定网络、隔离、监控和
受控重启。

**不确定下一步时怎么办？**

在接触交易所或凭据以前停下，只使用不敏感的信息提问。
