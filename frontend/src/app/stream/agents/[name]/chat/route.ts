/**
 * ============================================================================
 * 【流式输出代理端点 - 谨慎修改】
 *
 * 此文件是前端流式请求的专用代理，绕过 Next.js rewrites 的 /api 代理。
 * 使用路径: /stream/agents/{name}/chat
 *
 * 为什么需要专用路径？
 * - Next.js rewrites 代理可能会缓冲响应，导致流式输出失效
 * - 专用路径确保 SSE 数据能实时透传到前端
 *
 * 关键实现：
 * 1. runtime: 'nodejs' - 使用 Node.js 运行时
 * 2. dynamic: 'force-dynamic' - 禁用缓存
 * 3. 直接透传 res.body - 不做任何处理，保持流式特性
 * 4. 禁用缓冲 Headers - Cache-Control, X-Accel-Buffering
 *
 * ⚠️ 修改此文件可能影响：
 * - 流式输出的实时性
 * - SSE 连接稳定性
 * - 打字机效果
 *
 * 相关文件：
 * - backend.py: chat_stream() - 后端 SSE 端点
 * - src/agent_engine.py: stream() - 事件生成
 * - frontend/src/components/AgentChat.tsx - 前端渲染
 * ============================================================================
 */
import { NextRequest } from 'next/server';
import { rejectUntrustedFrontendRequest } from '@/lib/serverOrigin';

const BACKEND_URL = process.env.AGENT_BUILDER_BACKEND_URL || 'http://127.0.0.1:20881';

// 【关键】使用 nodejs 运行时，确保流式响应能正确处理
export const runtime = 'nodejs';
// 【关键】禁用缓存，确保每次请求都是实时的
export const dynamic = 'force-dynamic';

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  const originRejection = rejectUntrustedFrontendRequest(request);
  if (originRejection) return originRejection;

  const token = process.env.AGENT_BUILDER_API_TOKEN?.trim();
  if (!token) {
    return Response.json(
      { detail: 'Backend API token is not configured' },
      { status: 503 },
    );
  }

  const { name } = await params;
  const requestId = request.headers.get('x-request-id');

  // 转发请求到后端 SSE 端点
  let res: Response;
  try {
    res = await fetch(`${BACKEND_URL}/api/agents/${encodeURIComponent(name)}/chat/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': request.headers.get('content-type') || 'application/json',
        'Authorization': `Bearer ${token}`,
        ...(requestId ? { 'X-Request-ID': requestId } : {}),
      },
      body: request.body,
      cache: 'no-store',  // 【关键】禁用 fetch 缓存
      duplex: 'half',
    } as RequestInit & { duplex: 'half' });
  } catch {
    console.error('[stream proxy] backend request failed');
    return Response.json({ detail: 'Backend unavailable' }, { status: 502 });
  }

  if (!res.ok) {
    return new Response(res.body, {
      status: res.status,
      headers: { 'Content-Type': res.headers.get('content-type') || 'application/json' },
    });
  }

  // 【流式输出核心】直接透传流式响应，不做任何缓冲或处理
  // res.body 是 ReadableStream，直接透传确保流式特性
  return new Response(res.body, {
    status: res.status,
    headers: {
      'Content-Type': 'text/event-stream',  // 【关键】SSE MIME 类型
      'Cache-Control': 'no-cache, no-store, must-revalidate',  // 【关键】禁用所有缓存
      'X-Accel-Buffering': 'no',  // 【关键】禁用 nginx 缓冲
      ...(res.headers.get('x-request-id') ? { 'X-Request-ID': res.headers.get('x-request-id')! } : {}),
    },
  });
}
