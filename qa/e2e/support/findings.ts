import type { Finding, Severity } from './types';

type FindingInput = Omit<Finding, 'id' | 'viewport' | 'observedAt'> & {
  viewport?: Finding['viewport'];
};

function slug(value: string): string {
  return value
    .toLowerCase()
    .replace(/https?:\/\//g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 100);
}

export function createFinding(
  input: FindingInput,
  projectName: string,
  viewport: { width: number; height: number },
): Finding {
  const observedAt = new Date().toISOString();
  return {
    ...input,
    id: `${input.severity}-${slug(input.systemicId)}-${slug(input.route)}-${projectName}-${slug(input.url).slice(-72)}`,
    viewport: input.viewport ?? { name: projectName, ...viewport },
    observedAt,
  };
}

export function severityForAxeImpact(impact: string | null | undefined): Severity {
  if (impact === 'critical' || impact === 'serious') return 'P1';
  if (impact === 'moderate') return 'P2';
  return 'P3';
}
