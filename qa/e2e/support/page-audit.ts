import AxeBuilder from '@axe-core/playwright';
import type { Page, TestInfo } from '@playwright/test';
import { createFinding, severityForAxeImpact } from './findings';
import type { DiagnosticEvent, Finding, RouteCase } from './types';

interface LayoutSignals {
  viewportOverflow: null | { documentWidth: number; viewportWidth: number };
  overflowingText: Array<{ selector: string; text: string; clientWidth: number; scrollWidth: number }>;
  tableCollisions: Array<{ left: string; right: string; overlapX: number; overlapY: number }>;
  tinyControls: Array<{ selector: string; text: string; width: number; height: number }>;
  controlMinimumSize: number;
  tinyText: Array<{ selector: string; text: string; fontSize: number }>;
  semanticClickTargets: Array<{ selector: string; text: string; tag: string }>;
  duplicateIds: string[];
}

function context(testInfo: TestInfo, route: RouteCase, page: Page, screenshot: string) {
  const viewport = page.viewportSize() ?? { width: 0, height: 0 };
  return {
    project: testInfo.project.name,
    viewport,
    url: page.url(),
    route: route.id,
    state: route.state ?? 'local',
    screenshot,
  };
}

export async function attachFinding(testInfo: TestInfo, finding: Finding): Promise<void> {
  await testInfo.attach('qa-finding', {
    body: Buffer.from(JSON.stringify(finding)),
    contentType: 'application/vnd.scanops.qa-finding+json',
  });
}

