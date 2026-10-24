import { expect, test } from '@playwright/test';
import { attachDiagnostics, installDiagnostics } from './support/diagnostics';
import { createFinding } from './support/findings';
import { attachFinding, auditPage } from './support/page-audit';
import { routeInventory } from './support/routes';

test.describe('read-only exploratory route audit', () => {
  for (const route of routeInventory) {
    test(`${route.kind}: ${route.title} [${route.id}]`, async ({ page }, testInfo) => {
      const diagnostics = await installDiagnostics(page, testInfo);
      const viewport = page.viewportSize() ?? { width: 0, height: 0 };
      let responseStatus = 0;

      try {
        const response = await page.goto(route.path, { waitUntil: 'domcontentloaded' });
        responseStatus = response?.status() ?? 0;
        await page.waitForLoadState('networkidle', { timeout: 2_500 }).catch(() => undefined);
      } catch (error) {
        const finding = createFinding({
          systemicId: 'ROUTE_UNAVAILABLE',
          severity: 'P0',
          category: 'availability',
          title: 'Route could not be opened',
          url: new URL(route.path, String(testInfo.project.use.baseURL)).toString(),
          route: route.id,
          state: route.state ?? 'local',
          steps: [`Open ${route.path} with demo credentials`],
          expected: 'The route returns an HTML page.',
          actual: error instanceof Error ? error.message : String(error),
          evidence: {},
        }, testInfo.project.name, viewport);
        await attachFinding(testInfo, finding);
        throw error;
      }

      const screenshotPath = testInfo.outputPath(`${route.id}-full-page.png`);
      await page.screenshot({ path: screenshotPath, fullPage: true, animations: 'disabled', timeout: 30_000 });
      await testInfo.attach('qa-screenshot', { path: screenshotPath, contentType: 'image/png' });

      if (responseStatus >= 400) {
        const severity = responseStatus >= 500 ? 'P0' : 'P1';
        const finding = createFinding({
          systemicId: 'ROUTE_HTTP_ERROR',
          severity,
          category: 'availability',
          title: `Route returned HTTP ${responseStatus}`,
          url: page.url(),
          route: route.id,
          state: route.state ?? 'local',
          steps: [`Open ${route.path} with demo credentials`],
          expected: 'The route returns HTTP 200.',
          actual: `The main document returned HTTP ${responseStatus}.`,
          evidence: { screenshot: screenshotPath },
        }, testInfo.project.name, viewport);
        await attachFinding(testInfo, finding);
      }

      await auditPage(page, testInfo, route, diagnostics, screenshotPath);
      await attachDiagnostics(testInfo, diagnostics);

      // P0 availability/layout failures retain trace and video while other findings remain report-only.
      expect.soft(responseStatus, `main document status for ${route.path}`).toBeLessThan(500);
    });
  }
});
