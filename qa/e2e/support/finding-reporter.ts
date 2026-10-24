import fs from 'node:fs';
import path from 'node:path';
import type {
  FullConfig,
  FullResult,
  Reporter,
  Suite,
  TestCase,
  TestResult,
} from '@playwright/test/reporter';
import { routeInventoryMetadata } from './routes';
import type { Finding, Severity } from './types';

interface ReporterOptions {
  outputDir?: string;
  /** Playwright supplies this command mode to every reporter constructor. */
  _mode?: 'list' | 'test' | 'merge';
}

const severityOrder: Record<Severity, number> = { P0: 0, P1: 1, P2: 2, P3: 3 };

export default class FindingReporter implements Reporter {
  private readonly outputDir: string;
  private readonly listOnly: boolean;
  private findings: Finding[] = [];
  private testCounts = { passed: 0, failed: 0, skipped: 0, timedOut: 0, interrupted: 0 };
  private startedAt = new Date().toISOString();
  private totalTests = 0;

  constructor(options: ReporterOptions = {}) {
    this.outputDir = path.resolve(options.outputDir ?? process.env.QA_ARTIFACTS_DIR ?? 'qa/artifacts');
    this.listOnly = options._mode === 'list';
  }

  onBegin(_config: FullConfig, suite: Suite): void {
    if (this.listOnly) return;
    fs.mkdirSync(this.outputDir, { recursive: true });
    this.totalTests = suite.allTests().length;
  }

  onTestEnd(_test: TestCase, result: TestResult): void {
    if (this.listOnly) return;
    if (result.status === 'passed') this.testCounts.passed += 1;
    else if (result.status === 'skipped') this.testCounts.skipped += 1;
    else if (result.status === 'timedOut') this.testCounts.timedOut += 1;
    else if (result.status === 'interrupted') this.testCounts.interrupted += 1;
    else this.testCounts.failed += 1;

    const trace = result.attachments.find((attachment) => attachment.name === 'trace')?.path;
    const video = result.attachments.find((attachment) => attachment.name === 'video')?.path;
    for (const attachment of result.attachments.filter((item) => item.name === 'qa-finding')) {
      try {
        const body = attachment.body ?? (attachment.path ? fs.readFileSync(attachment.path) : undefined);
        if (!body) continue;
        const finding = JSON.parse(body.toString('utf8')) as Finding;
        if (trace) finding.evidence.trace = trace;
        if (video) finding.evidence.video = video;
        this.findings.push(finding);
      } catch (error) {
        process.stderr.write(`Could not parse qa-finding attachment: ${String(error)}\n`);
      }
    }
  }

  async onEnd(result: FullResult): Promise<{ status: FullResult['status'] }> {
    // `playwright test --list` calls reporter lifecycle hooks without executing
    // tests. It is inventory, not release evidence, so it must not publish or
    // announce a gate verdict.
    if (this.listOnly) return { status: result.status };

    const finishedAt = new Date().toISOString();
    this.findings.sort((a, b) =>
      severityOrder[a.severity] - severityOrder[b.severity]
      || a.systemicId.localeCompare(b.systemicId)
      || a.route.localeCompare(b.route)
      || a.viewport.width - b.viewport.width,
    );

    const severityCounts = this.findings.reduce<Record<Severity, number>>(
      (counts, finding) => ({ ...counts, [finding.severity]: counts[finding.severity] + 1 }),
      { P0: 0, P1: 0, P2: 0, P3: 0 },
    );
    const severityGatePassed = severityCounts.P0 === 0 && severityCounts.P1 === 0;
    const releaseGatePassed = severityGatePassed && result.status === 'passed';
    const finalStatus: FullResult['status'] = releaseGatePassed ? 'passed' : 'failed';
    const systemic = this.findings.reduce<Record<string, { count: number; severity: Severity; routes: Set<string>; viewports: Set<string> }>>(
      (groups, finding) => {
        const current = groups[finding.systemicId] ?? {
          count: 0,
          severity: finding.severity,
          routes: new Set<string>(),
          viewports: new Set<string>(),
        };
        current.count += 1;
        if (severityOrder[finding.severity] < severityOrder[current.severity]) current.severity = finding.severity;
        current.routes.add(finding.route);
        current.viewports.add(finding.viewport.name);
        groups[finding.systemicId] = current;
        return groups;
      },
      {},
    );

    const report = {
      schemaVersion: 1,
      run: {
        startedAt: this.startedAt,
        finishedAt,
        playwrightStatus: result.status,
        status: finalStatus,
        baseURL: process.env.QA_BASE_URL ?? 'http://localhost:18081',
        manifest: routeInventoryMetadata,
        totalTests: this.totalTests,
        testCounts: this.testCounts,
      },
      summary: {
        totalFindings: this.findings.length,
        severityCounts,
        systemicIssues: Object.entries(systemic).map(([systemicId, item]) => ({
          systemicId,
          severity: item.severity,
          occurrences: item.count,
          routes: [...item.routes].sort(),
          viewports: [...item.viewports].sort(),
        })).sort((a, b) => severityOrder[a.severity] - severityOrder[b.severity] || b.occurrences - a.occurrences),
      },
      gate: {
        policy: 'Playwright passed and zero P0/P1 findings',
        passed: releaseGatePassed,
      },
      findings: this.findings,
    };

    fs.writeFileSync(path.join(this.outputDir, 'findings.json'), `${JSON.stringify(report, null, 2)}\n`);
    fs.writeFileSync(path.join(this.outputDir, 'report.md'), this.renderMarkdown(report));
    process.stdout.write(
      `\nScanOps QA report: ${path.join(this.outputDir, 'report.md')} `
      + `(gate ${releaseGatePassed ? 'PASSED' : 'FAILED'}; P0 ${severityCounts.P0}, P1 ${severityCounts.P1}, P2 ${severityCounts.P2}, P3 ${severityCounts.P3})\n`,
    );
    return { status: finalStatus };
  }

