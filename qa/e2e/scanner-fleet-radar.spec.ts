import { expect, test } from '@playwright/test';

const responsiveWidths = [1920, 1440, 781, 780, 390, 320];

const runtimeWorker = (name: string, index: number) => ({
  name,
  last_beat_at: '2026-08-14T12:00:00Z',
  status: index < 3 ? 'healthy' : 'stale',
  stale: index >= 3,
  age_seconds: index * 60,
});

test.describe('Scanner fleet radar geometry', () => {
  test.beforeEach(async ({}, testInfo) => {
    test.skip(
      testInfo.project.name !== 'desktop-1440',
      'Fleet radar geometry runs once and controls its own viewport',
    );
  });

  test('sweep reaches and pulses every rendered runtime', async ({ page }, testInfo) => {
    await page.route('**/api/ui/scanner-fleet', async (route) => {
      await route.fulfill({
        json: {
          summary: {
            source: 'worker_heartbeats',
            total: 6,
            returned: 6,
            healthy: 3,
          },
          connectors: [],
          instances: [],
          workers: Array.from(
            { length: 6 },
            (_, index) => runtimeWorker(`runtime_${index + 1}`, index),
          ),
        },
      });
    });

    for (const width of responsiveWidths) {
      await page.setViewportSize({ width, height: 1000 });
      await page.goto('/scanners', { waitUntil: 'domcontentloaded' });
      const field = page.locator('.fleet-field');
      await expect(field).toHaveClass(/fleet-scan-ready/);
      await expect(page.locator('.fleet-node')).toHaveCount(6);

      const geometry = await field.evaluate((element) => {
        const fieldRect = element.getBoundingClientRect();
        const centerX = fieldRect.left + fieldRect.width / 2;
        const centerY = fieldRect.top + fieldRect.height / 2;
        const sweep = element.querySelector('.fleet-sweep');
        if (!sweep) throw new Error('Fleet sweep is missing');
        const sweepRect = sweep.getBoundingClientRect();
        const sweepRadius = Number.parseFloat(getComputedStyle(sweep).width) / 2;
        const nodes = [...element.querySelectorAll('.fleet-node')];
        const links = [...element.querySelectorAll('.fleet-links > i')];
        const core = element.querySelector('.fleet-core');
        if (!core) throw new Error('Fleet core is missing');
        const coreRect = core.getBoundingClientRect();
        const coreGap = (node: Element) => {
          const nodeRect = node.getBoundingClientRect();
          const coreCenterX = coreRect.left + coreRect.width / 2;
          const coreCenterY = coreRect.top + coreRect.height / 2;
          const closestX = Math.max(
            nodeRect.left,
            Math.min(coreCenterX, nodeRect.right),
          );
          const closestY = Math.max(
            nodeRect.top,
            Math.min(coreCenterY, nodeRect.bottom),
          );
          return Math.hypot(
            closestX - coreCenterX,
            closestY - coreCenterY,
          ) - coreRect.width / 2;
        };

        return {
          sweepCenterDeltaX: Math.abs(
            centerX - (sweepRect.left + sweepRect.width / 2),
          ),
          sweepCenterDeltaY: Math.abs(
            centerY - (sweepRect.top + sweepRect.height / 2),
          ),
          nodes: nodes.map((node) => {
            const signal = node.querySelector(':scope > i');
            if (!signal) throw new Error('Fleet signal is missing');
            const signalRect = signal.getBoundingClientRect();
            const nodeRect = node.getBoundingClientRect();
            const deltaX = signalRect.left + signalRect.width / 2 - centerX;
            const deltaY = signalRect.top + signalRect.height / 2 - centerY;
            const signalAngle = (
              Math.atan2(deltaX, -deltaY) * 180 / Math.PI + 360
            ) % 360;
            const delay = Number.parseFloat(
              getComputedStyle(node, '::before').animationDelay,
            );
            const pulseAngle = (delay * 45 + 22.5) % 360;
            const rawAngleError = Math.abs(signalAngle - pulseAngle);
            return {
              radialGap: sweepRadius - Math.hypot(deltaX, deltaY),
              angleError: Math.min(rawAngleError, 360 - rawAngleError),
              haloClipped: (
                nodeRect.left - 12 < fieldRect.left - 1
                || nodeRect.right + 12 > fieldRect.right + 1
                || nodeRect.top - 9 < fieldRect.top - 1
                || nodeRect.bottom + 9 > fieldRect.bottom + 1
              ),
              coreGap: coreGap(node),
              delay,
            };
          }),
          nodeOverlaps: nodes.flatMap((node, index) => {
            const rect = node.getBoundingClientRect();
            return nodes.slice(index + 1).map((other) => {
              const otherRect = other.getBoundingClientRect();
              return !(
                rect.right <= otherRect.left
                || rect.left >= otherRect.right
                || rect.bottom <= otherRect.top
                || rect.top >= otherRect.bottom
              );
            });
          }),
          links: links.map((link, index) => {
            const signal = nodes[index]?.querySelector(':scope > i');
            if (!signal) throw new Error('Fleet link target is missing');
            const linkLength = Number.parseFloat(getComputedStyle(link).width);
            const signalRect = signal.getBoundingClientRect();
            return Math.abs(
              linkLength - Math.hypot(
                signalRect.left + signalRect.width / 2 - centerX,
                signalRect.top + signalRect.height / 2 - centerY,
              ),
            );
          }),
          documentOverflow: (
            document.documentElement.scrollWidth
            - document.documentElement.clientWidth
          ),
        };
      });

      expect(geometry.sweepCenterDeltaX, `${width}px sweep center x`).toBeLessThan(1);
      expect(geometry.sweepCenterDeltaY, `${width}px sweep center y`).toBeLessThan(1);
      expect(geometry.documentOverflow, `${width}px document overflow`).toBeLessThanOrEqual(0);
      expect(geometry.nodeOverlaps, `${width}px node overlap`).not.toContain(true);
      expect(geometry.links, `${width}px spoke count`).toHaveLength(6);
      expect(Math.max(...geometry.links), `${width}px spoke target error`).toBeLessThan(.2);
      expect(new Set(geometry.nodes.map((node) => node.delay)).size).toBe(6);
      for (const [index, node] of geometry.nodes.entries()) {
        expect(node.radialGap, `${width}px node ${index + 1} radial coverage`).toBeGreaterThanOrEqual(20);
        expect(node.angleError, `${width}px node ${index + 1} pulse phase`).toBeLessThan(.05);
        expect(node.haloClipped, `${width}px node ${index + 1} halo bounds`).toBe(false);
        expect(node.coreGap, `${width}px node ${index + 1} core clearance`).toBeGreaterThanOrEqual(0);
      }

      const pulseStates = await field.evaluate((element) => (
        [...element.querySelectorAll('.fleet-node')].map((node) => {
          const delayMs = Number.parseFloat(
            getComputedStyle(node, '::before').animationDelay,
          ) * 1000;
          node.getAnimations({ subtree: true }).forEach((animation) => {
            animation.pause();
            animation.currentTime = delayMs + 320;
          });
          return {
            haloOpacity: Number.parseFloat(
              getComputedStyle(node, '::before').opacity,
            ),
            signalTransform: getComputedStyle(node.querySelector('i')!).transform,
            labelShadow: getComputedStyle(node.querySelector('span')!).textShadow,
          };
        })
      ));
      for (const [index, state] of pulseStates.entries()) {
        expect(state.haloOpacity, `${width}px node ${index + 1} halo`).toBeGreaterThan(.75);
        expect(state.signalTransform, `${width}px node ${index + 1} signal`).not.toBe('none');
        expect(state.labelShadow, `${width}px node ${index + 1} label`).not.toBe('none');
      }

      if (width === 1440 || width === 390) {
        const firstDelay = geometry.nodes[0].delay * 1000 + 320;
        await field.evaluate((element, currentTime) => {
          element.getAnimations({ subtree: true }).forEach((animation) => {
            animation.pause();
            animation.currentTime = currentTime;
          });
        }, firstDelay);
        const screenshotPath = testInfo.outputPath(`fleet-radar-pulse-${width}.png`);
        await field.screenshot({ path: screenshotPath });
        await testInfo.attach(`fleet-radar-pulse-${width}`, {
          path: screenshotPath,
          contentType: 'image/png',
        });
      }
    }

    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/scanners', { waitUntil: 'domcontentloaded' });
    const reducedMotion = await page.locator('.fleet-field').evaluate((field) => {
      const sweep = field.querySelector('.fleet-sweep');
      const firstNode = field.querySelector('.fleet-node');
      if (!sweep || !firstNode) throw new Error('Fleet motion elements are missing');
      return {
        duration: Number.parseFloat(getComputedStyle(sweep).animationDuration),
        iterations: getComputedStyle(sweep).animationIterationCount,
        haloDuration: Number.parseFloat(
          getComputedStyle(firstNode, '::before').animationDuration,
        ),
      };
    });
    expect(reducedMotion.duration).toBeLessThanOrEqual(.00001);
    expect(reducedMotion.haloDuration).toBeLessThanOrEqual(.00001);
    expect(reducedMotion.iterations).toBe('1');
  });

  test('remeasures a same-count live runtime replacement', async ({ page }) => {
    await page.clock.install();
    let refreshed = false;
    await page.route('**/api/ui/scanner-fleet', async (route) => {
      const names = refreshed
        ? ['runtime_6', 'runtime_2', 'replacement_runtime', 'runtime_4', 'runtime_5', 'runtime_1']
        : Array.from({ length: 6 }, (_, index) => `runtime_${index + 1}`);
      await route.fulfill({
        json: {
          summary: { source: 'worker_heartbeats', total: 6, returned: 6, healthy: 3 },
          connectors: [],
          instances: [],
          workers: names.map(runtimeWorker),
        },
      });
    });

    await page.goto('/scanners', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.fleet-field')).toHaveClass(/fleet-scan-ready/);
    await expect(page.locator('.fleet-node').first()).toContainText('runtime_1');

    refreshed = true;
    await page.clock.runFor(30_001);
    await expect(page.locator('.fleet-node').first()).toContainText('runtime_6');
    await expect(page.locator('.fleet-node').nth(2)).toContainText('replacement_runtime');

    const refreshedGeometry = await page.locator('.fleet-field').evaluate((field) => ({
      delays: [...field.querySelectorAll<HTMLElement>('.fleet-node')].map(
        (node) => node.style.getPropertyValue('--fleet-scan-delay'),
      ),
      spokeWidths: [...field.querySelectorAll<HTMLElement>('.fleet-links > i')].map(
        (link) => link.style.width,
      ),
      animationTimes: [
        ...field.querySelector<HTMLElement>('.fleet-sweep')!.getAnimations(),
        ...[...field.querySelectorAll<HTMLElement>('.fleet-node')].flatMap(
          (node) => node.getAnimations({ subtree: true }),
        ),
      ].map((animation) => animation.currentTime as number | null),
    }));
    expect(refreshedGeometry.delays).toHaveLength(6);
    expect(refreshedGeometry.delays.every(Boolean)).toBe(true);
    expect(refreshedGeometry.spokeWidths).toHaveLength(6);
    expect(refreshedGeometry.spokeWidths.every((width) => Number.parseFloat(width) > 0)).toBe(true);
    const runningTimes = refreshedGeometry.animationTimes.filter(
      (time): time is number => time !== null,
    );
    expect(runningTimes).toHaveLength(19);
    expect(Math.max(...runningTimes) - Math.min(...runningTimes)).toBeLessThan(2);
  });
});
