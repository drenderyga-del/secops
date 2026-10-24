import { expect, test, type Locator, type Page } from '@playwright/test';

const integrationPayload = {
  management: {
    available: true,
    auth_ready: true,
    credential_store_ready: true,
  },
  summary: {
    integration_types: 1,
    connections: 2,
    configured: 2,
    healthy: 2,
    failed: 0,
    untested: 0,
    disabled: 0,
  },
  rows: [{
    key: 'gitlab',
    name: 'GitLab',
    role: 'Source control and pipeline telemetry',
    detail: 'Repository discovery and security pipeline execution.',
    direction: 'inbound',
    canonical_direction: 'GitLab → ScanOps',
    configured: true,
    enabled: true,
    source: 'database',
    credential_ref: 'database:gitlab',
    updated_at: '2026-07-28T09:00:00Z',
    updated_by: 'operator',
    tested_at: '2026-07-28T09:05:00Z',
    test_status: 'healthy',
    test_message: 'Connected',
    health_status: 'healthy',
    health_reason: 'Both configured connections are healthy.',
    connection_summary: {
      total: 2,
      configured: 2,
      healthy: 2,
      failed: 0,
      untested: 0,
      disabled: 0,
    },
    last_successful_check: {
      at: '2026-07-28T09:05:00Z',
      message: 'Connected',
    },
    error: null,
    fields: [],
    instances: [
      {
        id: 'gl-1111111111111111',
        name: 'Primary GitLab',
        host: 'gitlab-primary.example.invalid',
        api_url: 'https://gitlab-primary.example.invalid/api/v4',
        token_configured: true,
        enabled: true,
        polling_enabled: true,
        tested_at: '2026-07-28T09:05:00Z',
        test_status: 'healthy',
        test_message: 'Connected',
        last_success_at: '2026-07-28T09:05:00Z',
        last_success_message: 'Connected',
        last_successful_check: {
          at: '2026-07-28T09:05:00Z',
          message: 'Connected',
        },
        credential_ref: 'database:gitlab:primary',
        health_label: 'Healthy',
      },
      {
        id: 'gl-2222222222222222',
        name: 'Secondary GitLab',
        host: 'gitlab-secondary.example.invalid',
        api_url: 'https://gitlab-secondary.example.invalid/api/v4',
        token_configured: true,
        enabled: true,
        polling_enabled: false,
        tested_at: '2026-07-28T09:04:00Z',
        test_status: 'healthy',
        test_message: 'Connected',
        last_success_at: '2026-07-28T09:04:00Z',
        last_success_message: 'Connected',
        last_successful_check: {
          at: '2026-07-28T09:04:00Z',
          message: 'Connected',
        },
        credential_ref: 'database:gitlab:secondary',
        health_label: 'Healthy',
      },
    ],
    href: '/integrations?integration=gitlab',
  }],
};

async function prepareIntegrations(page: Page): Promise<void> {
  await page.route('**/api/ui/integrations', async (route) => {
    await route.fulfill({ json: integrationPayload });
  });
  await page.goto('/integrations', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.connector-row')).toHaveCount(1);
}

async function openGitLabFrom(page: Page, opener: Locator): Promise<Locator> {
  await opener.click();
  const drawer = page.locator('.integration-settings-drawer');
  await expect(drawer).toHaveAttribute('role', 'dialog');
  await expect(page).toHaveURL(
    /\/integrations\?integration=gitlab&instance=gl-1111111111111111$/,
  );
  return drawer;
}

async function expectExactReturn(page: Page, opener: Locator): Promise<void> {
  await expect(page).toHaveURL(/\/integrations$/);
  await expect(page.locator('.integration-settings-drawer')).toHaveCount(0);
  await expect(opener).toBeFocused();
}

test.describe('Integrations focus return', () => {
  test.beforeEach(async ({}, testInfo) => {
    test.skip(testInfo.project.name !== 'desktop-1440', 'Drawer keyboard contract runs at the supported desktop viewport');
  });

  test('Escape returns focus to the exact connector row after trapping Tab', async ({ page }) => {
    await prepareIntegrations(page);
    const opener = page.locator('.connector-row').filter({ hasText: 'GitLab' });
    const drawer = await openGitLabFrom(page, opener);
    await expect(
      drawer.getByText(
        'Connection test verifies authentication and project discovery. Config Guard CI Lint is project-scoped.',
      ),
    ).toBeVisible();
    const close = drawer.getByRole('button', { name: 'Close GitLab settings' });
    const remove = drawer.getByRole('button', { name: 'Remove connection' });

    await expect(close).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(remove).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(close).toBeFocused();

    await page.keyboard.press('Escape');
    await expectExactReturn(page, opener);
  });

  test('Close returns focus to the exact connector row', async ({ page }) => {
    await prepareIntegrations(page);
    const opener = page.locator('.connector-row').filter({ hasText: 'GitLab' });
    const drawer = await openGitLabFrom(page, opener);
    await drawer.getByRole('button', { name: 'Close GitLab settings' }).click();
    await expectExactReturn(page, opener);
  });

  test('backdrop returns focus to the exact connector row', async ({ page }) => {
    await prepareIntegrations(page);
    const opener = page.locator('.connector-row').filter({ hasText: 'GitLab' });
    await openGitLabFrom(page, opener);
    await page.locator('.integration-settings-backdrop').click({
      position: { x: 8, y: 120 },
    });
    await expectExactReturn(page, opener);
  });

  test('browser Back returns focus to the Manage connections opener', async ({ page }) => {
    await prepareIntegrations(page);
    const opener = page.getByRole('button', { name: 'Manage connections' });
    await openGitLabFrom(page, opener);
    await page.goBack();
    await expectExactReturn(page, opener);
  });
});
