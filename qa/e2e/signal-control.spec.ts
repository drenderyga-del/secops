import { expect, test, type Locator, type Page } from '@playwright/test';

const expectSignalSelectValue = async (control: Locator, value: string) => {
  if (await control.evaluate((element) => element.tagName === 'SELECT')) {
    await expect(control).toHaveValue(value);
    return;
  }
  await expect(control).toHaveAttribute('data-value', value);
};

const selectSignalOption = async (
  page: Page,
  control: Locator,
  option: string,
) => {
  if (await control.evaluate((element) => element.tagName === 'SELECT')) {
    await control.selectOption({ label: option });
    return;
  }
  await control.click();
  await page.getByRole('option', { name: option, exact: true }).click();
};

const views = [
  ['overview', /^(?:Everything is moving\.|Signal needs attention\.|Waiting for first signal\.|Signal is unavailable\.)$/],
  ['executions', 'Live activity.'],
  ['jobs', 'Every observed job.'],
  ['pipelines', 'Pipelines in flight.'],
  ['config', 'Drift made visible.'],
  ['iac', /^Infrastructure(?:, (?:resolved|scanning|waiting)| needs attention| scan cancelled)\.$/],
  ['ptai', 'Attack surface, observed.'],
  ['failed', 'Failures, explained.'],
  ['coverage', 'What is protected.'],
  ['products', 'Products connected through ASOC.'],
  ['repositories', 'Every repository, one story.'],
  ['scanners', 'Compute at the ready.'],
  ['integrations', 'The control plane, connected.'],
] as const;

const primaryNavigationLabels = [
  'Overview',
  'Executions',
  'Job journal',
  'Active pipelines',
  'Config Guard',
  'IaC scanning',
  'PT AI',
  'Failed jobs',
  'Coverage',
  'Products',
  'Repositories',
  'Scanner fleet',
  'Integrations',
] as const;

const connectorFixture = (overrides: Record<string, unknown> = {}) => ({
  key: 'asoc',
  name: 'ASOC / Vampy',
  role: 'Evidence delivery',
  detail: 'Delivers normalized evidence to the application security control plane.',
  direction: 'outbound',
  canonical_direction: 'ScanOps → ASOC',
  configured: true,
  enabled: true,
  source: 'database',
  credential_ref: 'encrypted://integration/asoc',
  updated_at: '2026-07-28T08:00:00Z',
  updated_by: 'browser-contract',
  tested_at: '2026-07-28T08:04:03Z',
  test_status: 'healthy',
  test_message: 'ASOC accepted the exact evidence probe.',
  health_status: 'healthy',
  health_reason: 'Latest connection test passed',
  connection_summary: {
    total: 1,
    configured: 1,
    healthy: 1,
    failed: 0,
    untested: 0,
    disabled: 0,
  },
  last_successful_check: {
    at: '2026-07-28T08:04:03Z',
    message: 'ASOC accepted the latest successful check.',
  },
  error: null,
  fields: [
    {
      name: 'api_url',
      label: 'API URL',
      kind: 'url',
      secret: false,
      value: 'https://asoc.example.invalid/api',
      configured: true,
    },
    {
      name: 'token',
      label: 'Bearer token',
      kind: 'text',
      secret: true,
      value: '',
      configured: true,
    },
  ],
  instances: [],
  href: '/integrations?integration=asoc',
  ...overrides,
});

const integrationPayload = (rows: Array<Record<string, unknown>>) => ({
  management: {
    available: true,
    auth_ready: true,
    credential_store_ready: true,
  },
  summary: {
    integration_types: rows.length,
    connections: rows.length,
    configured: rows.length,
    healthy: rows.length,
    failed: 0,
    untested: 0,
    disabled: 0,
  },
  rows,
});

