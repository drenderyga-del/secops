import type { Page, TestInfo } from '@playwright/test';
import type { DiagnosticEvent } from './types';

function now(): string {
  return new Date().toISOString();
}

export async function installDiagnostics(page: Page, testInfo: TestInfo): Promise<DiagnosticEvent[]> {
  const events: DiagnosticEvent[] = [];
  const baseOrigin = new URL(String(testInfo.project.use.baseURL)).origin;

  await page.route('**/*', async (route) => {
    const request = route.request();
    let requestOrigin = '';
    try {
      requestOrigin = new URL(request.url()).origin;
    } catch {
      requestOrigin = 'invalid';
    }
    const safeMethod = request.method() === 'GET' || request.method() === 'HEAD';
    if (!safeMethod || requestOrigin !== baseOrigin) {
      events.push({
        type: 'blocked-request',
        timestamp: now(),
        url: request.url(),
        method: request.method(),
        text: !safeMethod ? 'Browser QA blocks non-read-only requests' : 'Browser QA blocks external origins',
      });
      await route.abort('blockedbyclient');
      return;
    }
    await route.continue();
  });

  page.on('console', (message) => {
    events.push({
      type: 'console',
      timestamp: now(),
      level: message.type(),
      text: message.text(),
    });
  });
  page.on('pageerror', (error) => {
    events.push({ type: 'pageerror', timestamp: now(), text: error.message });
  });
  page.on('requestfailed', (request) => {
    events.push({
      type: 'requestfailed',
      timestamp: now(),
      url: request.url(),
      method: request.method(),
      failure: request.failure()?.errorText,
    });
  });
  page.on('response', (response) => {
    if (response.status() < 200 || response.status() >= 300) {
      events.push({
        type: 'response',
        timestamp: now(),
        url: response.url(),
        method: response.request().method(),
        status: response.status(),
      });
    }
  });

  return events;
}

export async function attachDiagnostics(testInfo: TestInfo, events: DiagnosticEvent[]): Promise<string> {
  const diagnosticsPath = testInfo.outputPath('diagnostics.json');
  await testInfo.attach('browser-diagnostics', {
    body: Buffer.from(JSON.stringify(events, null, 2)),
    contentType: 'application/json',
  });
  return diagnosticsPath;
}
