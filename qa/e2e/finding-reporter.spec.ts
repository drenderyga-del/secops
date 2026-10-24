import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import type { FullConfig, FullResult, Suite } from '@playwright/test/reporter';
import FindingReporter from './support/finding-reporter';

test('list-only reporter lifecycle is read-only and returns no gate override', async () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'scanops-finding-reporter-'));
  const outputDir = path.join(tempRoot, 'artifacts');

  try {
    const reporter = new FindingReporter({ outputDir, _mode: 'list' });
    reporter.onBegin({} as FullConfig, { allTests: () => [] } as unknown as Suite);
    const outcome = await reporter.onEnd({ status: 'passed' } as FullResult);

    expect(outcome).toEqual({ status: 'passed' });
    expect(fs.existsSync(outputDir)).toBe(false);
  } finally {
    fs.rmSync(tempRoot, { force: true, recursive: true });
  }
});
