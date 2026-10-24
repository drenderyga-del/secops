import { expect, test } from '@playwright/test';

test.describe('execution duration detail', () => {
  test('uses the canonical Signal Control shell and exposes duration detail data', async ({
    page,
  }, testInfo) => {
    test.skip(
      testInfo.project.name !== 'desktop-1440',
      'The regression contract targets the supported desktop route.',
    );

    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/?timeframe=7d', { waitUntil: 'networkidle' });
    const orbitGeometry = await page.locator('.duration-card-orbit').evaluate((orbit) => {
      const sample = orbit.querySelector('small');
      if (!sample) return null;
      const orbitBox = orbit.getBoundingClientRect();
      const sampleBox = sample.getBoundingClientRect();
      return {
        leftInset: sampleBox.left - orbitBox.left,
        rightInset: orbitBox.right - sampleBox.right,
      };
    });
    expect(orbitGeometry).not.toBeNull();
    expect(orbitGeometry?.leftInset ?? 0).toBeGreaterThanOrEqual(5);
    expect(orbitGeometry?.rightInset ?? 0).toBeGreaterThanOrEqual(5);

    const durationApiResponsePromise = page.waitForResponse(
      (candidate) =>
        candidate.request().method() === 'GET' &&
        candidate.url().includes('/api/ui/duration?'),
    );
    const response = await page.goto('/dashboard/duration?timeframe=7d', {
      waitUntil: 'networkidle',
    });
    expect(response?.status()).toBe(200);
    const durationApiResponse = await durationApiResponsePromise;
    expect(durationApiResponse.status()).toBe(200);
    const durationData = (await durationApiResponse.json()) as {
      pipeline?: { count?: number | null };
      job_breakdown?: unknown[];
    };

    await expect(page.getByRole('heading', { name: 'Pipeline duration' })).toBeVisible();
    await expect(page.locator('.duration-average-orbit')).toContainText('Average');
    await expect(page.locator('.duration-focus-panel')).toContainText('terminal pipelines');
    await expect(page.locator('.duration-trend-panel')).toContainText('Average duration trend');
    await expect(page.locator('.duration-breakdown-panel')).toContainText('By job');

    const durationRows = page.locator('.duration-job-row');
    const terminalSamples = durationData.pipeline?.count ?? 0;
    if (terminalSamples === 0) {
      await expect(durationRows).toHaveCount(0);
      await expect(page.locator('.duration-breakdown-panel .duration-trend-empty')).toContainText(
        'No completed job durations in this window.',
      );
    } else {
      // Seeded kitchen-sink/volume profiles must continue to prove that data is rendered.
      await expect(durationRows).toHaveCount(durationData.job_breakdown?.length ?? 0);
      await expect(durationRows.first()).toBeVisible();
    }

    const timeframe = page.getByRole('combobox', { name: 'Duration timeframe' });
    await timeframe.click();
    await expect(page.getByRole('option', { name: 'Today' })).toBeVisible();
    await page.getByRole('option', { name: 'Last 30 days' }).click();
    await expect(page).toHaveURL(/timeframe=30d/);
    await expect(page.locator('.duration-page-title')).toContainText('Pipeline duration');

    const screenshotPath = testInfo.outputPath('operator-duration-detail-full-page.png');
    await page.screenshot({
      path: screenshotPath,
      fullPage: true,
      animations: 'disabled',
    });
    await testInfo.attach('operator-duration-detail', {
      path: screenshotPath,
      contentType: 'image/png',
    });
  });
});
