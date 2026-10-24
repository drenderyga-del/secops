import { expect, test } from '@playwright/test';

const affectedRoutes = [
  '/coverage/operator',
  '/coverage/products/1',
  '/config-scan/products/1',
  '/config-scan/runs/1',
  '/iac/operator',
  '/iac/runs/1',
  '/iac/repositories/1',
  '/repos/1/jobs',
  '/targets/repositories/1',
] as const;

test.describe('redundant local section navigation stays removed', () => {
  for (const route of affectedRoutes) {
    test(`${route} renders the content directly`, async ({ page }) => {
      const response = await page.goto(route, { waitUntil: 'domcontentloaded' });

      expect(response?.status(), `HTTP status for ${route}`).toBe(200);
      await expect(page.locator('.local-toc')).toHaveCount(0);
      await expect(page.locator('[data-local-toc]')).toHaveCount(0);

      const overflow = await page.evaluate(() => ({
        clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
      }));
      expect(
        overflow.scrollWidth,
        `document width for ${route}`,
      ).toBeLessThanOrEqual(overflow.clientWidth + 1);
    });
  }

  test('real route tabs remain on Coverage and IaC', async ({ page }) => {
    await page.goto('/coverage/operator', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.module-tabs')).toContainText('Current coverage');
    await expect(page.locator('.module-tabs')).toContainText('Run history');

    await page.goto('/iac/operator', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.module-tabs')).toContainText('Dashboard');
    await expect(page.locator('.module-tabs')).toContainText('Runs');
  });
});
