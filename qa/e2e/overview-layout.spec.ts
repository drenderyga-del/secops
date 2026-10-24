import { expect, test, type Locator } from '@playwright/test';

async function box(locator: Locator, name: string) {
  const value = await locator.boundingBox();
  expect(value, `${name} must be visible and measurable`).not.toBeNull();
  return value!;
}

test.describe('Overview composition contract', () => {
  test('keeps the operational rail and next row inside the first viewport', async ({ page }, testInfo) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle', { timeout: 2_500 }).catch(() => undefined);

    const viewport = page.viewportSize();
    expect(viewport).not.toBeNull();

    const signal = await box(page.locator('.signal-panel'), 'execution river');
    const attention = await box(page.locator('.attention'), 'attention rail');
    const throughput = await box(page.locator('.throughput-panel'), 'throughput chart');
    const duration = await box(page.locator('.duration-panel'), 'duration panel');

    expect(Math.abs(signal.y - attention.y), 'primary cards share one row').toBeLessThanOrEqual(2);
    expect(attention.x, 'attention stays to the right of the river').toBeGreaterThanOrEqual(
      signal.x + signal.width - 2,
    );
    expect(Math.abs(signal.height - attention.height), 'primary cards share one height').toBeLessThanOrEqual(4);

    expect(Math.abs(throughput.y - duration.y), 'telemetry cards share one row').toBeLessThanOrEqual(2);
    expect(duration.x, 'latency stays to the right of the chart').toBeGreaterThanOrEqual(
      throughput.x + throughput.width - 2,
    );

    expect(signal.height, 'river remains compact enough for an operations dashboard').toBeLessThanOrEqual(430);
    expect(throughput.y, 'telemetry begins inside the first viewport').toBeLessThan(viewport!.height);

    const screenshotPath = testInfo.outputPath('overview-first-viewport.png');
    await page.screenshot({
      path: screenshotPath,
      animations: 'disabled',
      fullPage: false,
    });
    await testInfo.attach('overview-first-viewport', {
      path: screenshotPath,
      contentType: 'image/png',
    });
  });
});
