import { expect, test, type Page, type Response } from '@playwright/test';
import { attachDiagnostics, installDiagnostics } from './support/diagnostics';
import { createFinding } from './support/findings';
import { attachFinding } from './support/page-audit';
import { recordBadNavigation, type HttpProbe } from './support/navigation';
import { compatibilityInventory, routeInventory } from './support/routes';

interface SafeLink {
  index: number;
  href: string;
  text: string;
}

async function navigate(page: Page, url: string): Promise<Response | null> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      return await page.goto(url, { waitUntil: 'domcontentloaded' });
    } catch (error) {
      const message = String(error);
      const retryable = message.includes('ERR_ABORTED') || message.includes('interrupted by another navigation');
      if (attempt === 1 || !retryable) throw error;
      await page.waitForTimeout(300);
    }
  }
  return null;
}

async function waitForInteractiveSurface(page: Page): Promise<void> {
  // Deep-linked drawers mount after the document event and make the
  // background application inert. Collect/click links only after React has
  // committed that state, otherwise the audit can snapshot a background link
  // just before the modal backdrop starts intercepting pointer events.
  await page.waitForLoadState('networkidle', { timeout: 2_500 }).catch(() => undefined);
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

async function probeGet(page: Page, url: string): Promise<HttpProbe> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const result = await page.evaluate(async (target) => {
        const response = await fetch(target, { method: 'GET', credentials: 'same-origin' });
        await response.text();
        return { status: response.status, url: response.url };
      }, url);
      return { status: () => result.status, url: () => result.url };
    } catch {
      if (page.isClosed()) break;
      if (attempt === 0) await page.waitForTimeout(250);
    }
  }
  return { status: () => 599, url: () => url };
}

function isSafeAppUrl(rawHref: string, source: string, baseURL: string): boolean {
  try {
    const url = new URL(rawHref, source);
    const base = new URL(baseURL);
    if (url.origin !== base.origin) return false;
    if (/\/(export|report\/|sarif)(\/|\.|$)/i.test(url.pathname)) return false;
    if (/(logout|delete|disable|cancel|rescan|reupload|repoll|sync|run|scan)/i.test(url.search)) return false;
    return true;
  } catch {
    return false;
  }
}

function linkContractKey(link: SafeLink): string {
  const url = new URL(link.href);
  const routeShape = url.pathname
    .replace(/\/\d+(?=\/|$)/g, '/{id}')
    .replace(/[0-9a-f]{8}-[0-9a-f-]{27,}/gi, '{uuid}');
  // Query/hash values represent filter and pagination states and are therefore
  // deliberately retained. Repeated entity rows without URL state collapse to
  // one representative interaction per route shape.
  return `${routeShape}${url.search}${url.hash}`;
}

