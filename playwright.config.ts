import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.QA_BASE_URL ?? 'http://localhost:18081';
const artifactRoot = process.env.QA_ARTIFACTS_DIR ?? 'qa/artifacts';

const viewports = [
  { name: 'desktop-1920', width: 1920, height: 1080 },
  { name: 'desktop-1440', width: 1440, height: 1000 },
];

export default defineConfig({
  testDir: './qa/e2e',
  outputDir: `${artifactRoot}/test-results`,
  fullyParallel: true,
  timeout: 60_000,
  expect: { timeout: 7_500 },
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: process.env.QA_WORKERS ? Number(process.env.QA_WORKERS) : undefined,
  reporter: [
    ['line'],
    ['./qa/e2e/support/finding-reporter.ts', { outputDir: artifactRoot }],
    ['html', { outputFolder: `${artifactRoot}/html`, open: 'never' }],
  ],
  use: {
    ...devices['Desktop Chrome'],
    baseURL,
    httpCredentials: {
      username: process.env.QA_USERNAME ?? 'demo',
      password: process.env.QA_PASSWORD ?? 'demo',
    },
    actionTimeout: 10_000,
    navigationTimeout: 20_000,
    colorScheme: 'dark',
    locale: 'en-US',
    timezoneId: 'Europe/Moscow',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
  },
  projects: viewports.map(({ name, width, height }) => ({
    name,
    use: { viewport: { width, height } },
  })),
});
