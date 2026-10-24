export type Severity = 'P0' | 'P1' | 'P2' | 'P3';

export type RouteKind = 'list' | 'detail' | 'dashboard';
export type RouteSurface = 'canonical' | 'operator' | 'detail' | 'compatibility';

export interface RouteCase {
  id: string;
  title: string;
  path: string;
  kind: RouteKind;
  tags: string[];
  state?: string;
  surface?: RouteSurface;
  expectedPath?: string;
}

export interface RouteManifest {
  version: 1;
  state?: string;
  generatedAt?: string;
  routes: RouteCase[];
}

export interface Evidence {
  screenshot?: string;
  selector?: string;
  status?: number;
  samples?: unknown[];
  diagnostics?: string;
  trace?: string;
  video?: string;
}

export interface Finding {
  id: string;
  systemicId: string;
  severity: Severity;
  category:
    | 'availability'
    | 'accessibility'
    | 'responsive'
    | 'interaction'
    | 'data-contract'
    | 'runtime'
    | 'navigation';
  title: string;
  url: string;
  route: string;
  state: string;
  viewport: { name: string; width: number; height: number };
  steps: string[];
  expected: string;
  actual: string;
  evidence: Evidence;
  observedAt: string;
}

export interface DiagnosticEvent {
  type: 'console' | 'pageerror' | 'requestfailed' | 'response' | 'blocked-request';
  timestamp: string;
  url?: string;
  method?: string;
  status?: number;
  level?: string;
  text?: string;
  failure?: string;
}
