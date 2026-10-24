import fs from 'node:fs';
import path from 'node:path';
import type { RouteCase, RouteManifest } from './types';

const canonicalRoutes: RouteCase[] = [
  { id: 'canonical-overview', title: 'Overview', path: '/', kind: 'dashboard', tags: ['smoke', 'overview', 'signal-control'], surface: 'canonical' },
  { id: 'canonical-executions', title: 'Execution lifecycle', path: '/executions', kind: 'list', tags: ['smoke', 'executions', 'signal-control'], surface: 'canonical' },
  { id: 'canonical-job-journal', title: 'Job journal', path: '/logs', kind: 'list', tags: ['smoke', 'executions', 'job-journal', 'signal-control'], surface: 'canonical' },
  { id: 'canonical-pipelines', title: 'Active pipelines', path: '/pipelines', kind: 'list', tags: ['smoke', 'pipelines', 'signal-control'], surface: 'canonical' },
  { id: 'canonical-config-guard', title: 'Config Guard', path: '/config-scan', kind: 'dashboard', tags: ['smoke', 'config-guard', 'signal-control'], surface: 'canonical' },
  { id: 'canonical-iac', title: 'IaC scanning', path: '/iac', kind: 'dashboard', tags: ['smoke', 'iac', 'signal-control'], surface: 'canonical' },
  { id: 'canonical-ptai', title: 'PT AI', path: '/ptai-scans', kind: 'dashboard', tags: ['smoke', 'ptai', 'signal-control'], surface: 'canonical' },
  { id: 'canonical-failed-jobs', title: 'Failed jobs', path: '/logs?status=failed', kind: 'list', tags: ['executions', 'job-journal', 'signal-control'], surface: 'canonical' },
  { id: 'canonical-coverage', title: 'Coverage', path: '/coverage', kind: 'dashboard', tags: ['smoke', 'coverage', 'signal-control'], surface: 'canonical' },
  { id: 'canonical-products', title: 'Products', path: '/targets?level=product', kind: 'list', tags: ['smoke', 'targets', 'signal-control'], surface: 'canonical' },
  { id: 'canonical-repositories', title: 'Repositories', path: '/targets?level=repository', kind: 'list', tags: ['targets', 'signal-control'], surface: 'canonical' },
  { id: 'canonical-scanners', title: 'Scanner fleet', path: '/scanners', kind: 'dashboard', tags: ['smoke', 'infrastructure', 'signal-control'], surface: 'canonical' },
  { id: 'canonical-integrations', title: 'Integrations', path: '/integrations', kind: 'dashboard', tags: ['smoke', 'infrastructure', 'signal-control'], surface: 'canonical' },
  { id: 'canonical-integration-asoc', title: 'ASOC integration settings', path: '/integrations?integration=asoc', kind: 'detail', tags: ['signal-control', 'integration-settings'], surface: 'detail' },
  { id: 'canonical-integration-gitlab', title: 'GitLab integration settings', path: '/integrations?integration=gitlab', kind: 'detail', tags: ['signal-control', 'integration-settings'], surface: 'detail' },
  { id: 'canonical-integration-ptai', title: 'PT AI integration settings', path: '/integrations?integration=ptai', kind: 'detail', tags: ['signal-control', 'integration-settings'], surface: 'detail' },
  { id: 'canonical-integration-mattermost', title: 'Mattermost integration settings', path: '/integrations?integration=mattermost', kind: 'detail', tags: ['signal-control', 'integration-settings'], surface: 'detail' },
];

const operatorRoutes: RouteCase[] = [
  { id: 'operator-duration', title: 'Execution duration', path: '/dashboard/duration', kind: 'dashboard', tags: ['operator', 'overview'], surface: 'operator' },
  { id: 'operator-config-guard', title: 'Config Guard operator workspace', path: '/config-scan/operator', kind: 'dashboard', tags: ['operator', 'config-guard'], surface: 'operator' },
  { id: 'operator-iac', title: 'IaC operator workspace', path: '/iac/operator', kind: 'dashboard', tags: ['operator', 'iac'], surface: 'operator' },
  { id: 'operator-iac-runs', title: 'IaC runs', path: '/iac/runs', kind: 'list', tags: ['operator', 'iac'], surface: 'operator' },
  { id: 'operator-iac-repositories', title: 'IaC repositories', path: '/iac/repositories', kind: 'list', tags: ['operator', 'iac'], surface: 'operator' },
  { id: 'operator-iac-findings', title: 'IaC findings', path: '/iac/findings', kind: 'list', tags: ['operator', 'iac'], surface: 'operator' },
  { id: 'operator-coverage', title: 'Coverage operator workspace', path: '/coverage/operator', kind: 'dashboard', tags: ['operator', 'coverage'], surface: 'operator' },
  { id: 'operator-coverage-runs', title: 'Coverage run history', path: '/coverage/runs', kind: 'list', tags: ['operator', 'coverage'], surface: 'operator' },
  { id: 'operator-scanners', title: 'Scanner adapter runtime workspace', path: '/scanners/operator', kind: 'dashboard', tags: ['operator', 'infrastructure'], surface: 'operator' },
  { id: 'operator-target-products', title: 'Product target operator workspace', path: '/targets/operator?level=product', kind: 'list', tags: ['operator', 'targets'], surface: 'operator' },
  { id: 'operator-target-repositories', title: 'Repository target operator workspace', path: '/targets/operator?level=repository', kind: 'list', tags: ['operator', 'targets'], surface: 'operator' },
];

