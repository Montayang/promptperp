# Migration policy

State-schema migration is allowed only when ownership is proven, services are stopped,
the account is reconciled and there are no positions, open orders, pending settlement
or unresolved objects. Migration must retain a verified source backup and a rollback
plan. Schema crossings are never performed implicitly by upgrade or rollback.

Private strategy packages are downstream extensions. Moving a strategy into or out of
PromptPerp does not authorize changing its live state or disclosing its source.
