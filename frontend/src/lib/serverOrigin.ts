const DEFAULT_FRONTEND_PORT = '20815';
const SAFE_METHODS = new Set(['GET', 'HEAD']);

function normalizeOrigin(value: string): string | null {
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol)) return null;
    if (url.username || url.password) return null;
    if (url.pathname !== '/' || url.search || url.hash) return null;
    return url.origin;
  } catch {
    return null;
  }
}

export function configuredFrontendOrigins(): ReadonlySet<string> {
  const configured = process.env.AGENT_BUILDER_FRONTEND_ORIGINS?.trim();
  const portValue = process.env.FRONTEND_PORT?.trim() || DEFAULT_FRONTEND_PORT;
  const port = /^\d{1,5}$/.test(portValue) ? portValue : DEFAULT_FRONTEND_PORT;
  const values = configured
    ? configured.split(',')
    : [`http://localhost:${port}`, `http://127.0.0.1:${port}`];

  return new Set(
    values
      .map((value) => normalizeOrigin(value.trim()))
      .filter((value): value is string => value !== null),
  );
}

function normalizedRequestHost(hostHeader: string | null): string | null {
  if (!hostHeader || hostHeader.includes(',')) return null;
  try {
    const url = new URL(`http://${hostHeader.trim()}`);
    if (url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
      return null;
    }
    return url.host.toLowerCase();
  } catch {
    return null;
  }
}

/**
 * Validate browser provenance before a server-side proxy attaches its private
 * backend token. Requests from an explicitly configured frontend origin are
 * accepted. Unsafe requests must carry a matching Origin; Fetch Metadata is
 * only an additional consistency check and cannot authorize a request alone.
 */
export function isTrustedFrontendRequest(request: Pick<Request, 'headers' | 'method'>): boolean {
  const allowedOrigins = configuredFrontendOrigins();
  const requestHost = normalizedRequestHost(request.headers.get('host'));
  const allowedHosts = new Set(
    [...allowedOrigins].map((origin) => new URL(origin).host.toLowerCase()),
  );
  if (!requestHost || !allowedHosts.has(requestHost)) return false;

  const originHeader = request.headers.get('origin');
  const fetchSite = request.headers.get('sec-fetch-site')?.toLowerCase();

  if (fetchSite && fetchSite !== 'same-origin') {
    return false;
  }

  if (originHeader) {
    const origin = normalizeOrigin(originHeader);
    return (
      origin !== null
      && allowedOrigins.has(origin)
      && new URL(origin).host.toLowerCase() === requestHost
    );
  }

  return SAFE_METHODS.has(request.method.toUpperCase());
}

export function rejectUntrustedFrontendRequest(
  request: Pick<Request, 'headers' | 'method'>,
): Response | null {
  if (isTrustedFrontendRequest(request)) return null;
  return Response.json({ detail: 'Forbidden request origin' }, { status: 403 });
}
