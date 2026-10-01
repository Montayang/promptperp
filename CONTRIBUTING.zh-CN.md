# 参与贡献

[English](CONTRIBUTING.md) | **简体中文**

PromptPerp 接受安全性、可靠性、文档和策略接口方面的改进。贡献内容不得包含私有策略、
凭据、账户数据、投资人数据、日志、运行状态、数据集或服务器详情。

## 开发环境

完整安全测试需要 Linux 开启非特权用户与网络命名空间，并安装 `bubblewrap` 和
`unshare`。Debian 或 Ubuntu 可执行：

```bash
sudo apt-get install bubblewrap util-linux
```

使用 Python 3.12，并在独立主题分支工作：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip install --no-deps -e .
./scripts/quality.sh
```

所有测试和示例都必须离线运行。绝不能把实盘入口当作安装检查。涉及执行、风险、批准、
所有权或恢复的改动，需要覆盖成功、拒绝、结果不确定和篡改场景，并提供用户可见的迁移说明。

生成的策略材料必须使用 `StrategySpec`；执行生成 Python 或绕过操作员批准的 Pull Request
不会被接受。

提交前检查 Git 状态、差异、空白错误、仓库扫描和构建产物。不要强推共享分支。
