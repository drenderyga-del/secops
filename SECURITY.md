# Security

Please report suspected vulnerabilities privately through the repository's
security reporting feature. Do not include credentials, live customer data,
or internal endpoints in public issues, pull requests, screenshots or logs.

Use synthetic data for examples and tests. Keep `.env`, trust bundles, private
keys, database dumps and private deployment configuration outside Git. The demo
credentials apply only to the isolated, loopback-only demo environment.

Before publishing or pushing source, install Gitleaks and run:

```bash
scripts/check_public_source.sh
```

The checked-in configuration permits only exact synthetic regression fixtures.
It does not exempt production code, whole test directories, or arbitrary tokens.
A clean scanner report is evidence from pattern-based detection; manually review
organization-specific data and generated or binary assets as well.

If a real credential was committed, revoke or rotate it before removing it from
Git history. Deleting a file or rewriting a branch does not revoke a credential
or remove copies in forks, caches, clones, or hosting-provider retention.
