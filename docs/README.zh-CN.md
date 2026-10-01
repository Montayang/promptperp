# PromptPerp 用户文档

[English](README.md) | **简体中文**

## 从这里开始

操作员或第一次使用本项目的人需要阅读下列文档。清单中的每一项都有英文和简体中文版本，
并且页面顶部可以相互跳转。

| 用途 | English | 简体中文 |
|---|---|---|
| 项目概览 | [README](../README.md) | [项目说明](../README.zh-CN.md) |
| 首次安装与安全准备币安 | [Beginner guide](BEGINNER_GUIDE.md) | [零基础教程](BEGINNER_GUIDE.zh-CN.md) |
| 安全与漏洞报告 | [Security policy](../SECURITY.md) | [安全政策](../SECURITY.zh-CN.md) |
| 参与项目开发 | [Contributing](../CONTRIBUTING.md) | [参与贡献](../CONTRIBUTING.zh-CN.md) |

## 双语规则

- 新增面向用户的文档时，必须在同一个 Pull Request 中同时加入英文和简体中文版本。
- 每一对文档都必须在页面顶部附近链接到另一种语言。
- 两个版本中的安全警告、命令、支持能力和限制必须表达相同含义。
- CI 会检查清单中声明的文档对以及双方跳转链接。
- 翻译不能悄悄增加系统能力，也不能削弱安全规则。

`docs/acceptance/` 中的文件是不可变工程证据，`docs/adr/` 中的是架构决策记录。其余设计和
运维材料是实现或审查集成时使用的维护者参考资料，不是新手完成离线快速入门所需执行的
步骤。如果其中任何内容将来进入用户使用流程，必须先补齐两个语言版本，才能加入本索引。
