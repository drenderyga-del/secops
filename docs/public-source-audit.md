# Public source publication

This repository starts with a new, reviewed source snapshot. Private Git
history, branches, tags, agent configurations, critique notes, local environment
files and untracked work are not part of the import. Public build defaults and
synthetic examples replace deployment-specific infrastructure configuration.

The public snapshot was checked with Gitleaks 8.30.1 (default rules plus exact
synthetic fixture exceptions), organization-reference searches, tracked-file
and asset inventory review, and local application tests. Checks are repeated
against a fresh clone after upload. Secret-scan and application checks are also
configured in GitHub Actions; the workflow does not publish images or deploy.

To reproduce the source checks:

```bash
scripts/check_public_source.sh
npm ci --prefix frontend
npm run build --prefix frontend
scripts/qa.sh fast
scripts/qa.sh integration
```

Pattern-based scanning cannot prove the absence of every possible secret or
confidential fact. Private hosting history and retention are separate from
this fresh public repository; no claim is made that a secret would be revoked
by deleting a file or replacing a repository. See SECURITY.md for incident
handling and private reporting.

Provider API contracts are tested using doubles. Live ASOC installations,
production access and external LLM vendors are not exercised by the demo.
