import { expect, test, type Page } from '@playwright/test';

const job = (overrides: Record<string, unknown> = {}) => ({
  id: 1,
  gitlab_job_id: 101,
  name: 'security scan',
  stage: 'security',
  status: 'success',
  ref: 'main',
  started_at: '2026-08-05T09:00:00Z',
  finished_at: '2026-08-05T09:02:00Z',
  duration_seconds: 120,
  error: null,
  allow_failure: null,
  retry: null,
  last_seen_at: '2026-08-05T09:03:00Z',
  links: { gitlab: 'https://gitlab.example.invalid/jobs/101' },
  ...overrides,
});

const detailPayload = (stages: unknown[], overrides: Record<string, unknown> = {}) => ({
  pipeline: {
    id: 42,
    pipeline_id: 9001,
    project_id: 77,
    repository_id: 8,
    repository: 'platform/api',
    product: 'Platform',
    ref: 'main',
    commit_sha: 'abcdef1234567890',
    status: 'failed',
    started_at: '2026-08-05T09:00:00Z',
    finished_at: '2026-08-05T09:10:00Z',
    duration_seconds: 600,
    error_message: null,
    last_polled_at: '2026-08-05T09:11:00Z',
    sync: { last_polled_at: '2026-08-05T09:11:00Z', mode: 'snapshot', refresh_seconds: null },
  },
  completeness: {
    mode: 'observed',
    is_partial: null,
    jobs_complete: null,
    known_jobs: 0,
    expected_jobs: null,
    tracked_job_names: null,
    message: 'This view contains every job currently persisted by the tracker; completeness cannot be verified from retained data.',
  },
  summary: { stages: stages.length, jobs: 0, succeeded_jobs: 0, failed_jobs: 0, skipped_jobs: 0, duration_seconds: 600 },
  ordering: {
    mode: 'alphabetical_name',
    message: 'GitLab does not persist the declared CI stage order in this snapshot; stages are grouped by name for a stable view.',
  },
  stages,
  links: {
    pipelines: '/pipelines',
    gitlab: 'https://gitlab.example.invalid/platform/api/-/pipelines/9001',
    repository: 'https://gitlab.example.invalid/platform/api',
  },
  ...overrides,
});

const serveDetail = async (page: Page, payload: unknown) => {
  await page.route('**/api/ui/pipelines/42', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload) });
  });
  await page.goto('/pipelines/42', { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('platform/api');
};

test.describe('Pipeline execution detail', () => {
  test('switches from detail to the pipeline list through sidebar and quick actions', async ({ page }) => {
    await serveDetail(page, detailPayload([]));

    await page
      .getByRole('navigation', { name: 'Primary' })
      .getByRole('link', { name: 'Active pipelines', exact: true })
      .click();
    await expect(page).toHaveURL(/\/pipelines$/);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Pipelines in flight.');

    await page.goto('/pipelines/42', { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('platform/api');
    await page.getByRole('button', { name: /Quick action/ }).click();
    const dialog = page.getByRole('dialog', { name: 'Quick action' });
    await dialog.getByRole('option', { name: /Active pipelines/ }).click();
    await expect(page).toHaveURL(/\/pipelines$/);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Pipelines in flight.');
  });

  test('renders zero, one, and multiple persisted jobs without inventing stages', async ({ page }) => {
    await serveDetail(page, detailPayload([]));
    await expect(page.getByText('No persisted jobs')).toBeVisible();
    await expect(page.getByText('expected total not recorded')).toBeVisible();

    await page.unroute('**/api/ui/pipelines/42');
    await serveDetail(page, detailPayload([
      { id: 'stage-1', name: 'security', status: 'completed', succeeded: 1, total: 1, jobs: [job()] },
      { id: 'stage-2', name: 'publish', status: 'completed', succeeded: 2, total: 2, jobs: [job({ id: 2, gitlab_job_id: 102, name: 'publish image', stage: 'publish' }), job({ id: 3, gitlab_job_id: 103, name: 'publish manifest', stage: 'publish' })] },
    ], {
      summary: { stages: 2, jobs: 3, succeeded_jobs: 3, failed_jobs: 0, skipped_jobs: 0, duration_seconds: 600 },
    }));
    await expect(page.getByText('security', { exact: true })).toBeVisible();
    await page.locator('details.pipeline-stage').nth(1).locator('summary').click();
    await expect(page.getByText('publish image', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open in GitLab ↗' })).toHaveAttribute('href', /pipelines\/9001$/);
    await expect(page.locator('.pipeline-job-link').first()).toHaveAttribute('href', /jobs\/101$/);
  });

  test('marks tracked-only data, expands failed stage, and avoids mobile overflow', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const failedStage = {
      id: 'stage-1',
      name: 'security',
      status: 'failed',
      succeeded: 0,
      total: 1,
      jobs: [job({ status: 'failed', name: 'gitleaks', error: null })],
    };
    await serveDetail(page, detailPayload([failedStage], {
      completeness: {
        mode: 'tracked_only',
        is_partial: true,
        jobs_complete: false,
        known_jobs: 1,
        expected_jobs: null,
        tracked_job_names: ['gitleaks'],
        message: 'Only jobs named by the registration contract are persisted; other GitLab jobs may be absent.',
      },
      summary: { stages: 1, jobs: 1, succeeded_jobs: 0, failed_jobs: 1, skipped_jobs: 0, duration_seconds: 600 },
    }));
    await expect(page.getByText('Tracked jobs only')).toBeVisible();
    await expect(page.locator('details.pipeline-stage').first()).toHaveAttribute('open', '');
    await expect(page.getByText('gitleaks', { exact: true })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
});
