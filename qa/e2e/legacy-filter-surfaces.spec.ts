import { expect, test, type Locator, type Page } from '@playwright/test';

const selectLegacyOption = async (page: Page, trigger: Locator, option: string) => {
  await trigger.click();
  await page.locator('.signal-select-menu:visible').getByRole('option', { name: option, exact: true }).click();
};

test.describe('server-rendered filter surfaces', () => {
  test('IaC inventory and findings submit the complete server-side filter contract', async ({ page }) => {
    await page.goto('/iac/repositories', { waitUntil: 'domcontentloaded' });
    const repositories = page.getByRole('form', { name: 'Filter IaC repositories' });
    await expect(repositories).toHaveClass(/signal-filter-surface/);
    await repositories.getByLabel('Search').fill('platform');
    await selectLegacyOption(page, repositories.getByRole('button', { name: 'IaC', exact: true }), 'IaC only');
    await repositories.getByLabel('Critical / High only').check();
    await repositories.getByRole('button', { name: 'Apply' }).click();
    await expect(page).toHaveURL(/\/iac\/repositories\?.*q=platform/);
    await expect(page).toHaveURL(/has_important_findings=yes/);

    await page.goto('/iac/findings', { waitUntil: 'domcontentloaded' });
    const findings = page.getByRole('form', { name: 'Filter IaC findings' });
    await expect(findings).toHaveClass(/signal-filter-surface/);
    await selectLegacyOption(page, findings.getByRole('button', { name: 'Severity', exact: true }), 'High');
    await findings.getByLabel('Rule').fill('CKV');
    await findings.getByLabel('Repository').fill('platform');
    await selectLegacyOption(page, findings.getByRole('button', { name: 'Scanner', exact: true }), 'Checkov');
    await findings.getByRole('button', { name: 'Apply' }).click();
    await expect(page).toHaveURL(/\/iac\/findings\?.*severity=high/);
    await expect(page).toHaveURL(/scanner=checkov/);
  });

  test('local IaC and Config Guard result filters are keyboard-accessible and history-addressable', async ({ page }) => {
    await page.goto('/iac/findings', { waitUntil: 'domcontentloaded' });
    const evidence = page.getByRole('link', { name: 'Open repository evidence' }).first();
    await expect(evidence).toBeVisible();
    const detailPath = await evidence.getAttribute('href');
    expect(detailPath).toMatch(/^\/iac\/repositories\/\d+$/);
    await page.goto(detailPath!, { waitUntil: 'domcontentloaded' });
    const iacSearch = page.getByLabel('Search results');
    const iacSeverity = page.getByRole('button', { name: 'Severity', exact: true });
    await expect(iacSearch).toBeVisible();
    await iacSearch.fill('policy');
    await expect(page).toHaveURL(/finding_q=policy/);
    await iacSeverity.focus();
    await page.keyboard.press('Enter');
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/finding_severity=/);
    await page.goBack();
    await expect(iacSearch).toHaveValue('policy');
    await expect(iacSeverity).toHaveText(/All \(/);

    const configGuardResponse = await page.request.get('/api/ui/config-guard');
    expect(configGuardResponse.ok()).toBeTruthy();
    const configGuard = await configGuardResponse.json() as { findings: Array<{ href: string }> };
    expect(configGuard.findings[0]?.href).toMatch(/^\/config-scan\/runs\/\d+\/findings$/);
    await page.goto(configGuard.findings[0].href, { waitUntil: 'domcontentloaded' });
    const configSearch = page.getByLabel('Search results');
    const configSeverity = page.getByRole('button', { name: 'Severity', exact: true });
    await expect(configSearch).toBeVisible();
    await configSearch.fill('repository');
    await expect(page).toHaveURL(/finding_q=repository/);
    await configSeverity.click();
    await page.locator('.signal-select-menu:visible').locator('[data-value="high"]').click();
    await expect(page).toHaveURL(/finding_severity=high/);
    await page.goBack();
    await expect(configSearch).toHaveValue('repository');
  });

  test('coverage filters use the shared SSR surface without absorbing actions', async ({ page }) => {
    await page.goto('/coverage/operator', { waitUntil: 'domcontentloaded' });
    const coverage = page.getByRole('form', { name: 'Filter coverage results' });
    await expect(coverage).toHaveClass(/signal-filter-surface/);
    await selectLegacyOption(page, coverage.locator('.signal-select-trigger'), 'Problems only');
    await expect(page).toHaveURL(/only_problems=true/);
    await expect(page.locator('.audit-run-form')).not.toHaveClass(/signal-filter-surface/);
  });

  test('SSR result filters stack without horizontal overflow on a narrow viewport', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const configGuardResponse = await page.request.get('/api/ui/config-guard');
    const configGuard = await configGuardResponse.json() as { findings: Array<{ href: string }> };
    const routes = [
      '/iac/repositories',
      '/iac/findings',
      '/coverage/operator',
      configGuard.findings[0].href,
    ];

    for (const route of routes) {
      await page.goto(route, { waitUntil: 'domcontentloaded' });
      const filter = page.locator('.signal-filter-surface').first();
      await expect(filter).toBeVisible();
      await expect.poll(() => filter.evaluate((element) => getComputedStyle(element).gridTemplateColumns.split(' ').length)).toBe(1);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, `${route} should not overflow at 390px`).toBeLessThanOrEqual(1);
    }
  });

  test('legacy job journal keeps its scoped SSR filter contract', async ({ page }) => {
    await page.goto('/targets/operator?level=repository', { waitUntil: 'domcontentloaded' });
    const jobsHref = await page.getByRole('link', { name: 'View job logs' }).first().getAttribute('href');
    expect(jobsHref).toMatch(/^\/repos\/\d+\/jobs$/);

    await page.goto(jobsHref!, { waitUntil: 'domcontentloaded' });
    const jobs = page.getByRole('form', { name: 'Filter jobs' });
    await expect(jobs).toHaveClass(/signal-filter-surface/);
    await expect(jobs.locator('.locked-field')).toContainText('qa/product-01/service-002');
    const jobInput = jobs.getByLabel('Job name');
    await expect(jobInput).toBeVisible();
    await jobInput.focus();
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/\/repos\/\d+\/jobs\?.*job=/);
    await expect(jobs.getByRole('button', { name: 'Status', exact: true })).toBeVisible();
    await expect(jobs.getByRole('button', { name: 'Timeframe', exact: true })).toBeVisible();
    await expect(jobs.getByRole('link', { name: 'Reset' })).toBeVisible();
    await expect(page.locator('a[href*="page="]')).toHaveCount(0);
  });

  test('coverage product filter discovers and preserves both server query values', async ({ page }) => {
    await page.goto('/coverage/operator', { waitUntil: 'domcontentloaded' });
    const coverageHref = await page.locator('a[href^="/coverage/products/"]').first().getAttribute('href');
    expect(coverageHref).toMatch(/^\/coverage\/products\/\d+/);

    await page.goto(coverageHref!, { waitUntil: 'domcontentloaded' });
    const coverage = page.getByRole('form', { name: 'Filter coverage results' });
    await expect(coverage).toHaveClass(/signal-filter-surface/);
    await coverage.locator('#coverage-product-view').selectOption('true');
    await expect(page).toHaveURL(/only_problems=true/);
    await expect(coverage.locator('input[name="status"]')).toHaveValue('all');
    await page.locator('a.audit-metric[href*="status=active"]').first().click();
    await expect(page).toHaveURL(/status=active/);
    await expect(page.getByRole('link', { name: 'Open product target' })).toHaveAttribute('href', /\/targets\/products\/\d+$/);
  });

  test('config scan product and run repository filters stay local and retain findings links', async ({ page }) => {
    await page.goto('/config-scan/operator', { waitUntil: 'domcontentloaded' });
    const productLinks = await page.locator('a[href^="/config-scan/products/"]').evaluateAll((links) => links.map((link) => (link as HTMLAnchorElement).getAttribute('href')).filter(Boolean) as string[]);
    let productHref: string | null = null;
    for (const href of productLinks) {
      const response = await page.request.get(href);
      if ((await response.text()).includes('cscan-repo-search-input')) {
        productHref = href;
        break;
      }
    }
    expect(productHref).toMatch(/^\/config-scan\/products\/\d+$/);

    await page.goto(productHref!, { waitUntil: 'commit' });
    const productFilter = page.getByRole('search', { name: 'Filter config scan repositories' });
    await expect(productFilter).toHaveClass(/signal-filter-surface/);
    const productRepository = page.locator('tr[data-repo-path]').first();
    await expect(productRepository).toBeVisible();
    const repoPath = (await productRepository.getAttribute('data-repo-path')) || '';
    expect(repoPath).not.toBe('');
    await productFilter.getByLabel('Filter repositories').fill(repoPath);
    await expect(productRepository).toBeVisible();
    await expect(productFilter).toContainText('1 matching repositories');
    await expect(page).toHaveURL(new RegExp(`${productHref!}$`));

    const runHref = await page.locator('a[href^="/config-scan/runs/"]').first().getAttribute('href');
    expect(runHref).toMatch(/^\/config-scan\/runs\/\d+$/);
    await page.goto(runHref!, { waitUntil: 'domcontentloaded' });
    const runFilter = page.getByRole('search', { name: 'Filter config scan repositories' });
    await expect(page.locator('a[href*="/config-scan/runs/"][href*="/findings"]')).not.toHaveCount(0);
    if (await runFilter.count()) {
      await expect(runFilter).toHaveClass(/signal-filter-surface/);
      const runRepository = page.locator('tr[data-repo-path]').first();
      await runFilter.getByLabel('Filter repositories').fill((await runRepository.getAttribute('data-repo-path')) || 'missing');
      const uniqueVisibleRepositories = await page.locator('tr[data-repo-path]').evaluateAll((rows) => new Set(rows.map((row) => row.getAttribute('data-repo-path')).filter(Boolean)).size);
      await expect(runFilter.locator('[data-cscan-repo-visible]')).toHaveText(String(uniqueVisibleRepositories));
    }
  });

  test('Target 360 source links preserve navigation semantics and detail links', async ({ page }) => {
    await page.goto('/targets/operator?level=repository', { waitUntil: 'domcontentloaded' });
    const targetHref = await page.locator('a[href^="/targets/repositories/"]').first().getAttribute('href');
    expect(targetHref).toMatch(/^\/targets\/repositories\/\d+$/);
    await page.goto(targetHref!, { waitUntil: 'domcontentloaded' });

    const sourceFilter = page.getByRole('navigation', { name: 'Execution source filter' });
    await expect(sourceFilter).toHaveClass(/signal-filter-links/);
    await expect(sourceFilter).toHaveAttribute('aria-controls', 'timeline');
    await expect(page.locator('#timeline')).toBeVisible();
    const sourceLink = sourceFilter.locator('a[href*="?source="]').first();
    await expect(sourceLink).toHaveAttribute('href', /\/targets\/repositories\/\d+\?source=/);
    await sourceLink.click();
    await expect(page).toHaveURL(/\/targets\/repositories\/\d+\?source=/);
    await expect(sourceFilter.locator('[aria-current="page"]')).toBeVisible();
    await expect(page.getByRole('link', { name: /View job logs/ })).toHaveAttribute('href', /\/repos\/\d+\/jobs$/);
  });

  test('newly covered surfaces keep a single-column filter geometry on narrow viewports', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const routes = ['/targets/operator?level=repository', '/coverage/operator', '/config-scan/operator'];
    for (const route of routes) {
      await page.goto(route, { waitUntil: 'domcontentloaded' });
      const hrefSelector = route.startsWith('/coverage')
        ? 'a[href^="/coverage/products/"]'
        : route.startsWith('/config-scan')
          ? 'a[href^="/config-scan/products/"]'
          : 'a[href^="/repos/"][href$="/jobs"]';
      const hrefs = await page.locator(hrefSelector).evaluateAll((links) => links.map((link) => (link as HTMLAnchorElement).getAttribute('href')).filter(Boolean) as string[]);
      let href: string | null = hrefs[0] || null;
      if (route.startsWith('/config-scan')) {
        for (const candidate of hrefs) {
          const response = await page.request.get(candidate);
          if ((await response.text()).includes('signal-filter-surface')) {
            href = candidate;
            break;
          }
        }
      }
      expect(href).toBeTruthy();
      await page.goto(href!, { waitUntil: 'domcontentloaded' });
      const filter = page.locator('.signal-filter-surface').first();
      await expect(filter).toBeVisible();
      await expect.poll(() => filter.evaluate((element) => getComputedStyle(element).gridTemplateColumns.split(' ').length)).toBe(1);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, `${href} should not overflow at 390px`).toBeLessThanOrEqual(1);
    }
  });
});