export async function collectLayoutSignals(page: Page): Promise<LayoutSignals> {
  return page.evaluate(() => {
    const isVisible = (element: Element): element is HTMLElement => {
      if (!(element instanceof HTMLElement)) return false;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      const clippedAssistiveText =
        rect.width <= 4
        && rect.height <= 4
        && (style.clip !== 'auto' || style.clipPath !== 'none');
      return (
        style.visibility !== 'hidden'
        && style.display !== 'none'
        && rect.width > 0
        && rect.height > 0
        && !clippedAssistiveText
      );
    };
    const selector = (element: Element): string => {
      if ((element as HTMLElement).id) return `#${CSS.escape((element as HTMLElement).id)}`;
      const testId = element.getAttribute('data-testid');
      if (testId) return `[data-testid="${CSS.escape(testId)}"]`;
      const classes = [...element.classList].slice(0, 3).map((name) => `.${CSS.escape(name)}`).join('');
      return `${element.tagName.toLowerCase()}${classes}`;
    };
    const text = (element: Element): string => (element.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 120);
    const visibleTextGeometryOverflows = (container: Element): boolean => {
      const bounds = container.getBoundingClientRect();
      const textNodes = [container, ...container.querySelectorAll('*')]
        .flatMap((element) => [...element.childNodes])
        .filter((node): node is Text =>
          node.nodeType === Node.TEXT_NODE
          && Boolean(node.textContent?.trim())
          && !node.parentElement?.closest('input, select, textarea, option'),
        );
      return textNodes.some((node) => {
        const range = document.createRange();
        range.selectNodeContents(node);
        return [...range.getClientRects()].some((rect) =>
          rect.width > 0
          && rect.height > 0
          && (
            rect.left < bounds.left - 3
            || rect.right > bounds.right + 3
            || rect.top < bounds.top - 3
            || rect.bottom > bounds.bottom + 3
          ),
        );
      });
    };

    const reportedDocumentWidth = Math.max(
      document.documentElement.scrollWidth,
      document.body?.scrollWidth ?? 0,
    );
    const viewportWidth = document.documentElement.clientWidth;
    const scrollingElement = document.scrollingElement;
    const initialScrollX = scrollingElement?.scrollLeft ?? window.scrollX;
    const initialScrollBehavior = document.documentElement.style.scrollBehavior;
    document.documentElement.style.scrollBehavior = 'auto';
    if (scrollingElement) scrollingElement.scrollLeft = reportedDocumentWidth;
    const reachedScrollX = scrollingElement?.scrollLeft ?? window.scrollX;
    const rootCanScrollHorizontally = Math.abs(reachedScrollX - initialScrollX) > 2 || initialScrollX > 2;
    if (scrollingElement) scrollingElement.scrollLeft = initialScrollX;
    document.documentElement.style.scrollBehavior = initialScrollBehavior;
    const documentWidth = rootCanScrollHorizontally ? reportedDocumentWidth : viewportWidth;

    const overflowingText = [...document.querySelectorAll('th, td, button, a, label, code, pre, [role="button"], [role="tab"]')]
      .filter(isVisible)
      .filter((element) => {
        const style = getComputedStyle(element);
        const hasOverflow = element.scrollWidth > element.clientWidth + 3;
        const scrollOwner = element.closest('.table-container, .responsive-table-scroll, [data-scroll-region]');
        const intentionallyScrollable = [element, scrollOwner]
          .filter((candidate): candidate is Element => Boolean(candidate))
          .some((candidate) => ['auto', 'scroll'].includes(getComputedStyle(candidate).overflowX));
        const explicitlyTruncated =
          style.textOverflow === 'ellipsis'
          || (style.webkitLineClamp !== 'none' && style.webkitLineClamp !== '0');
        const formControlOnlyOverflow =
          Boolean(element.querySelector('input, select, textarea'))
          && !visibleTextGeometryOverflows(element);
        return (
          hasOverflow
          && !intentionallyScrollable
          && !explicitlyTruncated
          && !formControlOnlyOverflow
          && text(element).length > 0
        );
      })
      .slice(0, 20)
      .map((element) => ({
        selector: selector(element),
        text: text(element),
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
      }));

    const tableCollisions: LayoutSignals['tableCollisions'] = [];
    for (const row of document.querySelectorAll('tr')) {
      const cells = [...row.querySelectorAll(':scope > th, :scope > td')].filter(isVisible);
      for (let i = 0; i < cells.length - 1; i += 1) {
        const left = cells[i].getBoundingClientRect();
        const right = cells[i + 1].getBoundingClientRect();
        const overlapX = Math.min(left.right, right.right) - Math.max(left.left, right.left);
        const overlapY = Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top);
        if (overlapX > 2 && overlapY > 2) {
          tableCollisions.push({
            left: `${selector(cells[i])}: ${text(cells[i])}`,
            right: `${selector(cells[i + 1])}: ${text(cells[i + 1])}`,
            overlapX: Math.round(overlapX),
            overlapY: Math.round(overlapY),
          });
        }
      }
    }

    const mobileControlContract = viewportWidth <= 780;
    const controlMinimumSize = mobileControlContract ? 44 : 32;
    const controlSelector = [
      'button',
      'input',
      'select',
      'textarea',
      '[role="button"]',
      '[role="tab"]',
      'a.btn[href]',
      'a[data-action][href]',
      '.page-actions a[href]',
      '.table-actions a[href]',
      '.repository-actions a[href]',
      '.ptai-status-anchor[href]',
      'td.action a[href]',
      'td.actions a[href]',
      '[data-action-region] a[href]',
      '.sidebar a[href]',
      '.module-tabs a[href]',
    ].join(', ');
    const tinyControls = [...document.querySelectorAll(controlSelector)]
      .filter(isVisible)
      .map((element) => {
        const measuredTarget = element instanceof HTMLInputElement
          && ['checkbox', 'radio'].includes(element.type)
          ? element.closest('label') ?? element
          : element;
        return { element, rect: measuredTarget.getBoundingClientRect() };
      })
      .filter(({ rect }) =>
        mobileControlContract
          ? rect.width < controlMinimumSize || rect.height < controlMinimumSize
          : rect.height < controlMinimumSize,
      )
      .slice(0, 20)
      .map(({ element, rect }) => ({
        selector: selector(element),
        text: text(element),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      }));

    const tinyText = [...document.querySelectorAll('button, a, label, th, td, p, span, small, dt, dd')]
      .filter(isVisible)
      .map((element) => ({ element, fontSize: Number.parseFloat(getComputedStyle(element).fontSize) }))
      .filter(({ element, fontSize }) => fontSize < 12 && text(element).length > 0)
      .slice(0, 20)
      .map(({ element, fontSize }) => ({
        selector: selector(element),
        text: text(element),
        fontSize,
      }));

    const semanticClickTargets = [...document.querySelectorAll('[onclick]')]
      .filter(isVisible)
      .filter((element) => !['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'SUMMARY'].includes(element.tagName))
      .filter((element) => !element.hasAttribute('role'))
      .slice(0, 20)
      .map((element) => ({ selector: selector(element), text: text(element), tag: element.tagName.toLowerCase() }));

    const ids = [...document.querySelectorAll('[id]')].map((element) => element.id);
    const duplicateIds = [...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))].slice(0, 20);

    return {
      viewportOverflow: documentWidth > viewportWidth + 2 ? { documentWidth, viewportWidth } : null,
      overflowingText,
      tableCollisions: tableCollisions.slice(0, 20),
      tinyControls,
      controlMinimumSize,
      tinyText,
      semanticClickTargets,
      duplicateIds,
    };
  });
}

