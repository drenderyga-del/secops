import type { Response, TestInfo } from '@playwright/test';
import { createFinding } from './findings';
import { attachFinding } from './page-audit';
import type { RouteCase } from './types';

export interface HttpProbe {
  status(): number;
  url(): string;
}

export async function recordBadNavigation(
  response: Response | HttpProbe | null,
  testInfo: TestInfo,
  viewport: { width: number; height: number },
  route: RouteCase,
  steps: string[],
  options: { allowNullHistoryResponse?: boolean } = {},
): Promise<void> {
  if (response === null && options.allowNullHistoryResponse) {
    await testInfo.attach('qa-navigation-diagnostic', {
      body: Buffer.from(JSON.stringify({
        kind: 'history-navigation-without-network-response',
        interpretation: 'bfcache or same-document history restore',
        route: route.id,
        steps,
      })),
      contentType: 'application/json',
    });
    return;
  }
  const status = response?.status() ?? 599;
  if (status < 400) return;
  const finding = createFinding({
    systemicId: status >= 500 ? 'INTERACTION_HTTP_5XX' : 'INTERACTION_HTTP_4XX',
    severity: status >= 500 ? 'P0' : 'P1', category: 'interaction',
    title: 'A safe navigation or GET filter returns an HTTP error', url: response?.url() ?? route.path,
    route: route.id, state: route.state ?? 'local', steps,
    expected: 'The destination renders successfully after a read-only interaction.',
    actual: `The destination returned HTTP ${status}.`, evidence: { status },
  }, testInfo.project.name, viewport);
  await attachFinding(testInfo, finding);
}
