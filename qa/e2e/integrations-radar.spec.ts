import { expect, test } from '@playwright/test';

const responsiveWidths = [1440, 1439, 1300, 1222, 1221, 1121, 1024, 781, 780, 390, 360, 320];

test.describe('Integrations radar responsive geometry', () => {
  test.beforeEach(async ({}, testInfo) => {
    test.skip(
      testInfo.project.name !== 'desktop-1440',
      'Responsive geometry runs once and sets its own viewport sizes',
    );
  });

  test('stacks before the radar controls can overlap or truncate', async ({ page }, testInfo) => {
    for (const width of responsiveWidths) {
      await page.setViewportSize({ width, height: 1000 });
      await page.goto('/integrations', { waitUntil: 'domcontentloaded' });
      await expect(page.locator('.radar-connector')).toHaveCount(4);

      const geometry = await page.evaluate(() => {
        const rect = (element: Element) => {
          const value = element.getBoundingClientRect();
          return {
            left: value.left,
            right: value.right,
            top: value.top,
            bottom: value.bottom,
            centerX: value.left + value.width / 2,
            centerY: value.top + value.height / 2,
          };
        };
        const overlaps = (
          first: ReturnType<typeof rect>,
          second: ReturnType<typeof rect>,
        ) => (
          first.left < second.right
          && first.right > second.left
          && first.top < second.bottom
          && first.bottom > second.top
        );
        const canvas = document.querySelector('.integration-canvas');
        const radar = document.querySelector('.integration-radar');
        const stage = document.querySelector('.radar-stage');
        const outerOrbit = document.querySelector('.radar-orbit-b');
        const sweep = document.querySelector('.radar-sweep');
        const core = document.querySelector('.core-node');
        const connectorList = document.querySelector('.connector-list');
        const connectors = [...document.querySelectorAll('.radar-connector')];
        const connectorLabels = [...document.querySelectorAll('.radar-connector > span')];

        if (!canvas || !radar || !stage || !outerOrbit || !sweep || !core || !connectorList) {
          throw new Error('Integration radar geometry is incomplete');
        }

        const canvasRect = rect(canvas);
        const radarRect = rect(radar);
        const stageRect = rect(stage);
        const coreRect = rect(core);
        const connectorListRect = rect(connectorList);
        const connectorRects = connectors.map(rect);
        const controlledSweepCenters = Array.from(
          { length: 8 },
          (_, index) => index * 45,
        ).map((angle) => {
          const sweepElement = sweep as HTMLElement;
          sweepElement.style.animation = 'none';
          sweepElement.style.transform = `translate(-50%, -50%) rotate(${angle}deg)`;
          const sweepRect = rect(sweepElement);
          return {
            angle,
            deltaX: Math.abs(stageRect.centerX - sweepRect.centerX),
            deltaY: Math.abs(stageRect.centerY - sweepRect.centerY),
          };
        });
        const sweepDiameter = Number.parseFloat(getComputedStyle(sweep).width);
        const outerOrbitDiameter = Number.parseFloat(getComputedStyle(outerOrbit).width);
        sweep.removeAttribute('style');

        return {
          canvasColumns: getComputedStyle(canvas).gridTemplateColumns.split(' ').length,
          stageWidth: stage.getBoundingClientRect().width,
          controlledSweepCenters,
          sweepDiameterDelta: Math.abs(sweepDiameter - outerOrbitDiameter),
          centerDeltaX: Math.abs(stageRect.centerX - coreRect.centerX),
          centerDeltaY: Math.abs(stageRect.centerY - coreRect.centerY),
          connectorCoreOverlaps: connectorRects.filter((item) => overlaps(item, coreRect)).length,
          truncatedLabels: connectorLabels.filter((item) => item.scrollWidth > item.clientWidth).length,
          escapedConnectors: connectorRects.filter((item) => (
            item.left < radarRect.left
            || item.right > radarRect.right
            || item.top < radarRect.top
            || item.bottom > radarRect.bottom
          )).length,
          connectorListEscaped: (
            connectorListRect.left < canvasRect.left
            || connectorListRect.right > canvasRect.right
            || connectorListRect.top < canvasRect.top
            || connectorListRect.bottom > canvasRect.bottom
          ),
          documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        };
      });

      expect(geometry.canvasColumns, `${width}px integration columns`).toBe(width <= 1439 ? 1 : 2);
      expect(geometry.stageWidth, `${width}px radar stage width`).toBeGreaterThanOrEqual(
        width <= 780 ? Math.min(400, width - 70) : 400,
      );
      for (const sweepCenter of geometry.controlledSweepCenters) {
        expect(
          sweepCenter.deltaX,
          `${width}px sweep horizontal center at ${sweepCenter.angle}deg`,
        ).toBeLessThan(1);
        expect(
          sweepCenter.deltaY,
          `${width}px sweep vertical center at ${sweepCenter.angle}deg`,
        ).toBeLessThan(1);
      }
      expect(geometry.sweepDiameterDelta, `${width}px sweep radius`).toBeLessThan(1);
      expect(geometry.centerDeltaX, `${width}px horizontal center`).toBeLessThan(1);
      expect(geometry.centerDeltaY, `${width}px vertical center`).toBeLessThan(1);
      expect(geometry.connectorCoreOverlaps, `${width}px connector overlaps`).toBe(0);
      expect(geometry.truncatedLabels, `${width}px truncated labels`).toBe(0);
      expect(geometry.escapedConnectors, `${width}px escaped connectors`).toBe(0);
      expect(geometry.connectorListEscaped, `${width}px connector list bounds`).toBe(false);
      expect(geometry.documentOverflow, `${width}px document overflow`).toBeLessThanOrEqual(0);

      const screenshotPath = testInfo.outputPath(`integrations-radar-${width}.png`);
      await page.screenshot({ path: screenshotPath, fullPage: true, animations: 'disabled' });
      await testInfo.attach(`integrations-radar-${width}`, {
        path: screenshotPath,
        contentType: 'image/png',
      });
    }
  });
});