test.describe('Signal Control production workspace', () => {
  test('ASOC mass-removal sync asks for confirmation and retries with the reviewed snapshot', async ({ page }) => {
    const bodies: Array<Record<string, unknown>> = [];
    await page.route('**/api/ui/asoc-reporting/sync', async (route) => {
      const body = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
      bodies.push(body);
      if (!body.confirm_mass_removal) {
        await route.fulfill({
          status: 409,
          json: {
            detail: {
              code: 'confirmation_required',
              message: 'Review this removal.',
              active_count: 8,
              incoming_count: 1,
              removal_count: 7,
              registry_version: 'a'.repeat(64),
            },
          },
        });
        return;
      }
      await route.fulfill({
        json: { ok: true, result: { total: 1, mapped: 1, partial: 0, conflict: 0, missing_target: 0, no_repositories: 0, removed: 7 } },
      });
    });

    await page.goto('/targets?level=product');
    const sync = page.getByRole('button', { name: 'Synchronize ASOC' });
    const confirmation = page.getByRole('alertdialog', { name: 'Confirm ASOC product removal' });

    await sync.click();
    await expect(confirmation).toContainText('ASOC returned 1 products while 8 are active.');
    await expect(confirmation).toContainText('7 product(s) will be marked as removed');
    await confirmation.getByRole('button', { name: 'Cancel' }).click();
    await expect(confirmation).toHaveCount(0);
    expect(bodies).toHaveLength(1);

    await sync.click();
    await confirmation.getByRole('button', { name: 'Remove 7 and sync' }).click();
    await expect(page.locator('.pr-sync-message')).toContainText(
      '1 ASOC products: 1 synced with ScanOps, 0 need attention, 7 removed.',
    );
    await expect(confirmation).toHaveCount(0);
    expect(bodies).toHaveLength(3);
    expect(bodies[2]).toMatchObject({
      confirm_mass_removal: true,
      expected_active_count: 8,
      expected_incoming_count: 1,
      expected_registry_version: 'a'.repeat(64),
    });
  });

  test('every section resolves live state without preview copy', async ({ page }) => {
    for (const [view, expectedHeading] of views) {
      await page.goto(`/next?view=${view}`, { waitUntil: 'domcontentloaded' });
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(expectedHeading);
      await expect(page.locator('.environment')).toHaveText(
        /INTERNAL · (?:LIVE|DEGRADED|UNAVAILABLE)/,
      );
      await expect(page.locator('body')).not.toContainText('Signal Control preview');
      await expect(page.locator('body')).not.toContainText('PREVIEW');
      await expect(page.locator('.live-state-loading')).toHaveCount(0, {
        timeout: 10_000,
      });
    }
  });

  test('sidebar navigation is URL-addressable and browser history works', async ({ page }) => {
    await page.goto('/next?view=overview');
    await page.getByRole('link', { name: 'Products' }).click();
    await expect(page).toHaveURL(/\/targets\?level=product$/);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'Products connected through ASOC.',
    );

    await page.getByRole('link', { name: 'Repositories' }).click();
    await expect(page).toHaveURL(/\/targets\?level=repository$/);
    await page.goBack();
    await expect(page).toHaveURL(/\/targets\?level=product$/);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'Products connected through ASOC.',
    );
  });

  test('leaving the job journal through the sidebar preserves SPA history destinations', async ({ page }) => {
    await page.goto('/logs?status=failed');
    await expect(page).toHaveURL(/\/logs\?status=failed$/);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Failures, explained.');

    await page.getByRole('link', { name: 'Active pipelines' }).click();
    await expect(page).toHaveURL(/\/pipelines$/);

    await page.goBack();
    await expect(page).toHaveURL(/\/logs\?status=failed$/);
    await page.goForward();
    await expect(page).toHaveURL(/\/pipelines$/);
  });

  test('quick action palette routes to a unique section', async ({ page }) => {
    await page.goto('/next?view=overview');
    await page.getByRole('button', { name: /Quick action/ }).click();
    const dialog = page.getByRole('dialog', { name: 'Quick action' });
    await expect(dialog).toBeVisible();
    await dialog.getByPlaceholder('Go to a ScanOps section…').fill('integr');
    await dialog.getByRole('button', { name: /Integrations/ }).click();
    await expect(page).toHaveURL(/\/integrations$/);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'The control plane, connected.',
    );
  });

  test('integration settings stay inside Signal Control and expose the real workflow', async ({
    page,
  }) => {
    await page.goto('/next?view=integrations', {
      waitUntil: 'domcontentloaded',
    });
    const asocRow = page.locator('.connector-row').filter({
      hasText: 'ASOC / Vampy',
    });
    await expect(asocRow).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.integration-footer')).toHaveCount(0);
    await asocRow.click();
    await expect(page).toHaveURL(/\/integrations\?integration=asoc/);

    const dialog = page.getByRole('dialog', { name: 'ASOC / Vampy' });
    await expect(dialog).toBeVisible();
    const apiUrl = dialog.getByLabel('API URL');
    const bearerToken = dialog.getByLabel('Bearer token');
    await expect(apiUrl).toHaveAttribute('required', '');
    await expect(bearerToken).toHaveAttribute('type', 'password');
    await expect(bearerToken).toHaveValue('');
    const close = dialog.getByRole('button', {
      name: 'Close ASOC / Vampy settings',
    });
    const disable = dialog.getByRole('button', {
      name: /Disable integration|Integration disabled/,
    });
    const focusable = dialog.locator(
      "button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href], [tabindex]:not([tabindex='-1'])",
    );
    await expect(close).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(focusable.last()).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(close).toBeFocused();
    await bearerToken.fill('intercepted-browser-test-token');
    if (!(await apiUrl.inputValue())) {
      await apiUrl.fill('https://asoc.example.invalid/api');
    }
    const save = dialog.getByRole('button', {
      name: /Save (configuration|& enable)/,
    });
    await expect(save).toBeVisible();
    await expect(dialog.getByRole('button', { name: 'Test connection' })).toBeVisible();
    await expect(disable).toBeVisible();
    expect(new URL(page.url()).pathname).toBe('/integrations');

    let saveRequests = 0;
    await page.route('**/integrations/asoc/save', async (route) => {
      saveRequests += 1;
      await route.fulfill({
        status: 303,
        headers: {
          location: '/integrations?notice=ASOC+configuration+saved&tone=success',
        },
      });
    });
    await save.click();
    const feedback = dialog.getByRole('status');
    await expect(feedback).toContainText(
      'ASOC configuration saved',
    );
    await expect(feedback).toBeFocused();
    await expect(bearerToken).toHaveValue('');
    expect(saveRequests).toBe(1);
    expect(new URL(page.url()).pathname).toBe('/integrations');

    const closeBox = await close.boundingBox();
    expect(closeBox?.width ?? 0).toBeGreaterThanOrEqual(44);
    expect(closeBox?.height ?? 0).toBeGreaterThanOrEqual(44);
    await apiUrl.fill('https://unsaved.example.invalid/api');
    page.once('dialog', async (confirmation) => {
      expect(confirmation.message()).toContain('Discard unsaved integration changes');
      await confirmation.dismiss();
    });
    await page.evaluate(() => window.history.back());
    await expect(dialog).toBeVisible();
    await expect(page).toHaveURL(/integration=asoc/);

    page.once('dialog', async (confirmation) => {
      await confirmation.accept();
    });
    await page.evaluate(() => window.history.back());
    await expect(dialog).toHaveCount(0);
    await expect(asocRow).toBeFocused();
    await expect(page).not.toHaveURL(/integration=/);

    await page.goto(
      '/next?view=integrations&integration=gitlab&instance=gl-deadbeefdeadbeef',
      { waitUntil: 'domcontentloaded' },
    );
    const gitlabDialog = page.getByRole('dialog', { name: 'GitLab' });
    await expect(gitlabDialog).toBeVisible();
    await expect(page).not.toHaveURL(/instance=gl-deadbeefdeadbeef/);
    await expect(gitlabDialog.locator('.gitlab-instance-rail > button')).toHaveCount(3);
    await expect(gitlabDialog.getByLabel('Display name')).toHaveCount(1);
    await expect(gitlabDialog.getByLabel('API URL')).toHaveCount(1);
    await expect(gitlabDialog.getByLabel('Access token')).toHaveCount(1);
    expect(new URL(page.url()).pathname).toBe('/integrations');
  });

  test('integration direct links isolate the workspace only after the drawer mounts', async ({
    page,
  }) => {
    await page.route('**/api/ui/integrations', async (route) => {
      await route.fulfill({
        json: integrationPayload([connectorFixture()]),
      });
    });
    await page.goto('/integrations?integration=asoc', {
      waitUntil: 'domcontentloaded',
    });

    const dialog = page.getByRole('dialog', { name: 'ASOC / Vampy' });
    await expect(dialog).toBeVisible();
    await expect(
      dialog.getByRole('button', { name: 'Close ASOC / Vampy settings' }),
    ).toBeFocused();
    await expect(page.locator('.app-shell')).toHaveAttribute('inert', '');
    await expect(page.locator('.app-shell')).toHaveAttribute(
      'aria-hidden',
      'true',
    );
    await expect(dialog.locator('.integration-instance-signal')).toContainText(
      '2026-07-28 08:04:03 UTC',
    );
    await expect(dialog.locator('.integration-instance-signal')).toContainText(
      'ASOC accepted the exact evidence probe.',
    );
    await expect(dialog.locator('.integration-instance-signal')).toContainText(
      'ASOC accepted the latest successful check.',
    );
  });

  test('failed integration deep links leave retry controls interactive', async ({
    page,
  }) => {
    await page.route('**/api/ui/integrations', async (route) => {
      await route.abort('failed');
    });
    await page.goto('/integrations?integration=asoc', {
      waitUntil: 'domcontentloaded',
    });

    const retry = page.getByRole('button', { name: 'Try again' });
    await expect(retry).toBeVisible();
    await expect(retry).toBeEnabled();
    await expect(page.locator('.app-shell')).not.toHaveAttribute('inert', '');
    await expect(page.locator('.app-shell')).not.toHaveAttribute(
      'aria-hidden',
      'true',
    );
    await retry.focus();
    await expect(retry).toBeFocused();
  });

  test('integration timestamps and Mattermost test side effects are explicit', async ({
    page,
  }) => {
    const mattermost = connectorFixture({
      key: 'mattermost',
      name: 'Mattermost',
      role: 'Operator notifications',
      detail: 'Sends operational notifications to a configured channel.',
      canonical_direction: 'ScanOps → Mattermost',
      credential_ref: 'encrypted://integration/mattermost',
      updated_at: '2026-07-28T09:00:00Z',
      tested_at: null,
      test_status: null,
      test_message: null,
      health_status: 'partial',
      health_reason: 'Configuration changed; run a new connection test.',
      connection_summary: {
        total: 1,
        configured: 1,
        healthy: 0,
        failed: 0,
        untested: 1,
        disabled: 0,
      },
      last_successful_check: {
        at: '2026-07-27T15:12:11Z',
        message: 'Mattermost accepted the most recent delivery check.',
      },
      fields: [
        {
          name: 'webhook_url',
          label: 'Webhook URL',
          kind: 'url',
          secret: true,
          value: '',
          configured: true,
        },
      ],
      href: '/integrations?integration=mattermost',
    });
    await page.route('**/api/ui/integrations', async (route) => {
      await route.fulfill({
        json: integrationPayload([mattermost]),
      });
    });
    await page.goto('/integrations', { waitUntil: 'domcontentloaded' });

    const row = page.locator('.connector-row').filter({ hasText: 'Mattermost' });
    await expect(row).toBeVisible();
    await expect(row.locator('.connector-lag small')).toHaveText('updated');
    await row.click();

    const dialog = page.getByRole('dialog', { name: 'Mattermost' });
    await expect(dialog).toContainText(
      'Testing sends one visible message to the configured Mattermost channel.',
    );
    await expect(dialog.locator('.integration-instance-signal')).toContainText(
      '2026-07-27 15:12:11 UTC',
    );
    await expect(dialog.locator('.integration-instance-signal')).toContainText(
      'Mattermost accepted the most recent delivery check.',
    );
    await expect(
      dialog.getByRole('button', { name: 'Test connection' }),
    ).toHaveAttribute(
      'title',
      'Sends one test message to the configured Mattermost channel',
    );
  });

  test('configured write-only fields remain optional and Close does not create a history loop', async ({
    page,
  }) => {
    await page.goto('/next?view=overview', { waitUntil: 'domcontentloaded' });
    await page.goto('/next?view=integrations', {
      waitUntil: 'domcontentloaded',
    });
    const mattermostRow = page.locator('.connector-row').filter({
      hasText: 'Mattermost',
    });
    await expect(mattermostRow).toBeVisible({ timeout: 10_000 });
    await mattermostRow.click();

    const dialog = page.getByRole('dialog', { name: 'Mattermost' });
    await expect(dialog).toBeVisible();
    const webhook = dialog.getByLabel('Webhook URL');
    await expect(webhook).toHaveAttribute(
      'placeholder',
      'Configured — leave blank to keep',
    );
    await expect(webhook).not.toHaveAttribute('required', '');

    await dialog.getByRole('button', {
      name: 'Close Mattermost settings',
    }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page).toHaveURL(/\/integrations$/);

    await page.goBack();
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole('dialog')).toHaveCount(0);
  });

  test('scanner operator handoff keeps browser Back stable when GitLab infers its default instance', async ({
    page,
  }) => {
    await page.goto('/scanners/operator', { waitUntil: 'domcontentloaded' });
    await page.getByRole('link', { name: 'Manage GitLab connection' }).first().click();
    await expect(page.getByRole('dialog', { name: 'GitLab' })).toBeVisible();
    await expect(page).toHaveURL(/\/integrations\?integration=gitlab$/);

    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('dialog', { name: 'GitLab' })).toBeVisible();
    await expect(page).toHaveURL(/\/integrations\?integration=gitlab$/);

    await page.goBack({ waitUntil: 'domcontentloaded' });
    await expect(page).toHaveURL(/\/scanners\/operator$/);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'Security execution runtime',
    );
  });

  test('legacy integration focus bookmarks open the requested connector and normalize once', async ({
    page,
  }) => {
    await page.goto('/integrations?focus=gitlab', {
      waitUntil: 'domcontentloaded',
    });

    await expect(page.getByRole('dialog', { name: 'GitLab' })).toBeVisible();
    await expect(page).toHaveURL(/\/integrations\?integration=gitlab$/);
    expect(new URL(page.url()).searchParams.has('focus')).toBe(false);
  });

  test('integration registry labels preserved data as last-known after refresh failure', async ({
    page,
  }) => {
    let failRefresh = false;
    let resolveFailedRefresh: (() => void) | undefined;
    const failedRefresh = new Promise<void>((resolve) => {
      resolveFailedRefresh = resolve;
    });
    await page.clock.install();
    await page.route('**/api/ui/integrations', async (route) => {
      if (failRefresh) {
        await route.abort('failed');
        resolveFailedRefresh?.();
        return;
      }
      await route.continue();
    });
    await page.goto('/next?view=integrations', {
      waitUntil: 'domcontentloaded',
    });
    await expect(page.locator('.connector-row')).toHaveCount(4, {
      timeout: 10_000,
    });
    failRefresh = true;
    await page.clock.runFor(60_001);
    await failedRefresh;
    const lastKnown = page.getByRole('alert').filter({
      hasText: 'Last-known connection registry',
    });
    await expect(lastKnown).toBeVisible();
    await expect(lastKnown).toContainText(
      'Status and credential metadata may be stale',
    );
    await expect(page.locator('.connector-row')).toHaveCount(4);
  });

  test('Products is one ASOC registry that drills into the product card', async ({ page }) => {
    await page.goto('/next?view=products');
    await expect(page).toHaveURL(/\/targets\?level=product$/);
    const registry = page.getByRole('table', { name: 'Current registry' });
    const rows = registry.getByRole('row').filter({ has: page.getByRole('cell') });
    await expect(rows).toHaveCount(9, { timeout: 10_000 });
    await expect(registry.getByRole('columnheader', { name: 'Sync status' })).toBeVisible();
    await expect(page.getByRole('region', { name: 'GitLab groups without an ASOC card' })).toContainText('QA Product 08');

    const audience = page.getByRole('combobox', { name: 'Product audience' });
    await audience.click();
    await page.getByRole('option', { name: 'Internal' }).click();
    await expect(page).toHaveURL(/scope=internal/);
    await expect(rows).toHaveCount(5);
    await page.goBack();
    await expect(rows).toHaveCount(9);

    await rows.filter({ hasText: 'Synced' }).first().getByRole('link').first().click();
    await expect(page).toHaveURL(/\/targets\/products\/asoc\/[^/]+$/);
    await expect(page.getByRole('link', { name: '← Products' })).toHaveAttribute('href', /\/targets\?level=product/);
    await expect(page.getByRole('region', { name: 'Product facts' })).toBeVisible();
    await expect(page.getByRole('region', { name: 'ASOC to GitLab sync' })).toBeVisible();
    const groups = page.getByRole('region', { name: 'GitLab groups' });
    await expect(groups.getByRole('link', { name: 'Pipelines' }).first()).toHaveAttribute('href', /\/pipelines\?product=\d+/);
    const repositories = page.getByRole('table', { name: 'Product repositories' });
    await expect(repositories.getByRole('link', { name: 'Repository 360' }).first()).toHaveAttribute(
      'href',
      /\/targets\/repositories\/\d+$/,
    );
    await page.getByRole('group', { name: 'Repository status filter' }).getByRole('button', { name: /^Not scanned/ }).click();
    await expect(repositories).toContainText('unscanned-service');
    await expect(repositories).not.toContainText('archive-tool');
  });

  test('Product registry keeps connection dates legible without page overflow', async ({ page }) => {
    await page.goto('/targets?level=product');
    const table = page.getByRole('table').first();
    await expect(table.getByRole('columnheader')).toHaveCount(6);
    await expect(table.getByRole('columnheader', { name: 'Connected' })).toBeVisible();
    const connected = table.getByRole('row').nth(1).locator('small').last();
    await expect(connected).toBeVisible();
    const fontSize = await connected.evaluate((element) =>
      Number.parseFloat(getComputedStyle(element).fontSize),
    );
    expect(fontSize).toBeGreaterThanOrEqual(12);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);
  });

  test('repository search keeps every repository in one comparable row', async ({ page }) => {
    await page.goto('/next?view=repositories');
    const rows = page.locator('.repository-row');
    await expect(rows).toHaveCount(25, { timeout: 10_000 });
    const repositorySearch = page.getByRole('combobox', { name: 'Search repositories', exact: true });
    await repositorySearch.fill('service-002');
    await repositorySearch.press('ArrowDown');
    await repositorySearch.press('Enter');
    await expect(repositorySearch).toHaveValue('qa/product-01/service-002');
    await repositorySearch.fill('service-002');
    await repositorySearch.press('Enter');
    await expect(page).toHaveURL(/q=service-002/);
    await expect(rows).toHaveCount(8);
    const tableWidth = await page.locator('.repository-table').evaluate((element) => ({
      client: element.clientWidth,
      scroll: element.scrollWidth,
    }));
    expect(tableWidth.scroll).toBeLessThanOrEqual(tableWidth.client + 1);
    await expect(rows.first().locator('.repository-stat')).toHaveCount(2);
    await expect(rows.first().locator('.repository-stat').first().locator('small')).toHaveText(
      /\d+ healthy · \d+ unresolved failures/,
    );
    const overlappingHealthActions = await rows.evaluateAll((elements) => elements.flatMap((row) => {
      const health = row.querySelector(':scope > .status')?.getBoundingClientRect();
      const actions = row.querySelector('.repository-actions')?.getBoundingClientRect();
      return health && actions && health.right > actions.left
        ? [{ healthRight: health.right, actionsLeft: actions.left }]
        : [];
    }));
    expect(overlappingHealthActions).toEqual([]);
    await expect(rows.first().getByRole('link', { name: 'Repository 360' })).toHaveAttribute(
      'href',
      /\/targets\/repositories\/\d+/,
    );
    await expect(rows.first().getByRole('link', { name: 'Jobs', exact: true })).toHaveAttribute(
      'href',
      /\/repos\/\d+\/jobs/,
    );
    await expect(rows.first().getByRole('link', { name: 'Executions' })).toHaveAttribute(
      'href',
      /\/executions\?repository=\d+/,
    );
  });

  test('repository pages preserve product scope, filters and pagination', async ({
    page,
  }) => {
    const repositoryRow = (id: number) => ({
      id,
      path: `group/service-${String(id).padStart(3, '0')}`,
      product_id: 7,
      product: 'Scoped product',
      status: 'active',
      last_report_at: '2026-07-28T09:00:00Z',
      jobs_total: 1,
      jobs_failed: 0,
      success_rate: 100,
      job_health_healthy: 1,
      job_health_broken: 0,
      job_health_total: 1,
      coverage: { expected: 1, configured: 1, missing: 0, excluded: 0 },
      href: `/targets/repositories/${id}`,
    });
    const inventory = Array.from({ length: 60 }, (_, index) => (
      repositoryRow(index + 1)
    ));
    await page.route('**/api/ui/repositories**', async (route) => {
      const url = new URL(route.request().url());
      const q = url.searchParams.get('q') ?? '';
      const product = Number(url.searchParams.get('product')) || null;
      const pageSize = Number(url.searchParams.get('page_size')) || 25;
      const requestedPage = Number(url.searchParams.get('page')) || 1;
      const matchingRows = inventory.filter((row) => (
        (!product || row.product_id === product)
        && (!q || `${row.path} ${row.product}`.toLowerCase().includes(q.toLowerCase()))
      ));
      const totalPages = Math.max(1, Math.ceil(matchingRows.length / pageSize));
      const effectivePage = Math.min(requestedPage, totalPages);
      const rows = matchingRows.slice(
        (effectivePage - 1) * pageSize,
        effectivePage * pageSize,
      );
      await route.fulfill({
        json: {
          rows,
          total: 80,
          matching: matchingRows.length,
          returned: rows.length,
          pagination: {
            page: effectivePage,
            page_size: pageSize,
            total_pages: totalPages,
            start: rows.length ? (effectivePage - 1) * pageSize + 1 : 0,
            end: Math.min(effectivePage * pageSize, matchingRows.length),
          },
          filters: { q, product },
        },
      });
    });

    await page.goto(
      '/targets?level=repository&product=7&q=service&page=2&page_size=25',
      { waitUntil: 'domcontentloaded' },
    );
    await expect(page.locator('.repository-row')).toHaveCount(25);
    await expect(page.locator('.repository-row').first()).toContainText('group/service-026');
    await expect(page.getByLabel('Search repositories')).toHaveValue('service');

    await selectSignalOption(
      page,
      page.getByLabel('Repositories per page'),
      '50',
    );
    await expect(page).toHaveURL(/product=7/);
    await expect(page).toHaveURL(/q=service/);
    await expect(page).toHaveURL(/page_size=50/);
    await expect(page).not.toHaveURL(/repository=/);
    await expect(page).not.toHaveURL(/[?&]page=2(?:&|$)/);
    await expect(page.locator('.repository-row')).toHaveCount(50);
    await expect(page.locator('.repository-row').first()).toContainText('group/service-001');

    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page).toHaveURL(/[?&]page=2(?:&|$)/);
    await expect(page.locator('.repository-row')).toHaveCount(10);
    await expect(page.locator('.repository-row').first()).toContainText('group/service-051');
    await page.goBack();
    await expect(page).not.toHaveURL(/[?&]page=2(?:&|$)/);
    await expect(page.locator('.repository-row')).toHaveCount(50);
    await expect(page.locator('.repository-row').first()).toContainText('group/service-001');
  });

  test('repository with no terminal job-key outcome stays neutral', async ({ page }) => {
    await page.route('**/api/ui/repositories**', async (route) => {
      await route.fulfill({
        json: {
          rows: [{
            id: 91,
            path: 'group/nonterminal-only',
            product_id: 7,
            product: 'Neutral product',
            status: 'failed',
            last_report_at: '2026-07-28T09:00:00Z',
            jobs_total: 2,
            jobs_failed: 0,
            success_rate: 0,
            job_health_healthy: 0,
            job_health_broken: 0,
            job_health_total: 0,
            coverage: { expected: 1, configured: 1, missing: 0, excluded: 0 },
            href: '/targets/repositories/91',
          }],
          total: 1,
          matching: 1,
          returned: 1,
          pagination: { page: 1, page_size: 25, total_pages: 1, start: 1, end: 1 },
          filters: { q: '', product: null },
        },
      });
    });

    await page.goto('/targets?level=repository', { waitUntil: 'domcontentloaded' });
    const row = page.locator('.repository-row');
    await expect(row.locator('.repository-stat').first().locator('strong')).toHaveText('—');
    await expect(row.locator('.status')).toHaveText('unknown');
    await expect(row.locator('.status-failed')).toHaveCount(0);
  });

  test('execution lifecycle filter changes the live collection', async ({ page }) => {
    await page.goto('/executions');
    await expect(page.locator('.execution-row')).toHaveCount(25, {
      timeout: 10_000,
    });
    await expectSignalSelectValue(page.getByLabel('Executions per page'), '25');
    await selectSignalOption(page, page.getByLabel('Executions per page'), '50');
    await expect(page).toHaveURL(/page_size=50/);
    await expect(page.locator('.execution-row')).toHaveCount(50);
    await page.locator('.filter-row').getByRole('button', { name: /failed/i }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get('status')).toBe('failed');
    await expect(page).toHaveURL(/page_size=50/);
    await expect(page.locator('.execution-row .status-failed').first()).toBeVisible();
  });

  test('pipeline scope and page size survive deep links and browser history', async ({
    page,
  }) => {
    const requestedQueries: string[] = [];
    await page.route('**/api/ui/pipelines?*', async (route) => {
      const url = new URL(route.request().url());
      requestedQueries.push(url.search);
      const pageSize = url.searchParams.get('page_size') === '50' ? 50 : 25;
      await route.fulfill({
        json: {
          rows: [{
            id: 41,
            pipeline_id: 9001,
            status: 'failed',
            repository_id: 9,
            repository: 'platform/api',
            product_id: 7,
            product: 'Platform',
            ref: 'main',
            commit_sha: 'abcdef123456',
            job_count: 3,
            activity_at: '2026-07-28T09:00:00Z',
            started_at: '2026-07-28T08:58:00Z',
            finished_at: '2026-07-28T09:00:00Z',
            duration_seconds: 120,
            error_message: 'security job failed',
            web_url: 'https://gitlab.example/pipelines/17',
            href: '/targets/repositories/9',
          }],
          counts: {
            needs_attention: 1,
            in_progress: 0,
            completed_recently: 0,
          },
          total: 1,
          matching: 1,
          page: 1,
          page_size: pageSize,
          page_count: 1,
          group: 'needs_attention',
          timeframe: '30d',
          status: 'failed',
          product: Number(url.searchParams.get('product')) || null,
          repository: Number(url.searchParams.get('repository')) || null,
          filter_options: {
            products: [
              { id: 7, name: 'Platform', path: 'platform' },
              { id: 8, name: 'ExampleApp', path: 'example_app' },
            ],
            repositories: [
              { id: 9, path: 'platform/api', product_id: 7 },
              { id: 10, path: 'example_app/api', product_id: 8 },
            ],
          },
        },
      });
    });

    await page.goto(
      '/pipelines?group=needs_attention&status=failed&timeframe=30d'
      + '&product=7&repository=9&page_size=50',
      { waitUntil: 'domcontentloaded' },
    );
    const product = page.getByLabel('Pipeline product');
    const repository = page.getByLabel('Pipeline repository');
    const pageSize = page.getByLabel('Pipelines per page');
    await expectSignalSelectValue(product, '7');
    await expectSignalSelectValue(repository, '9');
    await expectSignalSelectValue(pageSize, '50');
    await expect(page.locator('.lane-facts')).toContainText('Diagnostic');
    await expect(page.locator('.pipeline-axis')).toHaveCount(0);

    await selectSignalOption(page, pageSize, '25');
    await expect(page).not.toHaveURL(/page_size=/);
    await expect.poll(() => (
      new URLSearchParams(requestedQueries.at(-1)).get('page_size')
    )).toBe('25');
    await page.goBack();
    await expectSignalSelectValue(pageSize, '50');
    await expect(page).toHaveURL(/page_size=50/);

    await selectSignalOption(page, product, 'ExampleApp');
    await expectSignalSelectValue(repository, '');
    await expect(page).toHaveURL(/product=8/);
    await expect(page).not.toHaveURL(/repository=/);
    await page.goBack();
    await expectSignalSelectValue(product, '7');
    await expectSignalSelectValue(repository, '9');
  });

  test('keeps the keyboard-active pipeline option stable across a live refresh', async ({
    page,
  }) => {
    await page.clock.install();
    let requestCount = 0;
    await page.route('**/api/ui/pipelines?*', async (route) => {
      requestCount += 1;
      const url = new URL(route.request().url());
      const products = requestCount === 1
        ? [
          { id: 1, name: 'Alpha', path: 'alpha' },
          { id: 2, name: 'Beta', path: 'beta' },
          { id: 3, name: 'Gamma', path: 'gamma' },
        ]
        : [
          { id: 2, name: 'Beta', path: 'beta' },
          { id: 3, name: 'Gamma', path: 'gamma' },
        ];
      await route.fulfill({
        json: {
          rows: [],
          counts: { needs_attention: 0, in_progress: 0, completed_recently: 0 },
          total: 0,
          matching: 0,
          page: 1,
          page_size: 25,
          page_count: 0,
          group: 'in_progress',
          timeframe: '7d',
          status: null,
          product: Number(url.searchParams.get('product')) || null,
          repository: null,
          filter_options: { products, repositories: [] },
        },
      });
    });

    await page.goto('/pipelines?group=in_progress&timeframe=7d', {
      waitUntil: 'domcontentloaded',
    });
    const product = page.getByRole('combobox', { name: 'Pipeline product' });
    await expectSignalSelectValue(product, '');

    await product.press('b');
    let activeId = await product.getAttribute('aria-activedescendant');
    expect(activeId).not.toBeNull();
    await expect(page.locator(`#${activeId}`)).toHaveText('Beta');

    await page.clock.runFor(30_001);
    await expect.poll(() => requestCount).toBeGreaterThanOrEqual(2);
    await expect(page.getByRole('listbox', { name: 'Pipeline product' }).getByRole('option')).toHaveCount(3);
    activeId = await product.getAttribute('aria-activedescendant');
    expect(activeId).not.toBeNull();
    await expect(page.locator(`#${activeId}`)).toHaveText('Beta');

    await product.press('Enter');
    await expectSignalSelectValue(product, '2');
  });

  test('pipeline status-only links derive lifecycle while incompatible pairs stay empty', async ({
    page,
  }) => {
    const requestedQueries: string[] = [];
    await page.route('**/api/ui/pipelines?*', async (route) => {
      const url = new URL(route.request().url());
      requestedQueries.push(url.search);
      const group = url.searchParams.get('group') ?? 'in_progress';
      const status = url.searchParams.get('status');
      const incompatible = group === 'in_progress' && status === 'failed';
      await route.fulfill({
        json: {
          rows: incompatible ? [] : [{
            id: 91,
            pipeline_id: 9001,
            status: 'failed',
            repository_id: 9,
            repository: 'platform/api',
            product_id: 7,
            product: 'Platform',
            ref: 'main',
            commit_sha: 'abcdef123456',
            job_count: 3,
            activity_at: '2026-07-28T09:00:00Z',
            started_at: '2026-07-28T08:58:00Z',
            finished_at: '2026-07-28T09:00:00Z',
            duration_seconds: 120,
            error_message: 'security job failed',
            web_url: 'https://gitlab.example/pipelines/9001',
            href: '/targets/repositories/9',
          }],
          counts: {
            needs_attention: 1,
            in_progress: 51,
            completed_recently: 0,
          },
          total: 52,
          matching: incompatible ? 0 : 1,
          page: 1,
          page_size: 25,
          page_count: 1,
          group,
          timeframe: '7d',
          status,
          product: null,
          repository: null,
          filter_options: {
            products: [],
            repositories: [],
          },
        },
      });
    });

    await page.goto('/pipelines?status=failed', {
      waitUntil: 'domcontentloaded',
    });
    await expect.poll(() => {
      const query = new URLSearchParams(requestedQueries.at(-1));
      return [query.get('group'), query.get('status')];
    }).toEqual(['needs_attention', 'failed']);
    await expect(
      page.getByRole('button', { name: /Needs attention/ }),
    ).toHaveClass(/active/);
    await expectSignalSelectValue(page.getByLabel('Pipeline status'), 'failed');
    await expect(page.locator('.pipeline-lane')).toHaveCount(1);
    await expect(page.getByRole('link', { name: /Open pipeline detail for #9001 for platform\/api/ })).toHaveAttribute(
      'href',
      '/pipelines/91',
    );
    await expect(page.locator('.pipeline-external-link')).toHaveAttribute(
      'href',
      'https://gitlab.example/pipelines/9001',
    );
    await expect(page.locator('.worker-orbit')).toHaveCount(0);
    await expect(page.locator('.pipeline-register-columns')).toHaveCount(1);
    await expect(page.locator('.pipeline-register-columns')).toContainText(
      'Observed facts',
    );
    await expect(page.locator('.queue-panel')).toHaveCount(0);

    const desktopGeometry = await page.evaluate(() => {
      const workbench = document.querySelector('.pipeline-workbench');
      const board = document.querySelector('.pipeline-board');
      if (!workbench || !board) return null;
      const workbenchBox = workbench.getBoundingClientRect();
      const boardBox = board.getBoundingClientRect();
      return {
        workbenchWidth: workbenchBox.width,
        boardWidth: boardBox.width,
        overflow: document.documentElement.scrollWidth - window.innerWidth,
      };
    });
    expect(desktopGeometry).not.toBeNull();
    expect(desktopGeometry!.boardWidth).toBeGreaterThanOrEqual(
      desktopGeometry!.workbenchWidth - 2,
    );
    expect(desktopGeometry!.overflow).toBeLessThanOrEqual(1);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.locator('.live-state-loading')).toHaveCount(0, {
      timeout: 10_000,
    });
    await expect(page.locator('.pipeline-register-columns')).toBeHidden();
    await expect(page.locator('.pipeline-lane')).toHaveCount(1);
    await expect(page.locator('.pipeline-lane-link')).toHaveAttribute('href', /\/pipelines\/\d+/);
    const mobileGeometry = await page.evaluate(() => ({
      pageOverflow: document.documentElement.scrollWidth - window.innerWidth,
      laneOverflow: Array.from(document.querySelectorAll('.pipeline-lane')).some(
        (lane) => lane.scrollWidth > lane.clientWidth + 1,
      ),
    }));
    expect(mobileGeometry.pageOverflow).toBeLessThanOrEqual(1);
    expect(mobileGeometry.laneOverflow).toBe(false);

    requestedQueries.length = 0;
    await page.goto('/pipelines?group=in_progress&status=failed', {
      waitUntil: 'domcontentloaded',
    });
    await expect.poll(() => {
      const query = new URLSearchParams(requestedQueries.at(-1));
      return [query.get('group'), query.get('status')];
    }).toEqual(['in_progress', 'failed']);
    await expect(
      page.getByRole('button', { name: /In progress/ }),
    ).toHaveClass(/active/);
    await expectSignalSelectValue(page.getByLabel('Pipeline status'), 'failed');
    await expect(page.getByRole('status')).toContainText(
      'No pipelines are in progress',
    );
    await expect(page.locator('.pipeline-lane')).toHaveCount(0);
  });

  test('affected filter surfaces use one control height and labelled slots', async ({
    page,
  }) => {
    await page.goto('/pipelines', { waitUntil: 'domcontentloaded' });
    const pipelineControls = page.locator(
      '.pipeline-filter-grid .signal-select-trigger',
    );
    await expect(pipelineControls).toHaveCount(5);
    const pipelineHeights = await pipelineControls.evaluateAll((controls) =>
      controls.map((control) => Math.round(control.getBoundingClientRect().height)),
    );
    expect(new Set(pipelineHeights).size).toBe(1);

    await page.goto('/ptai-scans', { waitUntil: 'domcontentloaded' });
    const ptaiFields = [
      page.getByLabel('PT AI status'),
      page.getByLabel('PT AI repository'),
      page.getByLabel('PT AI branch'),
      page.locator('.filter-toggle'),
    ];
    await expect(page.locator('.ptai-filter-bar .signal-search-input')).toHaveCount(2);
    await expect(page.locator('.ptai-filter-bar input:not([type="checkbox"])')).toHaveCount(2);
    const ptaiBoxes = await Promise.all(
      ptaiFields.map((field) => field.boundingBox()),
    );
    expect(ptaiBoxes.every(Boolean)).toBe(true);
    expect(new Set(ptaiBoxes.map((box) => Math.round(box!.height))).size).toBe(1);
    await expect(page.locator('.filter-toggle-field > span').first()).toHaveText(
      'Attention',
    );

    await page.goto('/logs?status=failed', { waitUntil: 'domcontentloaded' });
    const jobControls = page.locator('.journal-filters .signal-search-input, .journal-filters .signal-select-trigger');
    await expect(jobControls).toHaveCount(4);
    await expect(page.locator('.journal-filters select')).toHaveCount(0);
    const jobHeights = await jobControls.evaluateAll((controls) =>
      controls.map((control) => Math.round(control.getBoundingClientRect().height)),
    );
    expect(new Set(jobHeights).size).toBe(1);
    await expectSignalSelectValue(page.getByLabel('Job status'), 'failed');
    await expectSignalSelectValue(page.getByLabel('Job timeframe'), '7d');

    const statusControl = page.getByRole('combobox', { name: 'Job status' });
    await statusControl.click();
    const statusMenu = page.getByRole('listbox', { name: 'Job status' });
    await expect(statusMenu).toBeVisible();
    const [statusBox, statusMenuBox] = await Promise.all([
      statusControl.boundingBox(),
      statusMenu.boundingBox(),
    ]);
    expect(Math.round(statusMenuBox!.width)).toBe(Math.round(statusBox!.width));
    await statusControl.press('Escape');

    await page.goto('/next?view=repositories', { waitUntil: 'domcontentloaded' });
    const repositorySearch = page.getByRole('combobox', { name: 'Search repositories' });
    await expect(repositorySearch).toHaveClass(/signal-search-input/);
    await repositorySearch.fill('service-002');
    await repositorySearch.press('Enter');
    await expect(page).toHaveURL(/q=service-002/);
  });

  test('keeps the keyboard-active option visible in long filter menus', async ({
    page,
  }) => {
    await page.goto('/pipelines', { waitUntil: 'domcontentloaded' });
    const control = page.getByRole('combobox', { name: 'Pipeline product' });
    await control.click();
    const listbox = page.getByRole('listbox', { name: 'Pipeline product' });
    await expect(listbox).toBeVisible();
    await expect.poll(() => listbox.getByRole('option').count()).toBeGreaterThan(6);

    await control.press('End');
    await expect.poll(async () => {
      const activeId = await control.getAttribute('aria-activedescendant');
      if (!activeId) return false;
      return page.locator(`#${activeId}`).evaluate((option) => {
        const optionRect = option.getBoundingClientRect();
        const menuRect = option.parentElement!.getBoundingClientRect();
        return optionRect.top >= menuRect.top && optionRect.bottom <= menuRect.bottom;
      });
    }).toBe(true);
  });

  test('job journal filters remain URL-addressable and restore through browser history', async ({
    page,
  }) => {
    await page.goto('/logs?status=failed', { waitUntil: 'domcontentloaded' });

    await page.getByRole('button', { name: 'SCA', exact: true }).click();
    await expect(page).toHaveURL(/category=sca/);
    await expect(page).toHaveURL(/status=failed/);

    await selectSignalOption(page, page.getByLabel('Job timeframe'), 'Last 30 days');
    await expect(page).toHaveURL(/timeframe=30d/);

    const jobControl = page.getByRole('combobox', { name: 'Job name', exact: true });
    const job = 'sca_scan';
    await jobControl.fill('sca');
    await expect(page).toHaveURL(/job=sca/);
    await page.getByPlaceholder('Filter by repository').fill(
      'qa/product-01/service-002',
    );
    await selectSignalOption(page, jobControl, job);
    await expect(page).toHaveURL(/repo=qa%2Fproduct-01%2Fservice-002/);
    await expect(page).toHaveURL(new RegExp(`job=${encodeURIComponent(job!)}`));

    await selectSignalOption(page, page.getByLabel('Job status'), 'All');
    await expect(page).not.toHaveURL(/status=/);
    await page.goBack();
    await expectSignalSelectValue(page.getByLabel('Job status'), 'failed');
    await expect(page.getByPlaceholder('Filter by repository')).toHaveValue(
      'qa/product-01/service-002',
    );
  });

  test('job journal canonicalizes malformed filter bookmarks', async ({ page }) => {
    await page.route('**/api/ui/job-journal?*', async (route) => {
      await route.fulfill({
        json: {
          rows: [],
          summary: {
            matching: 0,
            returned: 0,
            duration_samples: 0,
            median_duration_seconds: null,
            oldest_age_seconds: null,
            oldest_finished_at: null,
            successful: 0,
            failed: 0,
            terminal: 0,
            success_rate: null,
          },
          pagination: { page: 1, page_size: 20, total_pages: 1, start: 0, end: 0 },
          filters: {
            repo: '',
            job: '',
            category: 'all',
            status: 'all',
            timeframe: '7d',
            page: 1,
            repositories: [],
            jobs: [],
          },
        },
      });
    });
    await page.goto(
      '/logs?category=garbage&timeframe=garbage&status=garbage&page=0',
      { waitUntil: 'domcontentloaded' },
    );

    await expect(page).toHaveURL(/\/logs$/);
    await expect(
      page.getByRole('button', { name: 'All', exact: true }).first(),
    ).toHaveAttribute('aria-pressed', 'true');
    await expectSignalSelectValue(page.getByLabel('Job status'), 'all');
    await expectSignalSelectValue(page.getByLabel('Job timeframe'), '7d');
  });

  test('searchable job options keep unique ids for empty values', async ({ page }) => {
    await page.route('**/api/ui/job-journal**', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      payload.filters.jobs = ['empty'];
      await route.fulfill({ response, json: payload });
    });
    const responsePromise = page.waitForResponse('**/api/ui/job-journal**');
    await page.goto('/logs', { waitUntil: 'domcontentloaded' });
    await responsePromise;
    const jobControl = page.getByRole('combobox', { name: 'Job name', exact: true });
    await expect(jobControl).toBeVisible();
    await jobControl.press('ArrowDown');
    const ids = await page.getByRole('option', { name: /All jobs|empty/ }).evaluateAll(
      (options) => options.map((option) => option.id),
    );
    expect(ids).toHaveLength(2);
    expect(new Set(ids).size).toBe(ids.length);
  });

  test('global health chrome reports degraded state and its reason', async ({
    page,
  }) => {
    await page.route('**/api/health', async (route) => {
      await route.fulfill({
        json: {
          status: 'healthy',
          operational_status: 'degraded',
          degraded_reasons: ['worker heartbeat stale: iac_worker'],
        },
      });
    });

    await page.goto('/next?view=overview', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.environment')).toContainText(
      'INTERNAL · DEGRADED',
    );
    await expect(page.locator('.environment')).not.toContainText('LIVE');
    await expect(page.locator('.runtime')).toContainText('ScanOps degraded');
    await expect(page.locator('.runtime .health-detail')).toHaveText(
      'iac worker heartbeat stale',
    );
  });

  test('global health chrome does not keep stale LIVE data after a refresh error', async ({
    page,
  }) => {
    let failHealth = false;
    await page.clock.install();
    await page.route('**/api/health', async (route) => {
      if (failHealth) {
        await route.abort('failed');
        return;
      }
      await route.fulfill({
        json: {
          status: 'healthy',
          operational_status: 'healthy',
          degraded_reasons: [],
        },
      });
    });

    await page.goto('/next?view=overview', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.environment')).toContainText(
      'INTERNAL · LIVE',
    );
    failHealth = true;
    await page.clock.runFor(15_001);
    await expect(page.locator('.environment')).toContainText(
      'INTERNAL · UNAVAILABLE',
    );
  });

  test('overview marks preserved metrics as last-known after its refresh fails', async ({
    page,
  }) => {
    let failOverview = false;
    await page.clock.install();
    await page.route('**/api/ui/overview*', async (route) => {
      if (failOverview) {
        await route.abort('failed');
        return;
      }
      await route.continue();
    });

    await page.goto('/next?view=overview', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.signal-state')).toContainText('Live');
    failOverview = true;
    await page.clock.runFor(30_001);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'Signal is unavailable.',
    );
    await expect(page.locator('.signal-state')).toHaveClass(/signal-offline/);
    await expect(page.locator('.signal-state')).toContainText('Offline');
    await expect(page.locator('.signal-panel .panel-label')).toContainText(
      'LAST KNOWN',
    );
    await expect(page.locator('.signal-panel .sync-note')).toContainText(
      'Connection failed',
    );
  });

  test('healthy coverage states use truthful callouts', async ({
    page,
  }) => {
    await page.route('**/api/ui/coverage', async (route) => {
      await route.fulfill({
        json: {
          summary: {
            covered: 1,
            pipeline_no_sec: 0,
            no_pipelines: 0,
            inaccessible: 0,
            excluded: 0,
            controls_expected: 3,
            controls_configured: 3,
            control_rate: 100,
          },
          rows: [{
            product_id: 1,
            product: 'Fully covered',
            repositories: 1,
            audit_covered: 1,
            audit_total: 1,
            controls_expected: 3,
            controls_configured: 3,
            controls_missing: 0,
            last_scanned_at: '2026-07-28T09:00:00Z',
            href: '/coverage?product=1',
          }],
        },
      });
    });
    await page.goto('/next?view=coverage', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.coverage-gap')).toContainText(
      'No product gaps',
    );
    await expect(page.locator('.coverage-gap a')).toHaveCount(0);
    await expect(page.locator('.coverage-pulse')).toContainText(
      '1 of 1 repositories observed',
    );
    await expect(page.locator('.coverage-matrix-heading')).toContainText(
      'Audit is counted per repository',
    );
    await expect(page.locator('.coverage-matrix')).toContainText(
      '3 / 3 bindings',
    );
  });

  test('repository-scoped executions preserve scope and can return to all executions', async ({
    page,
  }) => {
    const requestedRepositories: Array<string | null> = [];
    await page.route('**/api/ui/executions?*', async (route) => {
      const repository = new URL(route.request().url()).searchParams.get(
        'repository',
      );
      requestedRepositories.push(repository);
      await route.fulfill({
        json: {
          rows: [],
          counts: { all: 0, running: 0, failed: 0, succeeded: 0 },
          repository_filter: repository ? Number(repository) : null,
        },
      });
    });

    await page.goto('/executions?repository=17&page=2&page_size=50', {
      waitUntil: 'domcontentloaded',
    });
    await expect(page.locator('.execution-scope')).toContainText(
      'Repository #17',
    );
    expect(requestedRepositories).toContain('17');

    await page.getByRole('button', { name: 'All executions' }).click();
    await expect(page).not.toHaveURL(/repository=/);
    await expect(page).not.toHaveURL(/[?&]page=2(?:&|$)/);
    await expect(page).toHaveURL(/page_size=50/);
    await expect(page.locator('.execution-scope')).toHaveCount(0);
    await expect.poll(() => requestedRepositories.at(-1)).toBeNull();
  });

  test('execution status bookmarks, controls and browser history stay synchronized', async ({
    page,
  }) => {
    const requestedStatuses: string[] = [];
    await page.route('**/api/ui/executions?*', async (route) => {
      const status = new URL(route.request().url()).searchParams.get('status') ?? 'all';
      requestedStatuses.push(status);
      await route.fulfill({
        json: {
          rows: [],
          counts: { all: 0, running: 0, failed: 0, succeeded: 0 },
          repository_filter: null,
        },
      });
    });

    await page.goto('/executions?status=failed', { waitUntil: 'domcontentloaded' });
    await expect(
      page.getByRole('button', { name: /^failed\s+0$/i }),
    ).toHaveAttribute('aria-pressed', 'true');
    await expect.poll(() => requestedStatuses.at(-1)).toBe('failed');

    await page.getByRole('button', { name: /^running\s+0$/i }).click();
    await expect(page).toHaveURL(/status=running/);
    await expect.poll(() => requestedStatuses.at(-1)).toBe('running');

    await page.goBack();
    await expect(page).toHaveURL(/status=failed/);
    await expect(
      page.getByRole('button', { name: /^failed\s+0$/i }),
    ).toHaveAttribute('aria-pressed', 'true');
    await expect.poll(() => requestedStatuses.at(-1)).toBe('failed');
  });

  test('Config Guard exposes every finding through the shared bottom pager', async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 900 });
    const findings = Array.from({ length: 64 }, (_, index) => ({
      id: index + 1,
      run_id: 9,
      project_path: `group/service-${String(index + 1).padStart(2, '0')}`,
      severity: index % 2 ? 'medium' : 'high',
      rule_id: `CFG-${String(index + 1).padStart(3, '0')}`,
      title: `Configuration finding ${index + 1}`,
      description: null,
      recommendation: null,
      href: `/config-scan/findings/${index + 1}`,
    }));
    await page.route('**/api/ui/config-guard?*', async (route) => {
      const url = new URL(route.request().url());
      const pageSize = Number(url.searchParams.get('page_size')) || 25;
      const requestedPage = Number(url.searchParams.get('page')) || 1;
      const totalPages = Math.ceil(findings.length / pageSize);
      const effectivePage = Math.min(requestedPage, totalPages);
      const rows = findings.slice(
        (effectivePage - 1) * pageSize,
        effectivePage * pageSize,
      );
      await route.fulfill({
        json: {
          active_run_id: null,
          selected_run_id: 9,
          selected_finding: findings[0],
          summary: { products_scanned: 1, open_findings: 64, retained_findings: 64 },
          runs: [{ id: 9, product: 'Platform', status: 'succeeded', scope_path: 'group', total_projects: 64, scanned_projects: 64, findings_count: 64, duration_seconds: 90, href: '/config-scan/runs/9' }],
          findings: rows,
          severity: { high: 32, medium: 32 },
          active: 0,
          pagination: {
            page: effectivePage,
            page_size: pageSize,
            total_pages: totalPages,
            start: (effectivePage - 1) * pageSize + 1,
            end: Math.min(effectivePage * pageSize, findings.length),
          },
        },
      });
    });

    await page.goto('/config-scan', { waitUntil: 'domcontentloaded' });
    const pageSizeTrigger = page.getByLabel('Config Guard findings per page');
    const triggerBox = await pageSizeTrigger.boundingBox();
    expect(triggerBox).not.toBeNull();
    expect(triggerBox!.width).toBeGreaterThanOrEqual(44);
    expect(triggerBox!.height).toBeGreaterThanOrEqual(44);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(768);

    await page.setViewportSize({ width: 390, height: 844 });
    const compactTriggerBox = await pageSizeTrigger.boundingBox();
    expect(compactTriggerBox).not.toBeNull();
    expect(compactTriggerBox!.width).toBeGreaterThanOrEqual(44);
    expect(compactTriggerBox!.height).toBeGreaterThanOrEqual(44);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(390);
    await expect(page.locator('.finding-table > a')).toHaveCount(25);
    await expect(page.locator('.finding-table .surface-heading')).toContainText('1–25 of 64');
    await expect(page.locator('.baseline-panel .policy-layer').last().locator('i > b')).toHaveAttribute('style', 'width: 50%;');
    await selectSignalOption(page, page.getByLabel('Config Guard findings per page'), '50');
    await expect(page).toHaveURL(/page_size=50/);
    await expect(page.locator('.finding-table > a')).toHaveCount(50);
    await page.getByRole('button', { name: 'Next page' }).click();
    await expect(page).toHaveURL(/[?&]page=2(?:&|$)/);
    await expect(page.locator('.finding-table > a')).toHaveCount(14);
    await expect(page.locator('.finding-table .surface-heading')).toContainText('51–64 of 64');
    await expect(page.locator('.baseline-panel .policy-layer').last().locator('i > b')).toHaveAttribute('style', 'width: 50%;');
  });

  test('PT AI distinguishes the current page from matching and total counts and selects is_active', async ({
    page,
  }) => {
    const scan = {
      product: null,
      branch: 'main',
      status: 'running',
      trigger_failed: false,
      ptai_stage: 'analysis',
      progress: 42,
      has_errors: false,
      error: null,
      created_at: '2026-07-28T09:00:00Z',
      updated_at: '2026-07-28T09:05:00Z',
      finished_at: null,
      job_web_url: 'https://gitlab.example/jobs/42',
      asoc_repository_url: 'https://asoc.example/repositories/17',
      ptai_result_url: 'https://ptai.example/results/99',
      href: '/ptai-scans/1',
    };
    await page.route('**/api/ui/ptai?*', async (route) => {
      const pageSize = Number(new URL(route.request().url()).searchParams.get('page_size')) || 25;
      await route.fulfill({
        json: {
          summary: { total: 230, returned: 5, active: 116 },
          matching: 100,
          pagination: {
            page: 1,
            page_size: pageSize,
            total_pages: 4,
            start: 1,
            end: 2,
          },
          filters: {
            status: 'ALL',
            repo: '',
            branch: '',
            problem: false,
          },
          rows: [
            { ...scan, id: 1, repository: 'group/not-active', is_active: false },
            { ...scan, id: 2, repository: 'group/selected-active', is_active: true },
            { ...scan, id: 3, repository: 'group/active-with-errors', is_active: true, has_errors: true, progress: 67 },
            { ...scan, id: 4, repository: 'group/trigger-failed', is_active: false, trigger_failed: true, status: 'TRIGGER_FAILED', progress: null },
            { ...scan, id: 5, repository: 'group/failed-engine', is_active: false, status: 'ERROR', ptai_stage: 'Failed', finished_at: '2026-07-28T09:06:00Z', progress: 81 },
          ],
        },
      });
    });

    await page.goto('/next?view=ptai', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.header-actions .header-evidence')).toContainText(
      '100 matching · 230 total',
    );
    await expect(page.locator('.scan-queue .surface-heading')).toContainText(
      '5 shown',
    );
    await expect(page.locator('.ptai-register-row')).toHaveCount(5);
    await expect(page.locator('.ptai-register-row').first()).toContainText('42%');
    await expect(page.getByRole('link', { name: 'Open GitLab job for scan 1' }).first()).toHaveAttribute(
      'href',
      'https://gitlab.example/jobs/42',
    );
    await expect(page.getByRole('link', { name: 'Open ASOC repository for scan 1' }).first()).toHaveAttribute(
      'href',
      'https://asoc.example/repositories/17',
    );
    await expect(page.getByRole('link', { name: 'Open PT AI result for scan 1' }).first()).toHaveAttribute(
      'href',
      'https://ptai.example/results/99',
    );
    await expect(page.locator('.ptai-register .ptai-scan-select').filter({
      hasText: 'not-active',
    })).toHaveAttribute('href', '/ptai-scans/1');
    await expect(page.locator('.ptai-register-row').filter({ hasText: 'active-with-errors' })).toContainText('67%');
    await expect(page.locator('.ptai-register-row').filter({ hasText: 'active-with-errors' })).not.toContainText('100%');
    await expect(page.locator('.ptai-register-row').filter({ hasText: 'trigger-failed' })).not.toContainText('100%');
    await expect(page.locator('.ptai-register-row').filter({ hasText: 'failed-engine' })).not.toContainText('100%');
    await expectSignalSelectValue(page.getByLabel('PT AI scans per page'), '25');
    await selectSignalOption(page, page.getByLabel('PT AI scans per page'), '50');
    await expect(page).toHaveURL(/page_size=50/);
    await expectSignalSelectValue(page.getByLabel('PT AI scans per page'), '50');
  });

  test('PT AI reports terminal scans with engine errors as completed with warnings, not zero percent', async ({
    page,
  }) => {
    await page.route('**/api/ui/ptai?*', async (route) => {
      await route.fulfill({
        json: {
          summary: { total: 1, returned: 1, active: 0 },
          matching: 1,
          pagination: { page: 1, page_size: 25, total_pages: 1, start: 1, end: 1 },
          filters: { status: 'ALL', repo: '', branch: '', problem: false },
          rows: [{
            id: 3209,
            repository: 'sandbox/sample-ledger/sample-ledger-retry',
            product: null,
            branch: 'release',
            status: 'SUCCESS',
            trigger_failed: false,
            is_active: false,
            ptai_stage: 'Done',
            progress: 0,
            has_errors: true,
            error: 'No source code for file /tmp/ai/.gitkeep',
            created_at: '2026-08-03T17:07:52Z',
            updated_at: '2026-08-03T17:10:05Z',
            finished_at: '2026-08-03T17:08:13Z',
            job_web_url: null,
            href: '/ptai-scans/3209',
          }],
        },
      });
    });

    await page.goto('/next?view=ptai', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.ptai-register-row')).toContainText('100%');
    await expect(page.locator('.scan-queue-list')).toContainText('Alert');
  });

  test('PT AI register keeps evidence links inside the mobile viewport', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'desktop-1440', 'The register mobile contract runs once from the supported desktop suite.');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.route('**/api/ui/ptai?*', async (route) => {
      await route.fulfill({
        json: {
          summary: { total: 1, returned: 1, active: 1 }, matching: 1,
          pagination: { page: 1, page_size: 25, total_pages: 1, start: 1, end: 1 },
          filters: { status: 'ALL', repo: '', branch: '', problem: false },
          rows: [{ id: 1, repository: 'group/repo', product: null, branch: 'main', status: 'IN_PROGRESS', trigger_failed: false, is_active: true, ptai_stage: 'analysis', progress: 42, has_errors: false, error: null, created_at: '2026-07-28T09:00:00Z', updated_at: '2026-07-28T09:05:00Z', finished_at: null, job_web_url: 'https://gitlab.example/jobs/42', asoc_repository_url: 'https://asoc.example/repositories/17', ptai_result_url: 'https://ptai.example/results/99', href: '/ptai-scans/1' }],
        },
      });
    });
    await page.goto('/next?view=ptai', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.ptai-register-row')).toHaveCount(1);
    await expect(page.getByRole('link', { name: 'Open GitLab job for scan 1' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open ASOC repository for scan 1' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open PT AI result for scan 1' })).toBeVisible();
    await expect.poll(async () => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(392);
  });

  test('IaC labels an active run without presenting empty evidence as resolved', async ({
    page,
  }) => {
    await page.route('**/api/ui/iac', async (route) => {
      await route.fulfill({
        json: {
          selected_run_id: 7,
          summary: {
            repositories: 0,
            findings: 0,
            critical: 0,
            high: 0,
            medium: 0,
            low: 0,
            info: 0,
          },
          runs: [{
            id: 7,
            status: 'running',
            target: 'all repositories',
            scanner: 'checkov',
            scanned_repositories: 0,
            uploaded_repositories: 0,
            findings_count: 0,
            duration_seconds: null,
            href: '/iac/runs/7',
          }],
          repositories: [],
          findings: [],
        },
      });
    });

    await page.goto('/next?view=iac', { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'Infrastructure, scanning.',
    );
    await expect(page.locator('.live-state-progress')).toContainText(
      'IaC run #7 is in progress',
    );
    await expect(page.locator('.iac-grid')).toHaveCount(0);
  });

  test('IaC keeps an active run visible while showing completed evidence', async ({
    page,
  }) => {
    await page.route('**/api/ui/iac', async (route) => {
      await route.fulfill({
        json: {
          active_run_id: 9,
          latest_run_id: 9,
          selected_run_id: 7,
          summary: {
            repositories: 1,
            findings: 0,
            critical: 0,
            high: 0,
            medium: 0,
            low: 0,
            info: 0,
          },
          runs: [
            {
              id: 9,
              status: 'running',
              target: 'all repositories',
              scanner: 'checkov',
              scanned_repositories: 3,
              uploaded_repositories: 1,
              findings_count: 0,
              duration_seconds: null,
              href: '/iac/runs/9',
            },
            {
              id: 7,
              status: 'succeeded',
              target: 'all repositories',
              scanner: 'checkov',
              scanned_repositories: 1,
              uploaded_repositories: 1,
              findings_count: 0,
              duration_seconds: 120,
              href: '/iac/runs/7',
            },
          ],
          repositories: [{
            id: 1,
            run_id: 7,
            project_path: 'group/repository',
            scan_status: 'succeeded',
            upload_status: 'uploaded',
            findings_count: 0,
            critical: 0,
            high: 0,
            medium: 0,
            low: 0,
            href: '/iac/repositories/1',
          }],
          findings: [],
        },
      });
    });

    await page.goto('/next?view=iac', { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'Infrastructure, scanning.',
    );
    await expect(page.locator('.run-context')).toContainText(
      'IaC run #9 is running',
    );
    await expect(page.locator('.module-map')).toContainText(
      '1 OF 1 SHOWN',
    );
  });

  test('IaC completed evidence with severe findings still needs attention', async ({
    page,
  }) => {
    await page.route('**/api/ui/iac', async (route) => {
      await route.fulfill({
        json: {
          active_run_id: null,
          latest_run_id: 7,
          selected_run_id: 7,
          summary: {
            repositories: 1,
            findings: 1,
            critical: 1,
            high: 0,
            medium: 0,
            low: 0,
            info: 0,
          },
          runs: [{
            id: 7,
            status: 'succeeded',
            target: 'all repositories',
            scanner: 'checkov',
            scanned_repositories: 1,
            uploaded_repositories: 1,
            findings_count: 1,
            duration_seconds: 120,
            href: '/iac/runs/7',
          }],
          repositories: [{
            id: 1,
            run_id: 7,
            project_path: 'group/repository',
            scan_status: 'succeeded',
            upload_status: 'uploaded',
            findings_count: 1,
            critical: 1,
            high: 0,
            medium: 0,
            low: 0,
            href: '/iac/repositories/1',
          }],
          findings: [{
            id: 1,
            run_id: 7,
            project_path: 'group/repository',
            severity: 'critical',
            rule_id: 'CKV-1',
            title: 'Critical policy finding',
            file_path: 'main.tf',
            line: 1,
            href: '/iac/repositories/1',
          }],
        },
      });
    });

    await page.goto('/next?view=iac', { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'Infrastructure needs attention.',
    );
  });

  test('worker-only scanner fleet preserves worker health and worker labels', async ({
    page,
  }, testInfo) => {
    await page.route('**/api/ui/scanner-fleet', async (route) => {
      await route.fulfill({
        json: {
          summary: {
            source: 'worker_heartbeats',
            total: 2,
            returned: 2,
            healthy: 1,
          },
          connectors: [],
          instances: [],
          workers: [
            {
              name: 'pipeline_worker',
              last_beat_at: '2026-07-28T09:05:00Z',
              status: 'healthy',
              stale: false,
              age_seconds: 5,
            },
            {
              name: 'iac_worker',
              last_beat_at: '2026-07-28T08:00:00Z',
              status: 'stale',
              stale: true,
              age_seconds: 3900,
            },
          ],
        },
      });
    });

    await page.goto('/next?view=scanners', { waitUntil: 'domcontentloaded' });
    const fleetCore = page.locator('.fleet-core');
    const fleetMeta = fleetCore.locator('.fleet-core-meta');
    await expect(fleetMeta).toContainText('0 connectors');
    await expect(fleetMeta).toContainText('2 runtimes');
    await expect(page.locator('.fleet-sweep')).toHaveCount(1);
    const scanMotion = await page.locator('.fleet-field').evaluate((field) => {
      const sweep = field.querySelector('.fleet-sweep');
      const nodes = [...field.querySelectorAll('.fleet-node')];
      if (!sweep) throw new Error('Fleet sweep is missing');
      return {
        sweepName: getComputedStyle(sweep).animationName,
        sweepDuration: getComputedStyle(sweep).animationDuration,
        nodeDelays: nodes.map((node) => getComputedStyle(node, '::before').animationDelay),
        haloNames: nodes.map((node) => getComputedStyle(node, '::before').animationName),
        signalNames: nodes.map((node) => getComputedStyle(node.querySelector('i')!).animationName),
        labelNames: nodes.map((node) => getComputedStyle(node.querySelector('span')!).animationName),
      };
    });
    expect(scanMotion.sweepName).toBe('fleetSweep');
    expect(scanMotion.sweepDuration).toBe('8s');
    expect(new Set(scanMotion.nodeDelays).size).toBe(2);
    expect(scanMotion.haloNames).toEqual(['fleetNodeHalo', 'fleetNodeHalo']);
    expect(scanMotion.signalNames).toEqual(['fleetNodeSignal', 'fleetNodeSignal']);
    expect(scanMotion.labelNames).toEqual(['fleetNodeLabel', 'fleetNodeLabel']);
    const pulseState = await page.locator('.fleet-field').evaluate((field) => {
      const firstNode = field.querySelector('.fleet-node');
      if (!firstNode) throw new Error('Fleet node is missing');
      const delayMs = Number.parseFloat(
        getComputedStyle(firstNode, '::before').animationDelay,
      ) * 1000;
      const pulseTime = delayMs + 320;
      field.getAnimations({ subtree: true }).forEach((animation) => {
        animation.pause();
        animation.currentTime = pulseTime;
      });
      return {
        haloOpacity: Number.parseFloat(
          getComputedStyle(firstNode, '::before').opacity,
        ),
        signalTransform: getComputedStyle(firstNode.querySelector('i')!).transform,
        labelShadow: getComputedStyle(firstNode.querySelector('span')!).textShadow,
      };
    });
    expect(pulseState.haloOpacity).toBeGreaterThan(0.75);
    expect(pulseState.signalTransform).not.toBe('none');
    expect(pulseState.labelShadow).not.toBe('none');
    const pulseScreenshot = testInfo.outputPath('scanner-fleet-scan-pulse.png');
    await page.locator('.fleet-field').screenshot({ path: pulseScreenshot });
    await testInfo.attach('scanner-fleet-scan-pulse', {
      path: pulseScreenshot,
      contentType: 'image/png',
    });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(390);
    const mobilePulseScreenshot = testInfo.outputPath(
      'scanner-fleet-scan-pulse-390.png',
    );
    await page.locator('.fleet-field').screenshot({
      path: mobilePulseScreenshot,
    });
    await testInfo.attach('scanner-fleet-scan-pulse-390', {
      path: mobilePulseScreenshot,
      contentType: 'image/png',
    });
    const [coreBox, metaBox] = await Promise.all([
      fleetCore.boundingBox(),
      fleetMeta.boundingBox(),
    ]);
    expect(coreBox).not.toBeNull();
    expect(metaBox).not.toBeNull();
    expect(metaBox!.x).toBeGreaterThanOrEqual(coreBox!.x);
    expect(metaBox!.x + metaBox!.width).toBeLessThanOrEqual(
      coreBox!.x + coreBox!.width,
    );
    await expect(page.locator('.fleet-map h2')).toHaveText(
      '2 of 2 runtimes mapped',
    );
    await expect(page.locator('.fleet-summary')).toContainText(
      'runtimes · all',
    );
    await expect(page.locator('.worker-table')).toContainText(
      'RUNTIME REGISTRY',
    );
    await expect(page.locator('.worker-table .status-succeeded')).toHaveCount(1);
    await expect(page.locator('.worker-table .status-partial')).toHaveCount(1);
    await expect(
      page.locator('.worker-table > .runtime-succeeded').filter({
        hasText: 'pipeline_worker',
      }),
    ).toHaveCount(1);
    await expect(
      page.locator('.worker-table > .runtime-partial').filter({
        hasText: 'iac_worker',
      }),
    ).toHaveCount(1);
  });

  test('scanner fleet combines instances and standalone workers without duplicates', async ({
    page,
  }) => {
    await page.route('**/api/ui/scanner-fleet', async (route) => {
      await route.fulfill({
        json: {
          summary: {
            source: 'scanner_instances',
            total: 2,
            returned: 2,
            healthy: 1,
          },
          connectors: [
            {
              id: 1,
              key: 'gitlab',
              name: 'GitLab',
              type: 'source',
              endpoint: 'https://gitlab.invalid/api/v4',
              enabled: true,
              status: 'healthy',
              reason: null,
              last_seen_at: '2026-07-28T09:07:00Z',
              instances: 1,
            },
          ],
          instances: [
            {
              id: 1,
              key: 'worker:iac_worker',
              name: 'IaC Worker',
              type: 'worker',
              endpoint: null,
              version: null,
              capabilities: ['background execution'],
              status: 'stale',
              reason: 'Worker heartbeat is stale',
              last_seen_at: '2026-07-28T08:00:00Z',
            },
            {
              id: 2,
              connector_id: 1,
              key: 'gitlab:primary',
              name: 'Primary GitLab',
              type: 'scanner',
              endpoint: 'https://gitlab.invalid',
              version: '1.0',
              capabilities: ['repository discovery'],
              status: 'healthy',
              reason: null,
              last_seen_at: '2026-07-28T09:05:00Z',
            },
          ],
          workers: [
            {
              name: 'iac_worker',
              last_beat_at: '2026-07-28T08:00:00Z',
              status: 'stale',
              stale: true,
              age_seconds: 3900,
            },
            {
              name: 'pipeline_worker',
              last_beat_at: '2026-07-28T09:05:00Z',
              status: 'healthy',
              stale: false,
              age_seconds: 5,
            },
          ],
        },
      });
    });

    await page.goto('/next?view=scanners', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.fleet-map h2')).toHaveText(
      '3 of 3 runtimes mapped',
    );
    await expect(page.locator('.worker-table > .runtime-row')).toHaveCount(3);
    await expect(
      page.locator('.worker-table > .runtime-row').filter({ hasText: 'IaC Worker' }),
    ).toHaveCount(1);
    await expect(
      page.locator('.worker-table > .runtime-row').filter({ hasText: 'pipeline_worker' }),
    ).toHaveCount(1);
    await expect(page.locator('.capacity-panel')).toContainText(
      'GitLab job observation',
    );
    await expect(
      page.locator('.capacity-panel time[datetime="2026-07-28T09:07:00Z"]'),
    ).toHaveCount(1);
    const gitlabRuntime = page
      .locator('.worker-table > .runtime-row')
      .filter({ hasText: 'Primary GitLab' });
    await expect(gitlabRuntime).toContainText('connection test');
    await expect(
      gitlabRuntime.locator('time[datetime="2026-07-28T09:05:00Z"]'),
    ).toHaveCount(1);
  });
});

test.describe('Signal Control overview contracts', () => {
  test('loads the bundled UI and telemetry fonts without a platform fallback', async ({
    page,
  }) => {
    await page.goto('/', { waitUntil: 'networkidle' });
    const typography = await page.evaluate(async () => {
      await document.fonts.ready;
      const loadProbe = async (spec: string, text: string) => {
        const faces = await document.fonts.load(spec, text);
        return {
          count: faces.length,
          loaded: faces.every((face) => face.status === 'loaded'),
          ready: document.fonts.check(spec, text),
        };
      };
      const probes = {
        interLatin: await loadProbe('600 28px "Inter Variable"', 'ScanOps'),
        interCyrillic: await loadProbe('600 28px "Inter Variable"', 'Сканирование'),
        monoLatin: await loadProbe('500 12px "JetBrains Mono Variable"', 'JOB-2048'),
        monoCyrillic: await loadProbe('500 12px "JetBrains Mono Variable"', 'сигнал'),
      };
      const heading = document.querySelector<HTMLElement>('.page-title h1');
      const label = document.querySelector<HTMLElement>('.panel-label');
      const fontResources = performance
        .getEntriesByType('resource')
        .map((entry) => entry.name)
        .filter((name) => /\.woff2(?:$|\?)/.test(name));

      return {
        headingFamily: heading ? getComputedStyle(heading).fontFamily : '',
        labelFamily: label ? getComputedStyle(label).fontFamily : '',
        probes,
        fontResources,
      };
    });

    expect(typography.headingFamily).toContain('Inter Variable');
    expect(typography.labelFamily).toContain('JetBrains Mono Variable');
    for (const probe of Object.values(typography.probes)) {
      expect(probe.count).toBeGreaterThan(0);
      expect(probe.loaded).toBe(true);
      expect(probe.ready).toBe(true);
    }
    expect(typography.fontResources.some((name) => name.includes('/inter-'))).toBe(true);
    expect(typography.fontResources.some((name) => name.includes('/jetbrains-mono-'))).toBe(true);
  });

  test('keeps visible interface copy at or above the compact 12px floor', async ({
    page,
  }) => {
    const routes = ['/', '/pipelines', '/iac', '/coverage', '/targets?level=product', '/scanners', '/integrations'];

    for (const route of routes) {
      await page.goto(route, { waitUntil: 'domcontentloaded' });
      const undersized = await page.locator('.app-shell').evaluate((root) =>
        Array.from(root.querySelectorAll<HTMLElement>('*'))
          .filter((element) => {
            const style = getComputedStyle(element);
            const hasOwnText = Array.from(element.childNodes).some(
              (node) => node.nodeType === Node.TEXT_NODE && Boolean(node.textContent?.trim()),
            );
            return (
              hasOwnText &&
              style.display !== 'none' &&
              style.visibility !== 'hidden' &&
              element.getClientRects().length > 0 &&
              Number.parseFloat(style.fontSize) < 12
            );
          })
          .map((element) => ({
            selector: `${element.tagName.toLowerCase()}.${Array.from(element.classList).join('.')}`,
            size: getComputedStyle(element).fontSize,
            text: element.textContent?.trim().slice(0, 80),
          })),
      );

      expect(undersized, `${route} contains undersized visible copy`).toEqual([]);
    }
  });

  test('uses job outcomes for success and keeps pipeline failures explicit', async ({
    page,
  }) => {
    const response = await page.request.get('/api/ui/overview?timeframe=7d');
    expect(response.ok()).toBeTruthy();
    const payload = await response.json();

    await page.goto('/', { waitUntil: 'domcontentloaded' });
    const stats = page.locator('.pulse-stats > div');
    await expect(stats).toHaveCount(4);
    await expect(stats.nth(0).locator(':scope > strong')).toHaveText(
      payload.topology.execution_total
        ? `${payload.topology.success_rate}%`
        : '—',
    );
    await expect(stats.nth(0)).toContainText('successful jobs');
    await expect(stats.nth(1).locator(':scope > strong')).toHaveText(
      payload.topology.execution_completed.toLocaleString(),
    );
    await expect(stats.nth(2).locator(':scope > strong')).toHaveText(
      payload.topology.execution_failed.toLocaleString(),
    );
    await expect(stats.nth(3).locator(':scope > strong')).toHaveText(
      payload.topology.pipeline_failed.toLocaleString(),
    );
    await expect(
      stats.nth(3).getByRole('link', { name: 'Pipeline failures' }),
    ).toHaveAttribute('href', '/pipelines?status=failed&timeframe=7d');
    await expect(
      page.locator('.header-actions a[href^="/overview/operator"]'),
    ).toHaveCount(0);
  });

  test('aligns the observation window and live state on the shared control scale', async ({
    page,
  }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    const windowControl = page.locator('.observation-window');
    const liveState = page.locator('.signal-state');
    await expect(windowControl).toBeVisible();
    await expect(liveState).toBeVisible();

    const [windowBox, liveBox, typeScale] = await Promise.all([
      windowControl.boundingBox(),
      liveState.boundingBox(),
      page.evaluate(() => ({
        body: getComputedStyle(document.body).fontSize,
        overviewCopy: getComputedStyle(
          document.querySelector('.page-title > div > span')!,
        ).fontSize,
      })),
    ]);
    expect(windowBox).not.toBeNull();
    expect(liveBox).not.toBeNull();
    expect(Math.abs(windowBox!.y - liveBox!.y)).toBeLessThanOrEqual(1);
    expect(Math.abs(windowBox!.height - liveBox!.height)).toBeLessThanOrEqual(1);
    const viewport = page.viewportSize();
    expect(viewport).not.toBeNull();
    if (viewport!.width <= 780) {
      expect(windowBox!.height).toBeGreaterThanOrEqual(44);
    } else {
      expect(windowBox!.height).toBeCloseTo(40, 3);
    }
    expect(typeScale).toEqual({ body: '14px', overviewCopy: '14px' });
  });

  test('changes the Overview observation window and preserves it through URL navigation', async ({
    page,
  }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    const windowControl = page.getByLabel('Overview timeframe');
    await expectSignalSelectValue(windowControl, '7d');

    const thirtyDayResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === '/api/ui/overview'
        && url.searchParams.get('timeframe') === '30d';
    });
    await selectSignalOption(page, windowControl, 'Last 30 days');
    await thirtyDayResponse;
    await expect(page).toHaveURL(/timeframe=30d/);
    await expectSignalSelectValue(windowControl, '30d');
    await expect(page.locator('.pulse-stats')).toContainText('Last 30 days');
    await expect(
      page.getByRole('link', { name: 'Pipeline failures' }),
    ).toHaveAttribute('href', '/pipelines?status=failed&timeframe=30d');

    const allTimeResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === '/api/ui/overview'
        && url.searchParams.get('timeframe') === 'all';
    });
    await selectSignalOption(page, windowControl, 'All time');
    const allTimePayload = await (await allTimeResponse).json();
    expect(allTimePayload.topology.execution_timeframe).toBe('all');
    await expect(page).toHaveURL(/timeframe=all/);
    await expectSignalSelectValue(windowControl, 'all');
    await expect(page.locator('.pulse-stats')).toContainText('All time');
    await expect(
      page.getByRole('link', { name: 'Pipeline failures' }),
    ).toHaveAttribute('href', '/pipelines?status=failed&timeframe=all');

    await page.goBack();
    await expect(page).toHaveURL(/timeframe=30d/);
    await expectSignalSelectValue(windowControl, '30d');
    await page.goBack();
    await expect(page).not.toHaveURL(/timeframe=/);
    await expectSignalSelectValue(windowControl, '7d');
  });

  test('renders the observation window as an in-product accessible listbox', async ({
    page,
  }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    const control = page.getByRole('combobox', { name: 'Overview timeframe' });

    await control.click();
    await expect(control).toHaveAttribute('aria-expanded', 'true');
    const listbox = page.getByRole('listbox', { name: 'Overview timeframe' });
    await expect(listbox).toBeVisible();
    await expect(listbox.getByRole('option')).toHaveCount(6);
    expect(await listbox.evaluate((element) => (
      window.getComputedStyle(element).backgroundColor
    ))).not.toBe('rgb(255, 255, 255)');

    await page.keyboard.press('Escape');
    await expect(listbox).toBeHidden();
    await expect(control).toBeFocused();

    await control.press('ArrowDown');
    await expect(control).toHaveAttribute('aria-expanded', 'true');
    await control.press('Escape');
    await expect(control).toHaveAttribute('aria-expanded', 'false');

    await control.press('Home');
    await expect(control).toHaveAttribute('aria-expanded', 'true');
    let activeId = await control.getAttribute('aria-activedescendant');
    expect(activeId).not.toBeNull();
    await expect(page.locator(`#${activeId}`)).toHaveText('Today');
    await control.press('Enter');
    await expectSignalSelectValue(control, 'today');

    await control.press('End');
    activeId = await control.getAttribute('aria-activedescendant');
    expect(activeId).not.toBeNull();
    await expect(page.locator(`#${activeId}`)).toHaveText('All time');
    await control.press('Escape');

    await control.press('l');
    activeId = await control.getAttribute('aria-activedescendant');
    expect(activeId).not.toBeNull();
    await expect(page.locator(`#${activeId}`)).toHaveText('Last 3 days');
    await control.press('Enter');
    await expectSignalSelectValue(control, '3d');

    await control.click();
    await page.locator('.page-title h1').click();
    await expect(control).toHaveAttribute('aria-expanded', 'false');
  });

  test('keeps all five river nodes and the track on one baseline', async ({
    page,
  }) => {
    await page.goto('/next?view=overview', { waitUntil: 'domcontentloaded' });

    const map = page.locator(
      '.flow-map[aria-label="Observed execution delivery stages"]',
    );
    const stages = map.locator(':scope > .flow-stage');
    const nodes = stages.locator('.flow-node');
    const track = map.locator(':scope > .flow-track');

    await expect(stages).toHaveCount(5);
    await expect(stages.locator('.flow-index')).toHaveText([
      '01',
      '02',
      '03',
      '04',
      '05',
    ]);
    expect(
      await map.locator(':scope > .flow-stage-active').count(),
      'the river may expose at most one active stage',
    ).toBeLessThanOrEqual(1);
    await expect(nodes).toHaveCount(5);
    await expect(track).toHaveCount(1);

    const geometry = await map.evaluate((element) => {
      const trackElement = element.querySelector(':scope > .flow-track');
      const nodeElements = element.querySelectorAll(
        ':scope > .flow-stage .flow-node',
      );
      if (!trackElement) throw new Error('flow track is missing');
      const trackRect = trackElement.getBoundingClientRect();
      return {
        trackCenter: trackRect.top + trackRect.height / 2,
        nodeCenters: Array.from(nodeElements).map((node) => {
          const rect = node.getBoundingClientRect();
          return rect.top + rect.height / 2;
        }),
      };
    });

    for (const [index, nodeCenter] of geometry.nodeCenters.entries()) {
      expect(
        Math.abs(nodeCenter - geometry.trackCenter),
        `river node 0${index + 1} center must share the track baseline`,
      ).toBeLessThanOrEqual(1);
    }
  });

  test('exposes operator context and actionable river stages', async ({
    page,
  }) => {
    await page.goto('/?timeframe=7d', { waitUntil: 'domcontentloaded' });

    await expect(page.locator('.overview-action-strip')).toBeVisible();
    await expect(page.locator('.overview-detail-grid')).toBeVisible();
    await expect(page.locator('.hotspot-panel')).toContainText(
      'Failure hotspots',
    );
    await expect(page.locator('.posture-panel')).toContainText(
      'Posture snapshot',
    );
    await expect(page.locator('.hotspot-row')).toHaveCount(5);
    await expect(page.locator('.posture-list > a')).toHaveCount(5);

    const stages = page.locator('.flow-stage');
    await expect(stages).toHaveCount(5);
    await expect(stages.nth(0)).toHaveAttribute(
      'href',
      '/pipelines?timeframe=7d',
    );
    await expect(stages.nth(1)).toHaveAttribute(
      'href',
      '/pipelines?group=in_progress&timeframe=7d',
    );
    await expect(stages.nth(2)).toHaveAttribute(
      'href',
      '/logs?timeframe=7d',
    );
    await expect(stages.nth(4)).toHaveAttribute('href', '/targets?level=product');

    await stages.nth(1).click();
    await expect(page).toHaveURL(
      /\/pipelines\?group=in_progress&timeframe=7d$/,
    );
  });

  test('renders an unavailable success rate neutrally when there are no executions', async ({
    page,
  }) => {
    await page.route('**/api/ui/overview*', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      Object.assign(payload.topology, {
        telemetry_state: 'empty',
        gitlab_triggers: 0,
        active_pipelines: 0,
        success_rate: 0,
        failure_rate: 0,
        execution_completed: 0,
        execution_failed: 0,
        execution_total: 0,
        normalized_outputs: 0,
        asoc_confirmed_deliveries: 0,
        asoc_mapped_products: 0,
        last_execution_at: null,
        last_execution_label: 'not recorded',
      });
      payload.throughput = payload.throughput.map(
        (bucket: { label: string; range_label: string; start_at: string; end_at: string }) => ({
          ...bucket,
          succeeded: 0,
          failed: 0,
        }),
      );
      await route.fulfill({ response, json: payload });
    });

    const overviewResponse = page.waitForResponse(
      (response) => new URL(response.url()).pathname === '/api/ui/overview',
    );
    await page.goto('/next?view=overview', { waitUntil: 'domcontentloaded' });
    await overviewResponse;
    await expect(page.locator('.sync-note')).toContainText(
      'Signal not recorded',
    );

    const successStat = page.locator('.pulse-stats > .hero-stat');
    const successValue = successStat.locator(':scope > strong');
    const renderedText = (await successValue.innerText()).trim();
    const classTokens = await successStat.evaluate((element) =>
      [element, ...element.querySelectorAll('*')].flatMap((node) =>
        Array.from(node.classList),
      ),
    );
    const positiveClassTokens = classTokens.filter((token) =>
      /(^|[-_])(?:positive|success|succeeded)(?:$|[-_])/.test(token),
    );
    const [successColor, positiveColor] = await Promise.all([
      successValue.evaluate((element) => getComputedStyle(element).color),
      page.evaluate(() => {
        const probe = document.createElement('span');
        probe.style.color = 'var(--acid)';
        document.body.appendChild(probe);
        const color = getComputedStyle(probe).color;
        probe.remove();
        return color;
      }),
    ]);

    expect.soft(renderedText).toMatch(/^(?:—|N\/A)$/);
    expect.soft(renderedText).not.toContain('%');
    expect.soft(positiveClassTokens).toEqual([]);
    await expect(page.locator('.flow-map')).toHaveClass(/is-empty/);
    await expect(page.locator('.flow-map .packet').first()).toBeHidden();
    expect
      .soft(
        successColor,
        'zero-sample success must not use the positive green tone',
      )
      .not.toBe(positiveColor);
  });

  test('keeps an initially unavailable overview neutral and truthful', async ({
    page,
  }) => {
    await page.route('**/api/ui/overview*', async (route) => {
      await route.abort('failed');
    });

    await page.goto('/next?view=overview', { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'Signal is unavailable.',
    );

    const successStat = page.locator('.pulse-stats > .hero-stat');
    const successValue = successStat.locator(':scope > strong');
    await expect(successStat).toHaveClass(/is-empty/);
    await expect(successValue).toHaveText('—');
    const unavailableDangerStats = page.locator(
      '.pulse-stats > .danger-stat',
    );
    await expect(unavailableDangerStats).toHaveCount(2);
    for (const dangerStat of await unavailableDangerStats.all()) {
      await expect(dangerStat).toHaveClass(/is-empty/);
    }
    await expect(page.locator('.flow-map')).toHaveClass(/is-empty/);
    await expect(page.locator('.flow-map .packet').first()).toBeHidden();
    await expect(page.locator('.attention')).toHaveClass(/is-unavailable/);
    await expect(page.locator('.calm-note')).toHaveClass(/is-unavailable/);
    await expect(page.locator('.calm-note')).toContainText(
      'Prioritization unavailable',
    );
    await expect(page.locator('.duration-panel')).toContainText(
      'Pipeline duration',
    );
    await expect(page.locator('.duration-panel')).toContainText(
      'Open details',
    );

    const [successColor, positiveColor] = await Promise.all([
      successValue.evaluate((element) => getComputedStyle(element).color),
      page.evaluate(() => {
        const probe = document.createElement('span');
        probe.style.color = 'var(--acid)';
        document.body.appendChild(probe);
        const color = getComputedStyle(probe).color;
        probe.remove();
        return color;
      }),
    ]);
    expect(successColor).not.toBe(positiveColor);
  });

  test('keeps throughput bars, axis and keyboard tooltip on the selected timeframe', async ({
    page,
  }) => {
    await page.goto('/next?view=overview&timeframe=30d', {
      waitUntil: 'domcontentloaded',
    });
    const chart = page.locator('.chart[role="group"]');
    await expect(chart).toHaveAttribute(
      'aria-label',
      /Job outcomes · Last 30 days · daily buckets · Europe\/Moscow/,
    );
    await expect(page.locator('.chart-window-label')).toContainText(
      'Last 30 days · daily buckets · Europe/Moscow',
    );
    await expect(chart.locator('.bar-field > span')).toHaveCount(31);
    const axisGeometry = await chart.evaluate((element) => {
      const chartRect = element.getBoundingClientRect();
      const labels = Array.from(element.querySelectorAll('.chart-axis span'))
        .map((node) => node.getBoundingClientRect());
      const gridRect = element.querySelector('.chart-grid')!.getBoundingClientRect();
      const barsRect = element.querySelector('.bar-field')!.getBoundingClientRect();
      const axisRect = element.querySelector('.chart-axis')!.getBoundingClientRect();
      return {
        visible: labels.every((rect) => rect.top >= chartRect.top && rect.bottom <= chartRect.bottom && rect.left >= chartRect.left && rect.right <= chartRect.right),
        overlaps: labels.some((rect, index) => labels.slice(index + 1).some((other) => rect.left < other.right && other.left < rect.right)),
        aligned: [gridRect, barsRect, axisRect].every((rect) => Math.abs(rect.left - chartRect.left - 8) < 1 && Math.abs(chartRect.right - rect.right - 8) < 1),
      };
    });
    expect(axisGeometry).toEqual({ visible: true, overlaps: false, aligned: true });

    await chart.click();
    await chart.press('ArrowLeft');
    const tooltip = chart.locator('.chart-tooltip');
    await expect(tooltip).toBeVisible();
    await expect(tooltip).toContainText('Total');
    await expect(tooltip).toContainText('Succeeded');
    await expect(tooltip).toContainText('Failed');
    const tooltipColors = await tooltip.evaluate((element) => {
      const colorFromToken = (token: string) => {
        const probe = document.createElement('span');
        probe.style.color = `var(${token})`;
        document.body.appendChild(probe);
        const color = getComputedStyle(probe).color;
        probe.remove();
        return color;
      };
      return {
        copy: getComputedStyle(element).color,
        heading: getComputedStyle(element.querySelector('strong')!).color,
        value: getComputedStyle(element.querySelector('b')!).color,
        muted: colorFromToken('--muted'),
        ink: colorFromToken('--ink'),
      };
    });
    expect(tooltipColors.copy).toBe(tooltipColors.muted);
    expect(tooltipColors.heading).toBe(tooltipColors.ink);
    expect(tooltipColors.value).toBe(tooltipColors.ink);

    for (let index = 0; index < 31; index += 1) {
      await chart.press('ArrowLeft');
    }
    for (let index = 0; index < 3; index += 1) {
      await chart.press('ArrowRight');
    }
    await expect(chart.locator('.bar-field > span').nth(3)).toHaveClass(/is-active/);
    await page.evaluate(() => {
      const url = new URL(window.location.href);
      url.searchParams.set('timeframe', '7d');
      window.history.pushState({}, '', url);
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    await expect(page).toHaveURL(/timeframe=7d/);
    await expect(chart.locator('.bar-field > span')).toHaveCount(8);
    await expect(tooltip).toBeHidden();
    await chart.press('ArrowLeft');
    await expect(tooltip).toBeVisible();
    await expect(tooltip).toContainText('Europe/Moscow');
    await chart.press('Escape');
    await expect(tooltip).toBeHidden();
  });

  test('populated demo exposes live execution and throughput evidence', async ({
    request,
  }) => {
    test.skip(
      process.env.QA_EXPECT_POPULATED_DEMO !== '1',
      'Set QA_EXPECT_POPULATED_DEMO=1 for the populated demo contract.',
    );

    const response = await request.get('/api/ui/overview');
    expect(
      response.ok(),
      `overview API returned HTTP ${response.status()}`,
    ).toBe(true);

    const payload = await response.json() as {
      topology: {
        execution_total: number;
        normalized_outputs: number;
        telemetry_state: string;
      };
      throughput: { succeeded: number; failed: number }[];
    };
    const throughputActivity = payload.throughput.reduce(
      (total, bucket) => total + bucket.succeeded + bucket.failed,
      0,
    );

    expect.soft(
      payload.topology.execution_total,
      'populated demo must expose terminal executions',
    ).toBeGreaterThan(0);
    expect.soft(
      payload.topology.normalized_outputs,
      'populated demo must expose normalized records',
    ).toBeGreaterThan(0);
    expect.soft(
      throughputActivity,
      'populated demo must expose throughput activity',
    ).toBeGreaterThan(0);
    expect.soft(
      payload.topology.telemetry_state,
      'populated demo telemetry must be live',
    ).toBe('live');
  });

  test('reconciles linked screen totals and labels their time scopes', async ({
    page,
    request,
  }) => {
    test.skip(
      process.env.QA_EXPECT_POPULATED_DEMO !== '1',
      'Set QA_EXPECT_POPULATED_DEMO=1 for the populated demo contract.',
    );

    const [overviewResponse, configResponse, iacResponse, pipelinesResponse] =
      await Promise.all([
        request.get('/api/ui/overview'),
        request.get('/api/ui/config-guard'),
        request.get('/api/ui/iac'),
        request.get('/api/ui/pipelines?group=in_progress&timeframe=7d'),
      ]);
    for (const response of [
      overviewResponse,
      configResponse,
      iacResponse,
      pipelinesResponse,
    ]) {
      expect(response.ok(), `${response.url()} must return HTTP 200`).toBe(true);
    }

    const overview = await overviewResponse.json() as {
      attention: { config_drift: number };
    };
    const config = await configResponse.json() as {
      summary: { open_findings: number };
    };
    const iac = await iacResponse.json() as {
      selected_run_id: number | null;
      summary: {
        findings: number;
        critical: number;
        high: number;
        medium: number;
        low: number;
        info: number;
      } | null;
      repositories: { run_id: number }[];
      findings: { run_id: number }[];
    };
    const pipelines = await pipelinesResponse.json() as {
      total: number;
      matching: number;
      counts: Record<string, number>;
    };
    const pipelineWindowTotal = Object.values(pipelines.counts).reduce(
      (total, count) => total + count,
      0,
    );

    expect(config.summary.open_findings).toBe(
      overview.attention.config_drift,
    );
    expect(pipelines.matching).toBe(pipelines.counts.in_progress);
    expect(pipelines.total).toBeGreaterThanOrEqual(pipelineWindowTotal);
    expect(iac.selected_run_id).not.toBeNull();
    expect(
      iac.repositories.every((row) => row.run_id === iac.selected_run_id),
    ).toBe(true);
    expect(
      iac.findings.every((row) => row.run_id === iac.selected_run_id),
    ).toBe(true);
    expect(iac.summary?.findings).toBe(
      (iac.summary?.critical ?? 0) +
        (iac.summary?.high ?? 0) +
        (iac.summary?.medium ?? 0) +
        (iac.summary?.low ?? 0) +
        (iac.summary?.info ?? 0),
    );

    await page.goto('/next?view=config', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.header-actions .quiet-button')).toContainText(
      `${config.summary.open_findings} open`,
    );

    await page.goto('/next?view=pipelines', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.pipeline-register-columns')).toHaveCount(1);
    await expect(page.locator('.queue-panel')).toHaveCount(0);

    await page.goto('/next?view=iac', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.severity-columns > span')).toHaveCount(5);
    const renderedSeverityTotal = await page
      .locator('.severity-columns > span > b')
      .allTextContents()
      .then((values) =>
        values.reduce((total, value) => total + Number(value), 0),
      );
    expect(renderedSeverityTotal).toBe(iac.summary?.findings);
  });
});

test.describe('Job journal outcome summary', () => {
  test('shows successful and failed job counts from the filtered journal scope', async ({
    page,
  }) => {
    const response = await page.request.get(
      '/api/ui/job-journal?status=all&timeframe=7d&page=1&page_size=25',
    );
    expect(response.ok()).toBeTruthy();
    const payload = await response.json();

    await page.goto('/logs', { waitUntil: 'domcontentloaded' });
    await expectSignalSelectValue(page.getByLabel('Jobs per page'), '25');
    const summary = page.locator('.job-outcome-summary > div');
    await expect(summary).toHaveCount(5);
    await expect(summary.nth(0).locator('strong')).toHaveText(
      payload.summary.matching.toLocaleString(),
    );
    await expect(summary.nth(1).locator('strong')).toHaveText(
      payload.summary.successful.toLocaleString(),
    );
    await expect(summary.nth(2).locator('strong')).toHaveText(
      payload.summary.failed.toLocaleString(),
    );
    await expect(summary.nth(2).locator('strong')).toHaveClass(
      /metric-danger/,
    );
  });
});

test.describe('Signal Control live interaction contracts', () => {
  test('keeps last confirmed data visible when refresh fails and recovers on retry', async ({
    page,
  }) => {
    let requests = 0;
    let failedRefresh = false;
    const confirmedExecution = {
      id: 901,
      reference: 'EX-00901',
      source: 'sast',
      source_label: 'SAST',
      status: 'succeeded',
      failure_category: null,
      failure_reason: null,
      repository_id: 17,
      repository: 'group/signal-control',
      product_id: null,
      product: null,
      correlation_key: 'qa-preservation-901',
      requested_at: '2026-08-12T08:00:00Z',
      started_at: '2026-08-12T08:00:01Z',
      finished_at: '2026-08-12T08:00:08Z',
      duration_seconds: 7,
      context: {},
      steps: [
        {
          id: 9011,
          name: 'SAST scan',
          source: 'sast',
          status: 'succeeded',
          failure_category: null,
          failure_reason: null,
          started_at: '2026-08-12T08:00:01Z',
          finished_at: '2026-08-12T08:00:08Z',
          duration_seconds: 7,
        },
      ],
      artifacts: [],
      href: '/targets/repositories/17',
    };
    const executionsPayload = {
      rows: [confirmedExecution],
      counts: { all: 1, running: 0, failed: 0, succeeded: 1 },
      filter: 'all',
      repository_filter: null,
    };
    await page.route('**/api/ui/executions?**', async (route) => {
      requests += 1;
      if (requests > 1 && !failedRefresh) {
        failedRefresh = true;
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'temporary outage' }),
        });
        return;
      }
      await route.fulfill({ json: executionsPayload });
    });

    await page.goto('/executions', { waitUntil: 'domcontentloaded' });
    const liveNote = page.locator('.live-filter-note');
    await expect(liveNote).toContainText('auto-refreshing');
    const executionRow = page.locator('.execution-row').first();
    await expect(executionRow).toBeVisible();
    await expect(executionRow).toContainText('EX-00901');
    const confirmedRowText = await executionRow.innerText();

    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    const refreshAlert = page.getByRole('alert').filter({
      hasText: 'Live refresh paused',
    });
    await expect(refreshAlert).toBeVisible();
    await expect(liveNote).toContainText('refresh paused');
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'Live activity.',
    );
    await expect(executionRow).toBeVisible();
    expect(await executionRow.innerText()).toBe(confirmedRowText);

    await refreshAlert.getByRole('button', { name: 'Retry now' }).click();
    await expect(refreshAlert).toHaveCount(0);
    await expect(liveNote).toContainText('auto-refreshing');
    await expect(executionRow).toBeVisible();
    expect(await executionRow.innerText()).toBe(confirmedRowText);
    expect(requests).toBeGreaterThanOrEqual(3);
  });

  test('quick action supports keyboard navigation, focus trap and focus return', async ({
    page,
  }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    const opener = page.getByRole('button', { name: 'Quick action' });
    await opener.click();

    const dialog = page.getByRole('dialog', { name: 'Quick action' });
    const input = page.getByRole('combobox', { name: 'Filter quick actions' });
    await expect(dialog).toBeVisible();
    await expect(input).toBeFocused();
    await expect(page.locator('.app-shell')).toHaveAttribute(
      'aria-hidden',
      'true',
    );
    expect(
      await page.locator('.app-shell').evaluate((element) => (
        element as HTMLElement
      ).inert),
    ).toBe(true);

    await input.press('ArrowDown');
    await expect(
      dialog.getByRole('option', { name: /Executions/ }),
    ).toHaveAttribute('aria-selected', 'true');
    await input.press('Enter');
    await expect(page).toHaveURL(/\/executions$/);
    await expect(opener).toBeFocused();

    await page.keyboard.press('Control+K');
    await expect(input).toBeFocused();
    await input.press('Shift+Tab');
    expect(
      await dialog.evaluate((element) => element.contains(document.activeElement)),
    ).toBe(true);
    await page.keyboard.press('Tab');
    await expect(input).toBeFocused();
    await input.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(opener).toBeFocused();
  });
});

