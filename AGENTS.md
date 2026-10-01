# Repository instructions

- Keep tests and examples offline and credential-free.
- Never start a strategy, call a private exchange API, place/cancel orders or send
  email as part of development verification.
- Treat uncertain orders, failed reconciliation, missing protection and foreign
  positions as blocking errors.
- Do not add proprietary strategies, account data, investor data, runtime state,
  credentials, logs, datasets or host access details.
- Generated strategy output is untrusted data and cannot authorize execution.
- Review status, diff, whitespace and the repository scan before committing.