export const compatibilityRoutes: RouteCase[] = [
  { id: 'compat-operator-overview', title: 'Operator overview alias', path: '/overview/operator', expectedPath: '/', kind: 'dashboard', tags: ['compatibility', 'navigation', 'overview'], surface: 'compatibility' },
  { id: 'compat-classic-overview', title: 'Classic overview alias', path: '/classic', expectedPath: '/', kind: 'dashboard', tags: ['compatibility', 'navigation'], surface: 'compatibility' },
  { id: 'compat-classic-pipelines', title: 'Classic pipelines alias', path: '/classic/pipelines', expectedPath: '/pipelines', kind: 'list', tags: ['compatibility', 'navigation'], surface: 'compatibility' },
  { id: 'compat-classic-products', title: 'Classic product targets alias', path: '/classic/targets?level=product', expectedPath: '/targets/operator?level=product', kind: 'list', tags: ['compatibility', 'navigation'], surface: 'compatibility' },
  { id: 'compat-classic-repositories', title: 'Classic repository targets alias', path: '/classic/targets?level=repository', expectedPath: '/targets/operator?level=repository', kind: 'list', tags: ['compatibility', 'navigation'], surface: 'compatibility' },
  { id: 'compat-classic-coverage', title: 'Classic coverage alias', path: '/classic/coverage', expectedPath: '/coverage/operator', kind: 'dashboard', tags: ['compatibility', 'navigation'], surface: 'compatibility' },
  { id: 'compat-classic-config-guard', title: 'Classic Config Guard alias', path: '/classic/config-scan', expectedPath: '/config-scan/operator', kind: 'dashboard', tags: ['compatibility', 'navigation'], surface: 'compatibility' },
  { id: 'compat-classic-iac', title: 'Classic IaC alias', path: '/classic/iac', expectedPath: '/iac/operator', kind: 'dashboard', tags: ['compatibility', 'navigation'], surface: 'compatibility' },
  { id: 'compat-classic-ptai', title: 'Classic PT AI alias', path: '/classic/ptai-scans', expectedPath: '/ptai-scans', kind: 'dashboard', tags: ['compatibility', 'navigation'], surface: 'compatibility' },
  { id: 'compat-classic-scanners', title: 'Classic scanner alias', path: '/classic/scanners', expectedPath: '/scanners/operator', kind: 'dashboard', tags: ['compatibility', 'navigation'], surface: 'compatibility' },
  { id: 'compat-classic-integrations', title: 'Classic integration alias', path: '/classic/integrations?focus=gitlab', expectedPath: '/integrations?integration=gitlab', kind: 'dashboard', tags: ['compatibility', 'navigation'], surface: 'compatibility' },
  { id: 'compat-classic-reports', title: 'Classic reports alias', path: '/classic/reports?year=2026&quarter=3', expectedPath: '/targets?level=product&period=2026-Q3&mode=new', kind: 'list', tags: ['compatibility', 'navigation', 'reporting'], surface: 'compatibility' },
  { id: 'compat-reports', title: 'Retired ASOC report', path: '/reports', expectedPath: '/targets?level=product', kind: 'list', tags: ['compatibility', 'navigation', 'reporting'], surface: 'compatibility' },
  { id: 'compat-reports-operator', title: 'Retired ASOC report workspace', path: '/reports/operator?year=2026&quarter=2', expectedPath: '/targets?level=product&period=2026-Q2&mode=new', kind: 'list', tags: ['compatibility', 'navigation', 'reporting'], surface: 'compatibility' },
  { id: 'compat-failed-jobs', title: 'Failed jobs alias', path: '/failed', expectedPath: '/logs?status=failed', kind: 'list', tags: ['compatibility', 'navigation'], surface: 'compatibility' },
  { id: 'compat-products', title: 'Products alias', path: '/products', expectedPath: '/targets?level=product', kind: 'list', tags: ['compatibility', 'navigation'], surface: 'compatibility' },
  { id: 'compat-repositories', title: 'Repositories alias', path: '/repositories', expectedPath: '/targets?level=repository', kind: 'list', tags: ['compatibility', 'navigation'], surface: 'compatibility' },
];

