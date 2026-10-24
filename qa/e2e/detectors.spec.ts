import { expect, test } from '@playwright/test';
import { collectLayoutSignals, runKeyboardAudit } from './support/page-audit';

test.describe('layout detector regressions', () => {
  test.beforeEach(async ({}, testInfo) => {
    test.skip(testInfo.project.name !== 'desktop-1440', 'Detector fixtures run once');
  });

  test('separates clipped, form-container, link-action, and owned-scroll contracts', async ({ page }) => {
    await page.setContent(`
      <style>
        html, body { margin: 0; }
        .visually-hidden, .sr-only {
          position: absolute;
          width: 1px;
          height: 1px;
          clip: rect(0 0 0 0);
          overflow: hidden;
        }
      </style>
      <a class="brand" href="/" style="display:block;width:60px;height:18px">ScanOps</a>
      <span>Repository <a id="ordinary-inline-data-link" href="/details/2" style="display:inline-block;height:18px">scanops/api</a></span>
      <a id="standalone-scanner-action" data-action="drilldown" href="/scanners/1" style="display:inline-flex;height:18px">Open executions</a>
      <label id="normal-filter-label" class="filter-field" style="display:block;width:132px">
        Connection year
        <select style="display:block;width:180px;height:36px"><option>All years</option></select>
      </label>
      <label class="visually-hidden">Security tool</label>
      <label id="visible-sr-only" class="sr-only" style="position:static;width:82px;height:20px;clip:auto;overflow:visible;display:block;white-space:nowrap">
        Visible overridden assistive label
      </label>
      <label id="genuine-label-overflow" style="display:block;width:96px;white-space:nowrap">
        Genuine long filter label
        <select style="display:block;width:96px;height:36px"><option>All values</option></select>
      </label>
      <div class="table-container" style="width:320px;max-width:100%;min-width:0;overflow:hidden">
        <div id="owned-scroll-region" class="responsive-table-scroll" style="width:100%;max-width:100%;min-width:0;overflow-x:auto">
          <table style="width:1420px"><tbody><tr>
            <td style="white-space:nowrap">
              <a id="ordinary-table-data-link" href="/details/1" style="display:inline-block;height:18px">A deliberately wide timestamp value</a>
            </td>
            <td class="table-actions"><a id="small-table-action" href="/details" style="display:inline-block;height:18px">Inspect</a></td>
          </tr></tbody></table>
        </div>
      </div>
      <button class="real-small-control" style="height:24px">Run</button>
    `);

    const signals = await collectLayoutSignals(page);
    const ownedScrollRegion = await page.locator('#owned-scroll-region').evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
    }));
    expect(ownedScrollRegion.clientWidth).toBeLessThan(ownedScrollRegion.scrollWidth);
    expect(signals.viewportOverflow).toBeNull();
    expect(signals.overflowingText.map((item) => item.selector)).toEqual(
      expect.arrayContaining(['#visible-sr-only', '#genuine-label-overflow']),
    );
    expect(signals.overflowingText.map((item) => item.selector)).not.toContain('#normal-filter-label');
    expect(signals.tinyControls.map((item) => item.selector)).toEqual(
      expect.arrayContaining([
        '#standalone-scanner-action',
        '#small-table-action',
        'button.real-small-control',
      ]),
    );
    expect(signals.tinyControls.map((item) => item.selector)).not.toContain('a.brand');
    expect(signals.tinyControls.map((item) => item.selector)).not.toContain('#ordinary-inline-data-link');
    expect(signals.tinyControls.map((item) => item.selector)).not.toContain('#ordinary-table-data-link');
  });

  test('still reports actual root horizontal scrolling', async ({ page }) => {
    await page.setContent(`
      <style>html, body { margin: 0; } html { scroll-behavior: smooth; }</style>
      <div id="root-overflow" style="width:calc(100vw + 120px);height:20px"></div>
    `);

    const signals = await collectLayoutSignals(page);
    expect(signals.viewportOverflow).not.toBeNull();
    expect(signals.viewportOverflow?.documentWidth).toBeGreaterThan(
      signals.viewportOverflow?.viewportWidth ?? Number.POSITIVE_INFINITY,
    );
  });

  test('does not mistake consecutive identical links for a keyboard trap', async ({
    page,
  }) => {
    await page.setContent(`
      <style>body { min-height: 400px; padding: 40px; }</style>
      <main>
        <a href="/same">Repeated finding title</a>
        <a href="/same">Repeated finding title</a>
        <a href="/same">Repeated finding title</a>
        <button type="button">Next action</button>
      </main>
    `);

    const keyboard = await runKeyboardAudit(page);

    expect(keyboard.focusStops.length).toBeGreaterThanOrEqual(20);
    expect(keyboard.stuck).toBe(false);
  });

  test('distinguishes identical links inside repeated wrappers', async ({
    page,
  }) => {
    await page.setContent(`
      <style>body { min-height: 400px; padding: 40px; }</style>
      <main>
        <div><a href="/same">Repeated finding title</a></div>
        <div><a href="/same">Repeated finding title</a></div>
        <div><a href="/same">Repeated finding title</a></div>
        <button type="button">Next action</button>
      </main>
    `);

    const keyboard = await runKeyboardAudit(page);

    expect(keyboard.focusStops.length).toBeGreaterThanOrEqual(20);
    expect(keyboard.stuck).toBe(false);
  });
});