export async function runKeyboardAudit(page: Page): Promise<{
  focusStops: Array<{ tag: string; text: string; ariaLabel: string | null }>;
  missingIndicator: Array<{ tag: string; text: string }>;
  stuck: boolean;
}> {
  await page.evaluate(() => {
    const active = document.activeElement;
    if (active instanceof HTMLElement) active.blur();
  });
  const focusStops: Array<{ tag: string; text: string; ariaLabel: string | null }> = [];
  const missingIndicator: Array<{ tag: string; text: string }> = [];
  let previous = '';
  let repeated = 0;

  for (let index = 0; index < 30; index += 1) {
    let state: null | {
      identity: string;
      tag: string;
      text: string;
      ariaLabel: string | null;
      visible: boolean;
      indicator: boolean;
    };
    try {
      await page.keyboard.press('Tab');
      state = await page.evaluate(() => {
        const active = document.activeElement as HTMLElement | null;
        if (!active || active === document.body) return null;
        const style = getComputedStyle(active);
        const rect = active.getBoundingClientRect();
        const domPath: string[] = [];
        let cursor: HTMLElement | null = active;
        while (cursor && cursor !== document.body) {
          const parent: HTMLElement | null = cursor.parentElement;
          const siblingIndex = parent
            ? Array.from(parent.children).indexOf(cursor)
            : 0;
          domPath.push(`${cursor.tagName}:${siblingIndex}`);
          cursor = parent;
        }
        return {
          identity: [
            domPath.reverse().join('/'),
            active.tagName,
            active.id,
            active.className,
            active.getAttribute('href') ?? '',
            active.getAttribute('name') ?? '',
            active.getAttribute('aria-label') ?? '',
            (active.innerText || active.getAttribute('value') || '').replace(/\s+/g, ' ').trim().slice(0, 80),
          ].join('|'),
          tag: active.tagName.toLowerCase(),
          text: (active.innerText || active.getAttribute('value') || '').replace(/\s+/g, ' ').trim().slice(0, 80),
          ariaLabel: active.getAttribute('aria-label'),
          visible: rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden',
          indicator:
            (style.outlineStyle !== 'none' && Number.parseFloat(style.outlineWidth) > 0) ||
            style.boxShadow !== 'none',
        };
      });
    } catch (error) {
      const message = String(error);
      if (!message.includes('Execution context was destroyed') && !message.includes('navigation')) throw error;
      await page.waitForLoadState('domcontentloaded').catch(() => undefined);
      previous = '';
      repeated = 0;
      continue;
    }
    if (!state) continue;
    repeated = state.identity === previous ? repeated + 1 : 0;
    previous = state.identity;
    if (!state.visible) continue;
    focusStops.push({ tag: state.tag, text: state.text, ariaLabel: state.ariaLabel });
    if (!state.indicator) missingIndicator.push({ tag: state.tag, text: state.text });
    if (repeated >= 2) break;
  }
  return { focusStops, missingIndicator, stuck: repeated >= 2 };
}

