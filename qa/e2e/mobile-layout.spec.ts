import { expect, test } from '@playwright/test';

async function settle(page: import('@playwright/test').Page): Promise<void> {
  await expect(page.locator('.live-state-loading')).toHaveCount(0, {
    timeout: 10_000,
  });
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

test.describe('canonical mobile layout contracts', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(
      testInfo.project.name !== 'desktop-1440',
      'Mobile geometry is sampled once from the canonical desktop project.',
    );
    await page.setViewportSize({ width: 390, height: 844 });
  });

  test('PT AI status evidence remains distinct, labelled and touch-sized', async ({ page }) => {
    await page.goto('/ptai-scans', { waitUntil: 'domcontentloaded' });
    await settle(page);

    const rows = page.locator('.ptai-register-row');
    if (await rows.count() === 0) {
      await expect(page.getByRole('status')).toContainText('No PT AI scans');
      return;
    }

    const geometry = await rows.evaluateAll((elements) => elements.map((element) => {
      const row = element.getBoundingClientRect();
      const cells = [...element.querySelectorAll<HTMLElement>('.ptai-status-link')]
        .map((cell) => {
          const rect = cell.getBoundingClientRect();
          const anchor = cell.querySelector<HTMLElement>('.ptai-status-anchor');
          const anchorRect = anchor?.getBoundingClientRect();
          return {
            label: cell.querySelector<HTMLElement>('.ptai-mobile-label')?.innerText.trim(),
            left: rect.left,
            right: rect.right,
            top: rect.top,
            bottom: rect.bottom,
            anchor: anchorRect ? {
              left: anchorRect.left,
              right: anchorRect.right,
              width: anchorRect.width,
              height: anchorRect.height,
            } : null,
          };
        });
      return { row: { left: row.left, right: row.right }, cells };
    }));

    for (const [rowIndex, item] of geometry.entries()) {
      expect(item.cells.map((cell) => cell.label), `row ${rowIndex} status labels`).toEqual([
        'GitLab',
        'ASOC',
        'PT AI',
      ]);
      for (let index = 0; index < item.cells.length - 1; index += 1) {
        const current = item.cells[index];
        const next = item.cells[index + 1];
        const overlapX = Math.min(current.right, next.right) - Math.max(current.left, next.left);
        const overlapY = Math.min(current.bottom, next.bottom) - Math.max(current.top, next.top);
        expect(
          overlapX > 1 && overlapY > 1,
          `row ${rowIndex} status cells ${index} and ${index + 1} overlap`,
        ).toBe(false);
      }
      for (const anchor of item.cells.flatMap((cell) => cell.anchor ? [cell.anchor] : [])) {
        expect(anchor.left, `row ${rowIndex} evidence starts inside its card`).toBeGreaterThanOrEqual(item.row.left - 1);
        expect(anchor.right, `row ${rowIndex} evidence ends inside its card`).toBeLessThanOrEqual(item.row.right + 1);
        expect(anchor.width, `row ${rowIndex} evidence link width`).toBeGreaterThanOrEqual(44);
        expect(anchor.height, `row ${rowIndex} evidence link height`).toBeGreaterThanOrEqual(44);
      }
    }
  });

  test('PT AI external evidence meets the desktop control-height contract', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto('/ptai-scans', { waitUntil: 'domcontentloaded' });
    await settle(page);

    const anchors = page.locator('.ptai-status-anchor');
    if (await anchors.count() === 0) {
      await expect(page.getByRole('status')).toContainText('No PT AI scans');
      return;
    }

    const undersized = await anchors.evaluateAll((elements) => elements.flatMap((element) => {
      const rect = element.getBoundingClientRect();
      return rect.height >= 32 ? [] : [{
        text: element.textContent?.trim(),
        height: Math.round(rect.height * 10) / 10,
      }];
    }));
    expect(undersized).toEqual([]);
  });

  test('repository row actions meet the mobile touch-target contract', async ({ page }) => {
    await page.goto('/targets?level=repository', { waitUntil: 'domcontentloaded' });
    await settle(page);

    const actions = page.locator('.repository-actions a');
    if (await actions.count() === 0) {
      await expect(page.getByRole('status')).toContainText(/No repositories/);
      return;
    }

    const undersized = await actions.evaluateAll((elements) => elements.flatMap((element) => {
      const rect = element.getBoundingClientRect();
      return rect.width >= 44 && rect.height >= 44 ? [] : [{
        text: element.textContent?.trim(),
        width: Math.round(rect.width * 10) / 10,
        height: Math.round(rect.height * 10) / 10,
      }];
    }));
    expect(undersized).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  });

  test('product registry and its drill-downs fit the phone width with legible text', async ({ page }) => {
    const registry = await page.request.get('/api/ui/products');
    expect(registry.ok()).toBeTruthy();
    const payload = await registry.json() as {
      rows: Array<{ href: string; groups: Array<{ href: string }> }>;
      unregistered_groups: Array<{ href: string }>;
    };
    const card = payload.rows.find((row) => row.groups.length) ?? payload.rows[0];
    const paths = [
      '/targets?level=product',
      ...(card ? [card.href] : []),
      ...payload.unregistered_groups.slice(0, 1).map((group) => group.href),
    ];

    for (const path of paths) {
      await page.goto(path, { waitUntil: 'domcontentloaded' });
      await settle(page);
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
        `${path} must not scroll horizontally`,
      ).toBe(390);
      const undersized = await page.locator('.products-view').evaluate((root) =>
        Array.from(root.querySelectorAll<HTMLElement>('*'))
          .filter((element) =>
            Array.from(element.childNodes).some((node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim())
            && element.getBoundingClientRect().width > 0
            && Number.parseFloat(getComputedStyle(element).fontSize) < 12)
          .map((element) => element.textContent?.trim().slice(0, 30)),
      );
      expect(undersized, `${path} text below 12px`).toEqual([]);
    }
  });

  test('the current breadcrumb remains readable at the 320px edge', async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 780 });
    for (const [path, label] of [
      ['/dashboard/duration', 'Execution duration'],
      ['/pipelines', 'Active pipelines'],
    ] as const) {
      await page.goto(path, { waitUntil: 'domcontentloaded' });
      await settle(page);
      const breadcrumb = page.locator('.topbar .breadcrumbs strong');
      await expect(breadcrumb).toHaveText(label);
      const size = await breadcrumb.evaluate((element) => ({
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
      }));
      expect(size.scrollWidth, `${path} breadcrumb must not be clipped`).toBeLessThanOrEqual(size.clientWidth + 1);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);
    }
  });
});
