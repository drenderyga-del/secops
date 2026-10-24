# Operator workflow recovery contract

Status: release acceptance contract for recovered production behavior.

## GitLab connection boundary

Each GitLab instance is saved and tested independently. `Test connection`
verifies:

1. the configured API endpoint answers `/version`;
2. the credential authenticates;
3. at least one project is visible through project discovery.

The connection test must not run CI Lint against the first returned project.
CI Lint and repository-content reads are project-scoped Config Guard
capabilities. Their result depends on the selected project, token role and
project policy, so they cannot be used as a generic credential-health probe.

## Active failed-job ordering

An unresolved failure is identified by repository, job name and ref. A missing
ref is a valid key value and matches another missing ref. A later success
resolves the failure. If GitLab reports equal terminal timestamps, ingestion
order is the deterministic tie-breaker:

- failure followed by success: resolved;
- success followed by failure: unresolved.

This changes query semantics only. It does not change the database schema and
does not require a migration.

## Release acceptance

- backend regression tests prove that connection testing never issues a CI
  Lint POST;
- malformed, empty and unauthorized project-discovery responses remain
  explicit errors;
- equal-timestamp recovery is tested in both insertion orders, including
  signals without a ref;
- Config Guard's project-scoped CI Lint client remains unchanged;
- integration QA and seeded browser QA pass on the exact deliverable commit;
- the exact source and target SHAs receive an independent security review.
