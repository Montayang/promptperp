# PromptPerp beginner guide

**English** | [简体中文](BEGINNER_GUIDE.zh-CN.md)

This guide is for someone who has never programmed or operated a Linux server. You
can copy each command exactly as shown. Stop whenever a result differs from the
expected result; do not guess your way through a trading-system error.

> [!CAUTION]
> PromptPerp `0.1.x` is an alpha framework, not a one-click trading bot. The public
> repository can install, validate strategies and run offline demonstrations. It does
> **not** ship a generic live strategy service. Preparing a Binance API key does not
> authorize PromptPerp, an AI agent or anyone else to trade.

## 1. What you will accomplish

By the end of this guide you will have:

1. a small Linux server;
2. a working PromptPerp installation;
3. a successful offline example that cannot place orders;
4. an understanding of how an AI agent may prepare an untrusted strategy candidate;
5. a safely prepared Binance Futures API key for a future, separately reviewed
   integration.

You will **not** have a live strategy running. Live use needs a deployment-specific
worker, Testnet evidence, account reconciliation, risk limits and a separate explicit
operator approval. Those pieces cannot safely be replaced by copying an API secret
into a file.

## 2. Before spending money

You need:

- a computer with a terminal application;
- a GitHub account only if you want to contribute changes;
- a cloud server with a stable public IP address;
- enough time to read every warning before considering Testnet or live use;
- a Binance account that is legally available in your location if you later use the
  exchange integration.

Binance products and permissions vary by country and account. Confirm local law,
Binance eligibility and the current Binance interface yourself. This project cannot
open an account, complete identity checks or accept Binance terms for you.

## 3. Choose a server

For learning and offline evaluation, a practical starting server is:

| Item | Beginner recommendation |
|---|---|
| Operating system | Ubuntu Server 24.04 LTS, 64-bit |
| CPU | 2 virtual CPUs |
| Memory | 4 GB RAM |
| Storage | 25 GB SSD or more |
| Network | Stable public IPv4 address |
| Login | SSH key, not only a password |

This is a learning baseline, not a production capacity guarantee. Choose a reputable
provider in a region where its service and Binance are legally available to you. Do
not buy a managed “trading bot image” or a server that arrives with unknown software.

When creating the server:

1. select a fresh Ubuntu 24.04 LTS image;
2. add your own SSH public key in the provider panel;
3. record the server IP address, but never publish it in an issue or screenshot;
4. enable provider backups only after understanding whether they are encrypted;
5. do not place Binance secrets in cloud-init, provider notes or support messages.

## 4. Connect and apply basic protection

Open Terminal on macOS/Linux or PowerShell on Windows. Replace `SERVER_IP` with the
address shown by your provider:

```bash
ssh ubuntu@SERVER_IP
```

Some providers use another login name such as `root`. Follow the provider's official
SSH instructions. Once connected, update the server and install the required tools:

```bash
sudo apt-get update
sudo apt-get upgrade -y
sudo apt-get install -y git python3 python3-venv python3-pip bubblewrap util-linux ca-certificates curl ufw
```

Enable the firewall without locking out SSH:

```bash
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status
```

The final output should say that the firewall is active and OpenSSH is allowed. Do not
open a web port, database port or remote desktop port for PromptPerp; none is needed
for the offline examples.

Check Python:

```bash
python3 --version
```

Ubuntu 24.04 normally reports Python 3.12. PromptPerp supports Python 3.10 through
3.12, while its primary quality environment uses 3.12.

## 5. Download and install PromptPerp

Run one block at a time:

```bash
git clone https://github.com/Montayang/promptperp.git
cd promptperp
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip install --no-deps -e .
```

After `. .venv/bin/activate`, the prompt normally starts with `(.venv)`. Activate the
environment again after every new SSH login:

```bash
cd promptperp
. .venv/bin/activate
```

Never run the project with `sudo`, and never paste an API key into an installation
command.

## 6. Run a harmless offline example

Start with the simplest example:

```bash
python examples/offline_platform.py
```

Expected output resembles:

```json
{"allowed": true, "execution_permitted": false, "mode": "offline", "strategy_id": "threshold_momentum", "symbol": "BTCUSDT"}
```

The important fields are `"mode": "offline"` and
`"execution_permitted": false`. The example uses fictional prices and cannot submit
an order.

Now run the repository quality gate:

```bash
./scripts/quality.sh
```

Formatting, lint, type checks and tests should pass. On a host that prevents Linux
user namespaces, the real sandbox acceptance may be reported as unavailable or
skipped. That is a missing safety capability, not permission to disable isolation.
Such a host is ineligible for the Agent pipeline until the host configuration is
fixed.

## 7. What “use AI with natural language” means

PromptPerp does not bundle an AI model. You may work with an external coding agent
that can read this repository. The safe workflow is:

1. you describe the strategy in ordinary language;
2. the agent produces a declarative `StrategySpec` candidate;
3. PromptPerp validates and evaluates it offline;
4. you inspect the strategy, assumptions and risk limits;
5. a separate, expiring approval may authorize a specific immutable package;
6. only a separately built and reviewed live integration could reach Binance.

You can give an agent this starter request:

