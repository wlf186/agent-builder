import { expect, test } from '@playwright/test';

import { apiPath, streamPath } from '../src/lib/apiPath';

test('apiPath encodes every dynamic segment without allowing path or query injection', () => {
  expect(apiPath('agents', 'a/b?#%', 'files', '../secret')).toBe(
    '/api/agents/a%2Fb%3F%23%25/files/..%2Fsecret',
  );
});

test('path builders preserve Unicode through standard URL encoding', () => {
  expect(streamPath('agents', '中文 agent', 'chat')).toBe(
    '/stream/agents/%E4%B8%AD%E6%96%87%20agent/chat',
  );
});

test('path builders reject dot-only segments before browser URL normalization', () => {
  expect(() => apiPath('agents', '..', 'files')).toThrow(/Dot-only/);
});
