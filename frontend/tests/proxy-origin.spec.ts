import { expect, test } from '@playwright/test';

test('API proxy rejects an untrusted browser origin before attaching credentials', async ({ request }) => {
  const response = await request.post('/api/system/check-runtime', {
    headers: {
      origin: 'https://attacker.invalid',
      'sec-fetch-site': 'cross-site',
    },
  });

  expect(response.status()).toBe(403);
  await expect(response.json()).resolves.toEqual({ detail: 'Forbidden request origin' });
});

test('stream proxy rejects an untrusted browser origin', async ({ request }) => {
  const response = await request.post('/stream/agents/test/chat', {
    headers: {
      origin: 'https://attacker.invalid',
      'sec-fetch-site': 'cross-site',
      'content-type': 'application/json',
    },
    data: { message: 'blocked' },
  });

  expect(response.status()).toBe(403);
});

test('API proxy accepts its configured frontend origin', async ({ request }) => {
  const frontendOrigin = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:20815';
  const response = await request.post('/api/system/check-runtime', {
    headers: {
      origin: frontendOrigin,
      'sec-fetch-site': 'same-origin',
    },
  });

  // The backend or its token may intentionally be unavailable in an isolated
  // frontend test; this assertion only verifies that provenance passed.
  expect(response.status()).not.toBe(403);
});

test('Fetch Metadata must also report a same-origin request', async ({ request }) => {
  const frontendOrigin = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:20815';
  const response = await request.post('/api/system/check-runtime', {
    headers: {
      origin: frontendOrigin,
      'sec-fetch-site': 'cross-site',
    },
  });

  expect(response.status()).toBe(403);
});

test('proxy rejects DNS rebinding through an untrusted Host', async ({ request }) => {
  const frontendOrigin = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:20815';
  const response = await request.post('/api/system/check-runtime', {
    headers: {
      host: 'rebound.attacker.invalid',
      origin: frontendOrigin,
      'sec-fetch-site': 'same-origin',
    },
  });

  expect(response.status()).toBe(403);
});

test('a forged same-origin Fetch Metadata header cannot bypass Host validation', async ({ request }) => {
  const response = await request.post('/api/system/check-runtime', {
    headers: {
      host: 'rebound.attacker.invalid',
      'sec-fetch-site': 'same-origin',
    },
  });

  expect(response.status()).toBe(403);
});

test('Fetch Metadata alone cannot authorize an unsafe request', async ({ request }) => {
  const frontendOrigin = new URL(process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:20815');
  const response = await request.post('/api/system/check-runtime', {
    headers: {
      host: frontendOrigin.host,
      'sec-fetch-site': 'same-origin',
    },
  });

  expect(response.status()).toBe(403);
});

test('an allowed Origin cannot be paired with a different allowed Host', async ({ request }) => {
  const response = await request.post('/api/system/check-runtime', {
    headers: {
      host: '127.0.0.1:20815',
      origin: 'http://localhost:20815',
      'sec-fetch-site': 'same-origin',
    },
  });

  expect(response.status()).toBe(403);
});
