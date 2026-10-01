# PromptPerp user documentation

**English** | [简体中文](README.zh-CN.md)

## Start here

These are the documents an operator or first-time user is expected to read. Every
document in this list has an English and Simplified Chinese version with reciprocal
links at the top.

| Purpose | English | 简体中文 |
|---|---|---|
| Project overview | [README](../README.md) | [项目说明](../README.zh-CN.md) |
| First installation and safe Binance preparation | [Beginner guide](BEGINNER_GUIDE.md) | [零基础教程](BEGINNER_GUIDE.zh-CN.md) |
| Security and vulnerability reporting | [Security policy](../SECURITY.md) | [安全政策](../SECURITY.zh-CN.md) |
| Contributing changes | [Contributing](../CONTRIBUTING.md) | [参与贡献](../CONTRIBUTING.zh-CN.md) |

## Language policy

- A new user-facing document must add English and Simplified Chinese versions in the
  same pull request.
- Each pair must link to the other language near the top of the page.
- Safety warnings, commands, supported capabilities and limitations must have the
  same meaning in both versions.
- CI checks the declared user-document pairs and reciprocal links.
- A translation must not silently add a capability or weaken a safety rule.

Files under `docs/acceptance/` are immutable engineering evidence, and files under
`docs/adr/` are architecture decision records. The remaining design and operations
documents are maintainer references for implementing or reviewing integrations; they
are not steps a beginner must follow to complete the offline quick start. When any of
that material is promoted into the user journey, both language versions are required
before it is linked from this index.
