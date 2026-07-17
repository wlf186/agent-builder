import { NextRequest } from 'next/server';
import { rejectUntrustedFrontendRequest } from '@/lib/serverOrigin';

const BACKEND_URL = process.env.AGENT_BUILDER_BACKEND_URL || 'http://127.0.0.1:20881';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type RouteContext = { params: Promise<{ path: string[] }> };

function backendToken(): string | null {
  const token = process.env.AGENT_BUILDER_API_TOKEN?.trim();
  return token || null;
}

function buildBackendUrl(request: NextRequest, path: string[]): URL {
  const encodedPath = path.map(encodeURIComponent).join('/');
  const url = new URL(`/api/${encodedPath}`, BACKEND_URL);
  url.search = request.nextUrl.search;
  return url;
}

function requestHeaders(request: NextRequest, token: string): Headers {
  const headers = new Headers(request.headers);
  headers.delete('host');
  headers.delete('connection');
  headers.delete('content-length');
  headers.delete('accept-encoding');
  headers.set('authorization', `Bearer ${token}`);
  return headers;
}

function responseHeaders(response: Response): Headers {
  const headers = new Headers(response.headers);
  // fetch may transparently decompress the body, so forwarding these values can
  // produce an invalid response. Next.js will calculate transfer framing.
  headers.delete('connection');
  headers.delete('content-encoding');
  headers.delete('content-length');
  headers.delete('transfer-encoding');
  return headers;
}

async function proxyRequest(request: NextRequest, context: RouteContext): Promise<Response> {
  const originRejection = rejectUntrustedFrontendRequest(request);
  if (originRejection) return originRejection;

  const token = backendToken();
  if (!token) {
    return Response.json(
      { detail: 'Backend API token is not configured' },
      { status: 503 },
    );
  }

  const { path } = await context.params;
  const method = request.method.toUpperCase();
  const canHaveBody = method !== 'GET' && method !== 'HEAD';
  const init: RequestInit & { duplex?: 'half' } = {
    method,
    headers: requestHeaders(request, token),
    body: canHaveBody ? request.body : undefined,
    cache: 'no-store',
    redirect: 'manual',
  };
  if (canHaveBody && request.body) init.duplex = 'half';

  try {
    const upstream = await fetch(buildBackendUrl(request, path), init);
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders(upstream),
    });
  } catch {
    // Do not serialize fetch error objects: they can contain internal target
    // details. The actionable status is exposed without the raw exception.
    console.error('[API proxy] backend request failed');
    return Response.json({ detail: 'Backend unavailable' }, { status: 502 });
  }
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const PATCH = proxyRequest;
export const DELETE = proxyRequest;
export const OPTIONS = proxyRequest;