```text
Read README.md, docs/BEGINNER_GUIDE.md, docs/STRATEGY_INTERFACE.md,
docs/RISK_ENGINE.md and docs/AGENT_STRATEGY_PIPELINE.md first. Turn the following
idea into a PromptPerp StrategySpec candidate. Work offline only. Do not use
credentials, call Binance, approve execution or claim profitability. Explain every
rule and risk assumption in plain language before running offline validation:

<describe the strategy here>
```

Generated material is untrusted. A sentence such as “run this live” cannot grant an
agent trading authority, bypass reconciliation or widen a risk limit.

## 8. Prepare Binance Futures safely

You do not need a Binance account or API key for the offline steps above. Complete
this section only if you are preparing for a future Testnet or reviewed integration.

1. Visit Binance by typing its official address yourself; do not follow API links from
   advertisements, direct messages or search-engine ads.
2. Protect the Binance account with a unique password, multi-factor authentication
   and the anti-phishing features offered for your account.
3. Complete the account and USDⓈ-M Futures activation steps Binance requires in your
   region.
4. Read the current official
   [USDⓈ-M Futures API documentation](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info).
5. Prefer the official
   [Binance Futures Testnet](https://testnet.binancefuture.com/) for integration work.
   Testnet and production credentials are different.

Interfaces and eligibility change. If a button or permission differs from this guide,
stop and follow current Binance documentation instead of selecting a similar-looking
option.

## 9. Create a restricted API key

In the Binance website, the current path is generally the account/profile menu and
**API Management**. Names may vary by region. Create a dedicated key only for this
deployment; never reuse a key from another bot or service. Use Binance's current
[official API-key instructions](https://www.binance.com/en/support/faq/how-to-create-api-keys-on-binance-360002502072)
if the interface differs from the outline below.

When Binance asks for settings:

1. give the key a clear name such as `promptperp-testnet` or
   `promptperp-production`;
2. complete Binance's security verification;
3. allow only the Futures trading permission needed by the reviewed integration;
4. **never enable withdrawals**;
5. restrict the key to the server's fixed public IP address;
6. keep Testnet and production keys separate;
7. copy the secret once into an approved password manager or secret store—the secret
   may not be shown again.

To learn the server's outbound IPv4 address, compare the cloud provider panel with:

```bash
curl -4 https://api.ipify.org
```

They must match before adding the address to the API allowlist. A dynamic residential
IP or a server whose outbound IP changes is unsuitable for this setup.

> [!WARNING]
> Never paste an API key or secret into GitHub, an issue, a strategy file, chat, email,
> a screenshot, shell history or a command-line argument. Never send it to an AI
> agent. If a secret is exposed, disable or delete the key in Binance immediately and
> create a new one after investigating the exposure.

## 10. Where credentials belong

The public Alpha does not provide a generic live worker, so there is no beginner step
that says “paste the key here.” That omission is intentional.

A reviewed downstream deployment must store credentials outside the Git repository,
in a service-specific file readable only by its operating-system identity. The
documented production layout uses `/etc/promptperp/credentials` with directory mode
`0700` and secret-file mode `0600`. Do not create that production layout merely to
make the offline examples work; they require no credentials.

Do not add keys to `.env` inside the repository. `.gitignore` reduces accidental
commits but is not a security boundary.

## 11. Required path before live trading

The safe progression is:

```text
offline validation
        ↓
Binance Futures Testnet integration
        ↓
stop/uncertain-order/recovery tests
        ↓
account reconciliation and ownership review
        ↓
small, time-limited production approval
        ↓
supervised live operation
```

Do not skip a stage. Before any real order, a deployment must at minimum demonstrate:

- a reviewed live worker exists and has a reliable stop procedure;
- exact symbol, leverage, margin and loss limits are bound to an approval;
- current balance, positions and open orders reconcile;
- foreign/manual positions block automation unless explicitly assigned;
- order timeouts are treated as uncertain rather than retried blindly;
- protective orders and restart recovery are tested;
- logs redact secrets and an operator can receive alerts;
- the API approval has an expiry and can be revoked.

PromptPerp fails closed when these facts are unknown. Removing a check to make an
error disappear makes the system less safe and is not a valid fix.

## 12. Updating and asking for help

Before updating, ensure no downstream live service is running. For this offline Alpha:

```bash
cd promptperp
. .venv/bin/activate
git status
git pull --ff-only
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip install --no-deps -e .
./scripts/quality.sh
```

If `git status` shows local changes, stop and ask for help instead of deleting them.

Use a public GitHub Issue for documentation mistakes and offline bugs. Never include
credentials, account identifiers, balances, orders, investor information, server IPs
or logs containing private data. Report security problems privately as described in
[the security policy](../SECURITY.md).

## 13. Common questions

**Can I run a profitable strategy after following this guide?**

No. The bundled strategy is an offline interface example, not a profit claim.

**Does an API key make the public version live?**

No. The generic live daemon is intentionally absent.

**Can an AI agent approve its own generated strategy?**

No. Generated output is untrusted, and approval is explicit, narrow, expiring and
separate.

**Can I use my laptop instead of a server?**

For offline learning, yes, if it runs a compatible Linux environment. A supervised
live service needs stable networking, isolation, monitoring and controlled restarts.

**What should I do if I am unsure?**

Stop before the exchange or credential step. Ask a question containing only
non-sensitive details.
