# Public release checklist

This checklist must pass before changing `Montayang/promptperp` to public.

## Repository contents

- [ ] Clean, purpose-built Git history with no ancestry from the private repository.
- [ ] No proprietary strategy implementation, test vector, behavior specification or
      live strategy unit.
- [ ] Repository and artifact scans report no credential-like material.
- [ ] Gitleaks scans the complete public history with no findings.
- [ ] The quality workflow passes from a clean checkout.
- [ ] Built wheel and sdist contain only expected public files.

## GitHub settings

- [ ] Default branch is `main`.
- [ ] Repository description and website identify the project as independent and
      experimental.
- [ ] Issues and private vulnerability reporting are enabled.
- [ ] Secret scanning and push protection are enabled.
- [ ] Dependabot alerts, security updates and version updates are enabled.
- [ ] A `main` ruleset blocks force-push and deletion, requires a pull request, linear
      history and the unique `offline-quality` status check.
- [ ] Code scanning is enabled before it is made a required merge check.
- [ ] Discussions, wiki and packages are disabled unless they have an owner.

## Release

- [ ] Create a signed `v0.1.0-alpha.1` tag only after the public CI result is green.
- [ ] Release notes repeat the alpha, no-audit, no-investment-advice and no-live-worker
      boundaries.
- [ ] Do not publish to PyPI until the name is reserved by a verified account with 2FA
      and trusted publishing configured.