  private renderMarkdown(report: {
    run: {
      startedAt: string;
      finishedAt: string;
      status: string;
      playwrightStatus: string;
      baseURL: string;
      totalTests: number;
      testCounts: {
        passed: number;
        failed: number;
        skipped: number;
        timedOut: number;
        interrupted: number;
      };
    };
    gate: {
      policy: string;
      passed: boolean;
    };
    summary: {
      totalFindings: number;
      severityCounts: Record<Severity, number>;
      systemicIssues: Array<{ systemicId: string; severity: Severity; occurrences: number; routes: string[]; viewports: string[] }>;
    };
    findings: Finding[];
  }): string {
    const lines = [
      '# ScanOps browser QA report',
      '',
      `- Run: ${report.run.startedAt} — ${report.run.finishedAt}`,
      `- Base URL: \`${report.run.baseURL}\``,
      `- Release gate: **${report.gate.passed ? 'PASSED' : 'FAILED'}** (${report.gate.policy})`,
      `- Playwright status: **${report.run.playwrightStatus}**`,
      `- Final status: **${report.run.status}**`,
      `- Tests: ${report.run.testCounts.passed} passed, ${report.run.testCounts.failed} failed, ${report.run.testCounts.timedOut} timed out, ${report.run.testCounts.skipped} skipped`,
      `- Findings: **${report.summary.totalFindings}** — P0 ${report.summary.severityCounts.P0}, P1 ${report.summary.severityCounts.P1}, P2 ${report.summary.severityCounts.P2}, P3 ${report.summary.severityCounts.P3}`,
      '',
      '## Systemic issues',
      '',
      '| Severity | Systemic ID | Occurrences | Routes | Viewports |',
      '|---|---|---:|---|---|',
      ...report.summary.systemicIssues.map((item) =>
        `| ${item.severity} | \`${item.systemicId}\` | ${item.occurrences} | ${item.routes.join(', ')} | ${item.viewports.join(', ')} |`,
      ),
      '',
      '## P0 / P1 findings',
      '',
    ];

    const important = report.findings.filter((finding) => finding.severity === 'P0' || finding.severity === 'P1');
    if (!important.length) lines.push('No P0/P1 findings were collected.', '');
    for (const finding of important) {
      lines.push(
        `### ${finding.severity} — ${finding.title}`,
        '',
        `- ID: \`${finding.id}\``,
        `- Systemic ID: \`${finding.systemicId}\``,
        `- URL: \`${finding.url}\``,
        `- State / viewport: \`${finding.state}\` / \`${finding.viewport.name} (${finding.viewport.width}×${finding.viewport.height})\``,
        `- Expected: ${finding.expected}`,
        `- Actual: ${finding.actual}`,
        '- Steps:',
        ...finding.steps.map((step, index) => `  ${index + 1}. ${step}`),
      );
      if (finding.evidence.screenshot) lines.push(`- Screenshot: \`${finding.evidence.screenshot}\``);
      if (finding.evidence.selector) lines.push(`- Selector: \`${finding.evidence.selector}\``);
      if (finding.evidence.trace) lines.push(`- Trace: \`${finding.evidence.trace}\``);
      if (finding.evidence.video) lines.push(`- Video: \`${finding.evidence.video}\``);
      lines.push('');
    }
    lines.push(
      '## Evidence',
      '',
      '- Full structured findings: `findings.json`',
      '- Per-test screenshots and diagnostics: `test-results/`',
      '- Interactive Playwright report: `html/index.html`',
      '- Traces and video are retained for failed tests.',
      '',
    );
    return `${lines.join('\n')}\n`;
  }
}
