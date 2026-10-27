# Prepare a private-to-public update

Use `scripts/prepare_public_sync.py` to prepare a reviewable update from a local
GitLab checkout into the public source tree. It does not change either checkout,
copy Git history, commit, push, or contact a hosting service.

## Inputs

You need Python 3.12, Git and Gitleaks on PATH, plus:

- A source checkout with the reviewed GitLab commits already fetched.
- The last accepted source revision and the new reviewed revision. The former
  must be an ancestor of the latter. Use the source revisions recorded in the
  preceding successful export report, not GitHub's independent commit IDs.
- A public checkout whose committed HEAD is the destination baseline. Commit
  public-only changes first; untracked and uncommitted files are not exported.
- Private replacement rules, stored outside both repositories. Start from
  [the generic example](../scripts/public-sync-rules.example.json), replace its
  example values locally, set mode 600, and keep the real policy out of Git.
  Rules must be an owner-private regular file outside both repositories;
  symlinks are refused.
- New output/report paths outside both repositories, under a private directory.

Example after setting `LAST_SOURCE_SHA` and `NEW_SOURCE_SHA` to reviewed commits:

```bash
python3 scripts/prepare_public_sync.py \
  --source /path/to/gitlab-checkout \
  --base-ref "$LAST_SOURCE_SHA" \
  --ref "$NEW_SOURCE_SHA" \
  --public /path/to/public-checkout \
  --rules /path/to/private-audit/rules.json \
  --output /path/to/private-audit/candidate-001 \
  --report /path/to/private-audit/report-001.json
```

The parent directories must exist. Use a mode-700 audit directory; the tool creates
its output with mode 700 and its report with mode 600. Existing outputs/reports
are never replaced. A nonzero exit means the candidate is blocked.

## What the tool does

1. Reads committed blobs from the selected source/base and public HEAD.
   Working-tree files, untracked state and Git history are not copied.
2. Excludes environment files except `.env.example`, certificates/keys, dumps,
   SQLite data, local agent state, build caches, browser artifacts and private
   GitLab CI/merge-gate files. Symlinks/submodules outside excluded paths block
   the export rather than being followed.
3. Runs Gitleaks on a filename manifest (including excluded paths), then on
   retained source files before replacements. A credential
   finding blocks preparation; replacing a credential does not revoke it.
4. Applies configured literal replacements to text. It then applies the source
   delta with three-way comparison, retaining independent public changes.
   Conflicts, including deletion of a publicly modified file, block the export.
   Case-folded/Unicode-equivalent filename and directory aliases also block
   preparation before files can overwrite one another.
5. Preserves the public README, contributor/agent instructions, license and
   GitHub workflows, and public secret-scanning policy/scripts. Source changes
   to these are recorded for manual review.
6. Blocks remaining configured internal markers, Cyrillic Markdown, and new
   binary content without an approved SHA-256. Unchanged public binaries are
   already approved by their current hashes.
7. Scans the assembled snapshot again, then creates a new candidate directory.

Source privacy checks use the public checkout's committed Gitleaks policy;
the incoming source cannot replace its own scanner policy for that run.

## Rule fields

| Field | Meaning |
| --- | --- |
| `schema_version` | Must be `1`. |
| `exclude` | Extra path globs to omit from source/base snapshots. |
| `keep_public` | Extra path globs owned by the public tree; incoming changes are reported but not applied. |
| `replacements` | Ordered rules with `id`, nonempty `find`, `replace`, optional `paths` globs and `ignore_case` (default true). Replacements are literal, not executable code or regular expressions. |
| `forbidden_literals` | Case-insensitive internal markers that must not remain in output paths or text. |
| `approved_binary_sha256` | Exact lowercase SHA-256 digests of manually reviewed new binary content. |

Unknown rule keys and invalid configuration fail closed. Reports contain rule IDs,
counts and paths, not matched text or scanner output. Keep reports private because
source filenames and revision metadata can themselves identify internal work.

## Review and publish

`prepared_for_review` means the configured mechanical checks passed. Review the
reported updated/removed paths and preserved-public paths. Inspect new images and
other binaries before approving their exact hashes. Patterns cannot identify every
confidential fact or infer which domain-specific code should become configurable.

Apply only the reviewed file delta to a focused public branch. Preserve `.git`,
local files and public-only work; do not mirror the candidate with a global delete.
Update English documentation for any source changes deliberately kept out of the
public front page. Run checks appropriate to the behavior, obtain independent
review, and publish through the normal protected pull-request workflow.

Keep the successful report as the audit record. After publication, record the
accepted source revision and public commit for the next synchronization. A blocked
or merely prepared candidate must not advance the accepted-source baseline.