test.describe('Signal Control mobile 390 regressions', () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(
      testInfo.project.name !== 'mobile-390',
      'These assertions define the 390px mobile layout contract.',
    );
  });

  test('all sections stay inside the root viewport', async ({ page }) => {
    for (const [view, expectedHeading] of views) {
      await page.goto(`/next?view=${view}`, { waitUntil: 'domcontentloaded' });
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(expectedHeading);
      await expect(page.locator('.live-state-loading')).toHaveCount(0, {
        timeout: 10_000,
      });

      await expect
        .poll(
          () =>
            page.evaluate(() =>
              Math.max(
                0,
                document.documentElement.scrollWidth - window.innerWidth,
              ),
            ),
          { message: `${view} must not create horizontal root overflow at 390px` },
        )
        .toBe(0);
      expect(await page.evaluate(() => window.innerWidth)).toBe(390);
    }
  });

  test('bottom navigation keeps every target touch-sized and visibly labelled', async ({
    page,
  }) => {
    await page.goto('/next?view=overview', { waitUntil: 'domcontentloaded' });
    const navigation = page.getByRole('navigation', { name: 'Primary' });
    const links = navigation.getByRole('link');
    await expect(links).toHaveCount(views.length);

    for (const label of primaryNavigationLabels) {
      const link = navigation.getByRole('link', { name: label, exact: true });
      const visibleLabel = link.locator('span');

      await expect(link).toBeVisible();
      await expect(visibleLabel).toHaveCount(1);
      await expect(visibleLabel).toBeVisible();
      await expect(visibleLabel).toHaveText(label);

      const box = await link.boundingBox();
      expect(box, `${label} must have a rendered touch target`).not.toBeNull();
      expect(box!.width, `${label} touch target width`).toBeGreaterThanOrEqual(44);
      expect(box!.height, `${label} touch target height`).toBeGreaterThanOrEqual(44);
    }
  });

  test('every visible interactive control meets the mobile touch target', async ({
    page,
  }) => {
    for (const [view, expectedHeading] of views) {
      await page.goto(`/next?view=${view}`, { waitUntil: 'domcontentloaded' });
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(expectedHeading);
      await expect(page.locator('.live-state-loading')).toHaveCount(0, {
        timeout: 10_000,
      });

      const undersized = await page
        .locator(
          'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [role="button"]:not([aria-disabled="true"]), [role="tab"]:not([aria-disabled="true"])',
        )
        .evaluateAll((elements) =>
          elements.flatMap((element) => {
            const measuredTarget = element instanceof HTMLInputElement
              && ['checkbox', 'radio'].includes(element.type)
              ? element.closest('label') ?? element
              : element;
            const rect = measuredTarget.getBoundingClientRect();
            const style = getComputedStyle(measuredTarget);
            if (
              style.display === 'none' ||
              style.visibility === 'hidden' ||
              rect.width === 0 ||
              rect.height === 0 ||
              (rect.width >= 44 && rect.height >= 44)
            ) {
              return [];
            }
            const htmlElement = element as HTMLElement;
            return [{
              name:
                element.getAttribute('aria-label') ??
                element.getAttribute('placeholder') ??
                htmlElement.innerText.trim().replace(/\s+/g, ' ').slice(0, 80),
              selector: `${element.tagName.toLowerCase()}.${element.className}`,
              width: Math.round(rect.width * 10) / 10,
              height: Math.round(rect.height * 10) / 10,
            }];
          }),
        );

      expect(
        undersized,
        `${view} has interactive controls below the 44px mobile target`,
      ).toEqual([]);
    }
  });

  test('integration settings keep every GitLab action reachable inside the mobile drawer', async ({
    page,
  }) => {
    await page.goto('/integrations?integration=gitlab', {
      waitUntil: 'domcontentloaded',
    });
    const dialog = page.getByRole('dialog', { name: 'GitLab' });
    await expect(dialog).toBeVisible();
    const scrollRegion = dialog.locator('.integration-settings-scroll');
    await expect
      .poll(() =>
        scrollRegion.evaluate(
          (element) => element.scrollHeight > element.clientHeight,
        ),
      )
      .toBe(true);

    const save = dialog.getByRole('button', { name: 'Save changes' });
    await save.scrollIntoViewIfNeeded();
    await expect(save).toBeVisible();
    const saveBox = await save.boundingBox();
    expect(saveBox, 'Save changes must render inside the mobile drawer').not.toBeNull();
    expect(saveBox!.y).toBeGreaterThanOrEqual(0);
    expect(saveBox!.y + saveBox!.height).toBeLessThanOrEqual(844);
    await expect(
      dialog.getByRole('button', { name: 'Close GitLab settings' }),
    ).toBeVisible();
  });

  test('selecting another execution keeps its detail visible and brings it near the viewport', async ({
    page,
  }) => {
    await page.goto('/executions', { waitUntil: 'domcontentloaded' });
    const rows = page.locator('.execution-row');
    await expect(rows).toHaveCount(25, { timeout: 10_000 });

    await rows.nth(1).click();
    await expect(rows.nth(1)).toHaveClass(/selected/);

    const detail = page.locator('.execution-detail');
    await expect(detail).toBeVisible();
    await expect
      .poll(
        () =>
          detail.evaluate((element) => {
            const rect = element.getBoundingClientRect();
            return rect.top < window.innerHeight && rect.bottom > 0;
          }),
        { message: 'the selected execution detail should intersect the mobile viewport' },
      )
      .toBe(true);
  });

  test('failed jobs render as full-width source rows without a duplicate detail pane', async ({
    page,
  }) => {
    await page.goto('/logs?status=failed', { waitUntil: 'domcontentloaded' });
    const failures = page.locator('.job-journal-row');
    await expect(failures.nth(1)).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.failure-detail')).toHaveCount(0);
    await expect(page.locator('.failure-workbench')).toHaveCount(0);

    const first = failures.first();
    await expect(first.locator('.job-identity strong')).not.toBeEmpty();
    await expect(first.locator('.job-repository-link')).toHaveAttribute(
      'href',
      /\/targets\/repositories\/\d+/,
    );
    await expect(first.getByRole('link', { name: /Open pipeline/ })).toHaveAttribute(
      'href',
      /\/pipelines\/\d+/,
    );
    await expect(first.locator('.job-ref code')).not.toBeEmpty();
    await expect(first.locator('.job-finished time')).toHaveAttribute(
      'datetime',
      /.+/,
    );
    await expect(first.locator('.job-duration b')).not.toBeEmpty();

    const pageHeight = await page.evaluate(
      () => document.documentElement.scrollHeight,
    );
    expect(pageHeight, 'failed jobs page height must remain bounded').toBeLessThan(
      10_000,
    );

    await page.goto('/logs', { waitUntil: 'domcontentloaded' });
    await expect
      .poll(() =>
        page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      )
      .toBe(true);
  });

  test('pipelines never overflow the root viewport', async ({ page }) => {
    await page.goto('/pipelines', { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'Pipelines in flight.',
    );
    await expect(page.locator('.live-state-loading')).toHaveCount(0, {
      timeout: 10_000,
    });

    await expect
      .poll(() =>
        page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      )
      .toBe(true);

  });

  test('failed jobs opens the pipeline attention backlog', async ({ page }) => {
    await page.goto('/logs?status=failed', { waitUntil: 'domcontentloaded' });
    await page.getByRole('link', { name: 'Inspect pipelines' }).click();

    await expect(page).toHaveURL(/\/pipelines\?group=needs_attention$/);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'Intervention queue.',
    );
    await expect(
      page.getByRole('button', { name: /Needs attention/ }),
    ).toHaveClass(/active/);
  });
});
