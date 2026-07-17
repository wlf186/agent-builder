import { expect, test } from '@playwright/test';

import { DebugLogger, sanitizeForLogging } from '../src/lib/debugLogger';

test('DebugLogger uses the caller-provided X-Request-ID value', () => {
  const logger = DebugLogger.create('req-shared-trace-id');
  expect(logger.getRequestId()).toBe('req-shared-trace-id');
});

test('log sanitization is cycle, depth, item and secret bounded', () => {
  const cyclic: Record<string, unknown> = {};
  cyclic.self = cyclic;

  const sanitized = sanitizeForLogging({
    cyclic,
    values: Array.from({ length: 50 }, (_, index) => index),
    password: 'do-not-export',
    nested: { a: { b: { c: { d: { e: { f: 'too deep' } } } } } },
  });
  const serialized = JSON.stringify(sanitized);

  expect(serialized).toContain('[Circular]');
  expect(serialized).toContain('30 more items omitted');
  expect(serialized).toContain('[REDACTED]');
  expect(serialized).toContain('[Max depth reached]');
  expect(serialized).not.toContain('do-not-export');
});

test('message-like payload fields are summarized instead of copied', () => {
  const privateText = 'a private user prompt';
  const serialized = JSON.stringify(sanitizeForLogging({ message: privateText }));

  expect(serialized).toContain(`${privateText.length} chars`);
  expect(serialized).not.toContain(privateText);
});