const detailContracts = [
  '/targets/products/{product_id}',
  '/targets/products/asoc/{asoc_id}',
  '/targets/repositories/{repository_id}',
  '/repos/{repository_id}/jobs',
  '/coverage/products/{product_id}',
  '/coverage/runs/{run_id}',
  '/config-scan/products/{product_id}',
  '/config-scan/runs/{run_id}',
  '/config-scan/runs/{run_id}/findings',
  '/iac/runs/{run_id}',
  '/iac/repositories/{repo_row_id}',
  '/ptai-scans/{scan_id}',
] as const;

function validateRoute(route: unknown, index: number): asserts route is RouteCase {
  if (!route || typeof route !== 'object') {
    throw new Error(`Route manifest entry ${index} must be an object`);
  }
  const value = route as Record<string, unknown>;
  if (typeof value.id !== 'string' || !/^[a-z0-9][a-z0-9-]*$/.test(value.id)) {
    throw new Error(`Route manifest entry ${index} has an invalid id`);
  }
  if (typeof value.title !== 'string' || typeof value.path !== 'string' || !value.path.startsWith('/')) {
    throw new Error(`Route manifest entry ${index} must have title and an absolute app path`);
  }
  if (!['list', 'detail', 'dashboard'].includes(String(value.kind))) {
    throw new Error(`Route manifest entry ${index} has an invalid kind`);
  }
  if (!Array.isArray(value.tags) || value.tags.some((tag) => typeof tag !== 'string')) {
    throw new Error(`Route manifest entry ${index} must have string tags`);
  }
  if (
    value.surface !== undefined
    && !['canonical', 'operator', 'detail', 'compatibility'].includes(String(value.surface))
  ) {
    throw new Error(`Route manifest entry ${index} has an invalid surface`);
  }
}

function loadManifest(): { state: string; routes: RouteCase[]; source?: string } {
  const manifestFile = process.env.QA_ROUTE_MANIFEST;
  if (!manifestFile) return { state: process.env.QA_DATA_STATE ?? 'local', routes: [] };

  const absolute = path.resolve(manifestFile);
  const parsed = JSON.parse(fs.readFileSync(absolute, 'utf8')) as RouteManifest;
  if (parsed.version !== 1 || !Array.isArray(parsed.routes)) {
    throw new Error(`Unsupported QA route manifest: ${absolute}`);
  }
  parsed.routes.forEach(validateRoute);
  return {
    state: parsed.state ?? process.env.QA_DATA_STATE ?? 'local',
    routes: parsed.routes,
    source: absolute,
  };
}

const manifest = loadManifest();
const tags = new Set(
  (process.env.QA_ROUTE_TAGS ?? '')
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean),
);

const visualRoutes = [
  ...canonicalRoutes,
  ...operatorRoutes,
  ...manifest.routes.map((route) => ({ ...route, surface: route.surface ?? 'detail' as const })),
].map((route) => ({
  ...route,
  state: route.state ?? manifest.state,
}));

const routeCatalog = [
  ...visualRoutes,
  ...compatibilityRoutes.map((route) => ({ ...route, state: route.state ?? manifest.state })),
];
const duplicateIds = routeCatalog
  .map((route) => route.id)
  .filter((id, index, ids) => ids.indexOf(id) !== index);
if (duplicateIds.length) throw new Error(`Duplicate route ids: ${[...new Set(duplicateIds)].join(', ')}`);

const duplicateStates = visualRoutes
  .map((route) => `${route.path}::${route.state}`)
  .filter((key, index, keys) => keys.indexOf(key) !== index);
if (duplicateStates.length) {
  throw new Error(`Duplicate visual route states: ${[...new Set(duplicateStates)].join(', ')}`);
}

export const routeInventory = tags.size
  ? visualRoutes.filter((route) => route.tags.some((tag) => tags.has(tag)))
  : visualRoutes;

export const compatibilityInventory = tags.size
  ? compatibilityRoutes.filter((route) => route.tags.some((tag) => tags.has(tag)))
  : compatibilityRoutes;

export const routeInventoryMetadata = {
  manifestSource: manifest.source,
  manifestState: manifest.state,
  detailContracts,
  staticRouteCount: canonicalRoutes.length + operatorRoutes.length,
  canonicalRouteCount: canonicalRoutes.length,
  operatorRouteCount: operatorRoutes.length,
  compatibilityRouteCount: compatibilityRoutes.length,
  detailRouteCount: manifest.routes.filter((route) => route.kind === 'detail').length,
};