test.describe('safe navigation and URL-state contracts', () => {
  test.beforeEach(async ({}, testInfo) => {
    test.skip(testInfo.project.name !== 'desktop-1440', 'Interactions run once at 1440px');
  });

  for (const route of routeInventory) {
    test(`links, filters, back and reload: ${route.title} [${route.id}]`, async ({ page }, testInfo) => {
      test.setTimeout(120_000);
      const diagnostics = await installDiagnostics(page, testInfo);
      const baseURL = String(testInfo.project.use.baseURL);
      const viewport = page.viewportSize() ?? { width: 0, height: 0 };
      const sourceResponse = await page.goto(route.path, { waitUntil: 'domcontentloaded' });
      expect(sourceResponse?.status() ?? 599).toBeLessThan(500);
      await waitForInteractiveSurface(page);
      const sourceUrl = page.url();
      const maxLinks = Number(process.env.QA_MAX_SAFE_LINKS ?? 0);

      const links = await page.locator('main a[href]').evaluateAll((elements) => elements.map((element, index) => ({
        index,
        href: (element as HTMLAnchorElement).href,
        text: (element.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 100),
        target: (element as HTMLAnchorElement).target,
        download: (element as HTMLAnchorElement).hasAttribute('download'),
        disabled: element.getAttribute('aria-disabled') === 'true' || element.classList.contains('disabled'),
        visible:
          (element as HTMLElement).getBoundingClientRect().width > 0
          && (element as HTMLElement).getBoundingClientRect().height > 0
          && !element.closest('[inert], [aria-hidden="true"]'),
      }))) as Array<SafeLink & { target: string; download: boolean; disabled: boolean; visible: boolean }>;

      const prioritizedAll = links
        .filter((link) => link.visible && !link.disabled && !link.target && !link.download && isSafeAppUrl(link.href, sourceUrl, baseURL))
        .sort((a, b) => {
          const score = (link: SafeLink) => {
            const url = new URL(link.href);
            return (url.search ? 4 : 0) + (url.hash ? 3 : 0) + (/next|previous|page|filter|all|active/i.test(link.text) ? 2 : 0);
          };
          return score(b) - score(a);
        })
        .filter((link, index, all) => all.findIndex((candidate) => linkContractKey(candidate) === linkContractKey(link)) === index);
      const prioritized = maxLinks > 0 ? prioritizedAll.slice(0, maxLinks) : prioritizedAll;
      // Large registries can expose many distinct query-addressed states. Keep
      // the exhaustive contract instead of truncating it, while giving each
      // navigation/reload/Back cycle a finite budget proportional to the work.
      testInfo.setTimeout(Math.max(
        testInfo.timeout,
        30_000 + prioritized.length * 6_000,
      ));

      for (const link of prioritized) {
        await navigate(page, sourceUrl);
        await waitForInteractiveSurface(page);
        const candidates = page.locator('main a[href]');
        if (link.index >= await candidates.count()) continue;
        const candidateIsActionable = await candidates.nth(link.index).evaluate((element) => {
          const rect = element.getBoundingClientRect();
          return rect.width > 0
            && rect.height > 0
            && !element.closest('[inert], [aria-hidden="true"]');
        });
        if (!candidateIsActionable) continue;
        const beforeClick = page.url();
        await candidates.nth(link.index).click({ noWaitAfter: true });
        await page.waitForURL((url) => url.href !== beforeClick, { timeout: 5_000 }).catch(() => undefined);
        await page.waitForLoadState('domcontentloaded').catch(() => undefined);
        const destination = page.url();

        const destinationResponse = await probeGet(page, destination);
        await recordBadNavigation(destinationResponse, testInfo, viewport, route, [
          `Open ${route.path}`, `Click “${link.text || link.href}”`,
        ]);

        if (new URL(destination).origin !== new URL(baseURL).origin) {
          const finding = createFinding({
            systemicId: 'NAVIGATION_ESCAPES_APP_ORIGIN', severity: 'P0', category: 'navigation',
            title: 'Safe in-app link unexpectedly leaves ScanOps', url: sourceUrl, route: route.id,
            state: route.state ?? 'local',
            steps: [`Open ${route.path}`, `Click “${link.text || link.href}”`],
            expected: 'A read-only application link remains on the ScanOps origin.', actual: `Navigated to ${destination}.`,
            evidence: { selector: `main a[href="${link.href}"]` },
          }, testInfo.project.name, viewport);
          await attachFinding(testInfo, finding);
        }

        const beforeReload = page.url();
        const reloadResponse = await page.reload({ waitUntil: 'domcontentloaded' }).catch(async () => navigate(page, beforeReload));
        await recordBadNavigation(reloadResponse, testInfo, viewport, route, [
          `Open ${route.path}`, `Click “${link.text || link.href}”`, 'Reload the destination',
        ]);
        if (page.url() !== beforeReload) {
          const finding = createFinding({
            systemicId: 'NAVIGATION_URL_STATE_NOT_PERSISTENT', severity: 'P1', category: 'interaction',
            title: 'Read-only navigation state is lost on reload', url: beforeReload, route: route.id,
            state: route.state ?? 'local',
            steps: [`Open ${route.path}`, `Click “${link.text || link.href}”`, 'Reload the destination'],
            expected: `Reload remains at ${beforeReload}.`, actual: `Reload changed the URL to ${page.url()}.`,
            evidence: { selector: `main a[href="${link.href}"]` },
          }, testInfo.project.name, viewport);
          await attachFinding(testInfo, finding);
        }

        if (beforeReload !== sourceUrl) {
          const backSteps = [
            `Open ${route.path}`, `Click “${link.text || link.href}”`,
            'Reload the destination', 'Use browser Back',
          ];
          const backResponse = await page.goBack({ waitUntil: 'domcontentloaded' }).catch((): HttpProbe => ({
            status: () => 599,
            url: () => page.url(),
          }));
          await recordBadNavigation(backResponse, testInfo, viewport, route, backSteps, {
            allowNullHistoryResponse: true,
          });
          if (page.url() !== sourceUrl) {
            const finding = createFinding({
              systemicId: 'BROWSER_BACK_STATE_BROKEN', severity: 'P1', category: 'navigation',
              title: 'Browser Back does not restore the source view', url: beforeReload, route: route.id,
              state: route.state ?? 'local',
              steps: [`Open ${route.path}`, `Click “${link.text || link.href}”`, 'Reload the destination', 'Use browser Back'],
              expected: `Return to ${sourceUrl}.`, actual: `Returned to ${page.url()}.`,
              evidence: {},
            }, testInfo.project.name, viewport);
            await attachFinding(testInfo, finding);
          }
        }
      }

      // GET forms are safe to exercise. POST forms remain blocked and are never submitted.
      await navigate(page, sourceUrl);
      const getSelects = await page.locator('main form select[name]').evaluateAll((elements) => elements.map((element, index) => {
        const select = element as HTMLSelectElement;
        const form = select.form;
        const options = [...select.options];
        const values = options
          .filter((option) => !option.disabled)
          .map((option) => option.value);
        return {
          index,
          name: select.name,
          current: select.value,
          values,
          method: (form?.method ?? 'get').toLowerCase(),
          action: form?.action ?? '',
        };
      }));

      const filterCases = getSelects
        .filter((item) => item.method === 'get')
        .flatMap((item) => item.values.map((value) => ({ ...item, value })));

      for (const filter of filterCases) {
        await navigate(page, sourceUrl);
        const selects = page.locator('main form select[name]');
        if (filter.index >= await selects.count()) continue;
        const before = page.url();
        await Promise.all([
          page.waitForURL((url) => url.href !== before, { timeout: 2_500 }).catch(() => undefined),
          selects.nth(filter.index).selectOption(String(filter.value)),
        ]);
        await page.waitForLoadState('domcontentloaded').catch(() => undefined);
        await page.waitForTimeout(150);
        if (page.url() === before) {
          await Promise.all([
            page.waitForURL((url) => url.href !== before, { timeout: 2_500 }).catch(() => undefined),
            selects.nth(filter.index).evaluate((element) => (element as HTMLSelectElement).form?.requestSubmit()),
          ]);
          await page.waitForLoadState('domcontentloaded').catch(() => undefined);
        }
        const filteredUrl = page.url();
        const filteredResponse = await probeGet(page, filteredUrl);
        await recordBadNavigation(filteredResponse, testInfo, viewport, route, [
          `Open ${route.path}`, `Set ${filter.name} to ${filter.value || '(empty)'}`,
        ]);
        const selectedInUrl = new URL(filteredUrl).searchParams.get(filter.name);
        const valueAddressable =
          filter.value === ''
            ? selectedInUrl === null || selectedInUrl === ''
            : selectedInUrl === filter.value;
        if (!valueAddressable) {
          const finding = createFinding({
            systemicId: 'FILTER_NOT_ADDRESSABLE_IN_URL', severity: 'P1', category: 'interaction',
            title: 'GET filter does not preserve its selected value in the URL', url: filteredUrl, route: route.id,
            state: route.state ?? 'local',
            steps: [`Open ${route.path}`, `Set ${filter.name} to ${filter.value || '(empty)'}`],
            expected: filter.value === ''
              ? `URL omits ${filter.name} or contains an empty value.`
              : `URL contains ${filter.name}=${filter.value}.`,
            actual: `URL contains ${filter.name}=${selectedInUrl ?? '(missing)'}.`,
            evidence: { selector: `select[name="${filter.name}"]` },
          }, testInfo.project.name, viewport);
          await attachFinding(testInfo, finding);
        }

        const beforeReload = page.url();
        const reloadResponse = await page.reload({ waitUntil: 'domcontentloaded' }).catch(async () => navigate(page, beforeReload));
        await recordBadNavigation(reloadResponse, testInfo, viewport, route, [
          `Open ${route.path}`, `Set ${filter.name} to ${filter.value || '(empty)'}`, 'Reload the filtered view',
        ]);
        if (page.url() !== beforeReload) {
          const finding = createFinding({
            systemicId: 'FILTER_URL_STATE_NOT_PERSISTENT', severity: 'P1', category: 'interaction',
            title: 'GET filter state is lost on reload', url: beforeReload, route: route.id,
            state: route.state ?? 'local',
            steps: [`Open ${route.path}`, `Set ${filter.name} to ${filter.value || '(empty)'}`, 'Reload the page'],
            expected: `Reload remains at ${beforeReload}.`, actual: `Reload changed the URL to ${page.url()}.`,
            evidence: { selector: `select[name="${filter.name}"]` },
          }, testInfo.project.name, viewport);
          await attachFinding(testInfo, finding);
        }
        if (page.url() !== sourceUrl) {
          const backSteps = [
            `Open ${route.path}`, `Set ${filter.name} to ${filter.value || '(empty)'}`,
            'Reload the page', 'Use browser Back',
          ];
          const backResponse = await page.goBack({ waitUntil: 'domcontentloaded' }).catch((): HttpProbe => ({
            status: () => 599,
            url: () => page.url(),
          }));
          await recordBadNavigation(backResponse, testInfo, viewport, route, backSteps, {
            allowNullHistoryResponse: true,
          });
          if (page.url() !== sourceUrl) {
            const finding = createFinding({
              systemicId: 'FILTER_BROWSER_BACK_STATE_BROKEN', severity: 'P1', category: 'interaction',
              title: 'Browser Back does not restore the pre-filter view', url: beforeReload, route: route.id,
              state: route.state ?? 'local',
              steps: [`Open ${route.path}`, `Set ${filter.name} to ${filter.value || '(empty)'}`, 'Reload the page', 'Use browser Back'],
              expected: `Return to ${sourceUrl}.`, actual: `Returned to ${page.url()}.`,
              evidence: { selector: `select[name="${filter.name}"]` },
            }, testInfo.project.name, viewport);
            await attachFinding(testInfo, finding);
          }
        }
      }

      await attachDiagnostics(testInfo, diagnostics);
    });
  }

  for (const route of compatibilityInventory) {
    test(`compatibility redirect: ${route.title} [${route.id}]`, async ({ page }, testInfo) => {
      const diagnostics = await installDiagnostics(page, testInfo);
      const response = await navigate(page, route.path);
      expect(response?.status() ?? 599).toBeLessThan(500);

      const expected = new URL(String(route.expectedPath), String(testInfo.project.use.baseURL));
      const actual = new URL(page.url());
      expect(`${actual.pathname}${actual.search}`).toBe(`${expected.pathname}${expected.search}`);

      await attachDiagnostics(testInfo, diagnostics);
    });
  }

  test('Product registry quarter filter Back and HTTP-error matrix', async ({ page }, testInfo) => {
    test.setTimeout(120_000);
    const diagnostics = await installDiagnostics(page, testInfo);
    const route = routeInventory.find((candidate) => candidate.id === 'canonical-products');
    test.skip(!route, 'Products route is excluded by the active tag filter');
    if (!route) return;

    const viewport = page.viewportSize() ?? { width: 0, height: 0 };
    const sourceResponse = await navigate(page, route.path);
    await recordBadNavigation(sourceResponse, testInfo, viewport, route, [`Open ${route.path}`]);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    const sourceUrl = page.url();
    const registry = await page.request.get('/api/ui/products');
    expect(registry.ok()).toBeTruthy();
    const periods = ((await registry.json()).periods ?? []) as { key: string; label: string }[];
    const modes = [
      { value: 'new', label: 'New in quarter' },
      { value: 'total', label: 'Total at quarter end' },
    ];

    for (const period of periods.slice(0, 4)) {
      for (const mode of modes) {
        await navigate(page, sourceUrl);
        const target = new URL(route.path, sourceUrl);
        target.searchParams.set('period', period.key);
        target.searchParams.set('mode', mode.value);
        const steps = [`Open ${route.path}`, `Navigate to period=${period.key}, mode=${mode.value}`];

        const response = await navigate(page, target.href);
        await recordBadNavigation(response, testInfo, viewport, route, steps);
        await expect.soft(
          page.getByRole('group', { name: 'Quarter slice' }).getByRole('button', { name: mode.label }),
        ).toHaveAttribute('aria-pressed', 'true');

        const beforeReload = page.url();
        const reloadResponse = await page.reload({ waitUntil: 'domcontentloaded' }).catch(async () => navigate(page, beforeReload));
        await recordBadNavigation(reloadResponse, testInfo, viewport, route, [...steps, 'Reload the filtered view']);
        expect.soft(page.url()).toBe(beforeReload);

        const backSteps = [...steps, 'Reload the filtered view', 'Use browser Back'];
        const backResponse = await page.goBack({ waitUntil: 'domcontentloaded' }).catch((): HttpProbe => ({
          status: () => 599,
          url: () => page.url(),
        }));
        await recordBadNavigation(backResponse, testInfo, viewport, route, backSteps, {
          allowNullHistoryResponse: true,
        });
        if (page.url() !== sourceUrl) {
          const finding = createFinding({
            systemicId: 'PRODUCT_REGISTRY_BROWSER_BACK_STATE_BROKEN', severity: 'P1', category: 'interaction',
            title: 'Product registry browser Back does not restore the unfiltered view',
            url: beforeReload, route: route.id, state: route.state ?? 'local',
            steps: backSteps,
            expected: `Return to ${sourceUrl}.`, actual: `Returned to ${page.url()}.`,
            evidence: {},
          }, testInfo.project.name, viewport);
          await attachFinding(testInfo, finding);
        }
      }
    }

    await attachDiagnostics(testInfo, diagnostics);
  });
});
