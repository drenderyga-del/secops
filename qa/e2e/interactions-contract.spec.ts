import { expect, test, type TestInfo } from '@playwright/test';
import { recordBadNavigation } from './support/navigation';
import { routeInventory } from './support/routes';

interface CapturedAttachment {
  name: string;
  body: Buffer;
}

function fakeTestInfo(attachments: CapturedAttachment[]): TestInfo {
  return {
    project: { name: 'desktop-1440' },
    attach: async (name: string, options: { body?: string | Buffer }) => {
      attachments.push({
        name,
        body: Buffer.isBuffer(options.body) ? options.body : Buffer.from(options.body ?? ''),
      });
    },
  } as unknown as TestInfo;
}

test.describe('history navigation diagnostics', () => {
  test.beforeEach(async ({}, testInfo) => {
    test.skip(testInfo.project.name !== 'desktop-1440', 'Contract fixtures run once');
  });

  test('a Back response with HTTP 500 becomes a P0 finding', async () => {
    const attachments: CapturedAttachment[] = [];
    const route = routeInventory[0];
    await recordBadNavigation(
      { status: () => 500, url: () => 'http://localhost:18081/back-failed' },
      fakeTestInfo(attachments),
      { width: 1440, height: 1000 },
      route,
      ['Use browser Back'],
      { allowNullHistoryResponse: true },
    );

    const findingAttachment = attachments.find((item) => item.name === 'qa-finding');
    expect(findingAttachment).toBeDefined();
    const finding = JSON.parse(findingAttachment?.body.toString('utf8') ?? '{}');
    expect(finding.systemicId).toBe('INTERACTION_HTTP_5XX');
    expect(finding.severity).toBe('P0');
    expect(finding.evidence.status).toBe(500);
  });

  test('a null Back response is explicitly diagnosed as a safe history restore', async () => {
    const attachments: CapturedAttachment[] = [];
    await recordBadNavigation(
      null,
      fakeTestInfo(attachments),
      { width: 1440, height: 1000 },
      routeInventory[0],
      ['Use browser Back'],
      { allowNullHistoryResponse: true },
    );

    expect(attachments.some((item) => item.name === 'qa-finding')).toBe(false);
    const diagnostic = attachments.find((item) => item.name === 'qa-navigation-diagnostic');
    expect(diagnostic).toBeDefined();
    expect(JSON.parse(diagnostic?.body.toString('utf8') ?? '{}')).toMatchObject({
      kind: 'history-navigation-without-network-response',
      interpretation: 'bfcache or same-document history restore',
    });
  });
});