export async function auditPage(
  page: Page,
  testInfo: TestInfo,
  route: RouteCase,
  diagnostics: DiagnosticEvent[],
  screenshot: string,
): Promise<Finding[]> {
  const findings: Finding[] = [];
  const ctx = context(testInfo, route, page, screenshot);
  const add = (input: Parameters<typeof createFinding>[0]) => {
    findings.push(createFinding(input, ctx.project, ctx.viewport));
  };
  const steps = [`Open ${route.path} with read-only browser guard`, `Wait for DOM content`, `Inspect ${ctx.project}`];

  const signals = await collectLayoutSignals(page);
  if (signals.viewportOverflow) {
    add({
      systemicId: 'LAYOUT_VIEWPORT_HORIZONTAL_OVERFLOW', severity: 'P1', category: 'responsive',
      title: 'Page overflows the viewport horizontally', url: ctx.url, route: ctx.route, state: ctx.state,
      steps, expected: 'The page fits the viewport; only intentional table wrappers may scroll.',
      actual: `Document width is ${signals.viewportOverflow.documentWidth}px at a ${signals.viewportOverflow.viewportWidth}px viewport.`,
      evidence: { screenshot, samples: [signals.viewportOverflow] },
    });
  }
  if (signals.overflowingText.length) {
    add({
      systemicId: 'LAYOUT_TEXT_OVERFLOW', severity: 'P1', category: 'responsive',
      title: 'Visible text overflows its control or cell', url: ctx.url, route: ctx.route, state: ctx.state,
      steps, expected: 'Text wraps, truncates explicitly, or is contained in an intentional scroll region.',
      actual: `${signals.overflowingText.length} sampled elements have content wider than their box.`,
      evidence: { screenshot, samples: signals.overflowingText },
    });
  }
  if (signals.tableCollisions.length) {
    add({
      systemicId: 'TABLE_CELL_COLLISION', severity: 'P0', category: 'responsive',
      title: 'Adjacent table cells overlap', url: ctx.url, route: ctx.route, state: ctx.state,
      steps, expected: 'Adjacent columns remain spatially separated at every supported viewport.',
      actual: `${signals.tableCollisions.length} sampled adjacent-cell collisions were found.`,
      evidence: { screenshot, samples: signals.tableCollisions },
    });
  }
  if (signals.tinyControls.length) {
    add({
      systemicId: 'CONTROL_TARGET_TOO_SHORT', severity: 'P1', category: 'accessibility',
      title: 'Interactive targets are too small', url: ctx.url, route: ctx.route, state: ctx.state,
      steps,
      expected: signals.controlMinimumSize === 44
        ? 'Mobile controls and links remain at least 44×44 CSS pixels.'
        : 'Controls at the supported desktop widths remain at least 32px high.',
      actual: `${signals.tinyControls.length} sampled controls are below the ${signals.controlMinimumSize}px target contract.`,
      evidence: { screenshot, samples: signals.tinyControls },
    });
  }
  if (signals.tinyText.length) {
    add({
      systemicId: 'TYPOGRAPHY_TEXT_BELOW_12PX', severity: 'P1', category: 'accessibility',
      title: 'Interface text is below 12px', url: ctx.url, route: ctx.route, state: ctx.state,
      steps, expected: 'Operational labels and values remain legible without browser zoom.',
      actual: `${signals.tinyText.length} sampled labels or values render below 12px.`,
      evidence: { screenshot, samples: signals.tinyText },
    });
  }
  if (signals.semanticClickTargets.length) {
    add({
      systemicId: 'INTERACTION_NON_SEMANTIC_CLICK_TARGET', severity: 'P1', category: 'accessibility',
      title: 'Clickable elements have no button or link semantics', url: ctx.url, route: ctx.route, state: ctx.state,
      steps, expected: 'Every clickable affordance is a native link/button or exposes correct role and keyboard behavior.',
      actual: `${signals.semanticClickTargets.length} sampled onclick elements have no interactive semantics.`,
      evidence: { screenshot, samples: signals.semanticClickTargets },
    });
  }
  if (signals.duplicateIds.length) {
    add({
      systemicId: 'DOM_DUPLICATE_IDS', severity: 'P1', category: 'accessibility',
      title: 'DOM contains duplicate IDs', url: ctx.url, route: ctx.route, state: ctx.state,
      steps, expected: 'Every DOM id is unique.', actual: `Duplicate IDs: ${signals.duplicateIds.join(', ')}`,
      evidence: { screenshot, samples: signals.duplicateIds },
    });
  }

  const axe = await new AxeBuilder({ page }).analyze();
  for (const violation of axe.violations) {
    add({
      systemicId: `AXE_${violation.id.toUpperCase().replaceAll('-', '_')}`,
      severity: severityForAxeImpact(violation.impact), category: 'accessibility',
      title: violation.help, url: ctx.url, route: ctx.route, state: ctx.state,
      steps, expected: `${violation.help}.`,
      actual: `${violation.nodes.length} affected node(s): ${violation.description}`,
      evidence: {
        screenshot,
        samples: violation.nodes.slice(0, 10).map((node) => ({ target: node.target, failureSummary: node.failureSummary })),
      },
    });
  }

  const keyboard = await runKeyboardAudit(page);
  if (!keyboard.focusStops.length || keyboard.stuck) {
    add({
      systemicId: 'KEYBOARD_NAVIGATION_BLOCKED', severity: 'P0', category: 'accessibility',
      title: 'Keyboard navigation cannot progress through the page', url: ctx.url, route: ctx.route, state: ctx.state,
      steps: [...steps, 'Press Tab repeatedly'], expected: 'Focus progresses through visible interactive controls without traps.',
      actual: keyboard.stuck ? 'Focus remained on the same element for three Tab presses.' : 'No visible focus stops were found.',
      evidence: { screenshot, samples: keyboard.focusStops },
    });
  } else if (keyboard.missingIndicator.length) {
    add({
      systemicId: 'FOCUS_INDICATOR_MISSING', severity: 'P1', category: 'accessibility',
      title: 'Keyboard focus is not visibly indicated', url: ctx.url, route: ctx.route, state: ctx.state,
      steps: [...steps, 'Press Tab through the first 30 focus stops'],
      expected: 'Every keyboard-focusable control has a visible focus indicator.',
      actual: `${keyboard.missingIndicator.length} sampled focus stops have no detectable outline or shadow.`,
      evidence: { screenshot, samples: keyboard.missingIndicator },
    });
  }

  const runtimeErrors = diagnostics.filter(
    (event) => event.type === 'pageerror' || (event.type === 'console' && event.level === 'error'),
  );
  if (runtimeErrors.length) {
    add({
      systemicId: 'BROWSER_RUNTIME_ERROR', severity: 'P1', category: 'runtime',
      title: 'Page emits browser runtime errors', url: ctx.url, route: ctx.route, state: ctx.state,
      steps, expected: 'The page loads and remains free of uncaught exceptions and console errors.',
      actual: `${runtimeErrors.length} runtime error(s) were collected.`,
      evidence: { screenshot, diagnostics: 'attachment:browser-diagnostics', samples: runtimeErrors.slice(0, 20) },
    });
  }
  const failedResponses = diagnostics.filter(
    (event) => event.type === 'response' && (event.status ?? 0) >= 400,
  );
  if (failedResponses.length) {
    add({
      systemicId: 'NETWORK_NON_2XX_RESPONSE', severity: 'P1', category: 'runtime',
      title: 'Page receives failed HTTP responses', url: ctx.url, route: ctx.route, state: ctx.state,
      steps, expected: 'Required page resources and read-only data requests return successful responses.',
      actual: `${failedResponses.length} HTTP response(s) returned status 400 or higher.`,
      evidence: { screenshot, diagnostics: 'attachment:browser-diagnostics', samples: failedResponses.slice(0, 20) },
    });
  }
  const failedRequests = diagnostics.filter((event) => event.type === 'requestfailed');
  if (failedRequests.length) {
    add({
      systemicId: 'NETWORK_REQUEST_FAILED', severity: 'P1', category: 'runtime',
      title: 'Browser requests fail before receiving an HTTP response', url: ctx.url, route: ctx.route, state: ctx.state,
      steps, expected: 'Required page resources and read-only data requests complete without transport failures.',
      actual: `${failedRequests.length} browser request(s) failed before a response was received.`,
      evidence: { screenshot, diagnostics: 'attachment:browser-diagnostics', samples: failedRequests.slice(0, 20) },
    });
  }
  const blockedRequests = diagnostics.filter((event) => event.type === 'blocked-request');
  if (blockedRequests.length) {
    add({
      systemicId: 'UNSAFE_OR_EXTERNAL_BROWSER_REQUEST', severity: 'P1', category: 'runtime',
      title: 'Page attempted an unsafe or external browser request during read-only audit',
      url: ctx.url, route: ctx.route, state: ctx.state, steps,
      expected: 'Read-only page inspection does not mutate state or contact external browser origins.',
      actual: `${blockedRequests.length} request(s) were blocked by the QA safety guard.`,
      evidence: { screenshot, diagnostics: 'attachment:browser-diagnostics', samples: blockedRequests.slice(0, 20) },
    });
  }

  for (const finding of findings) await attachFinding(testInfo, finding);
  return findings;
}
