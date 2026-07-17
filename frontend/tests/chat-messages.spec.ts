import { expect, test } from '@playwright/test';
import {
  buildHistoryMessages,
  finalizeTurnMessages,
  selectConversationPersistence,
} from '../src/lib/chatMessages';

test('history contains only messages from before the current turn', () => {
  const previousMessages = [
    { id: 'u1', role: 'user' as const, content: 'first question' },
    { id: 'a1', role: 'assistant' as const, content: 'first answer' },
    { id: 'placeholder', role: 'assistant' as const, content: '   ' },
  ];

  expect(buildHistoryMessages(previousMessages, 5)).toEqual([
    { role: 'user', content: 'first question' },
    { role: 'assistant', content: 'first answer' },
  ]);
  expect(buildHistoryMessages(previousMessages, 0)).toEqual([]);
});

test('finalization commits streamed assistant content and returns only this turn', () => {
  const result = finalizeTurnMessages(
    [
      { id: 'u1', role: 'user' as const, content: 'old question' },
      { id: 'a1', role: 'assistant' as const, content: 'old answer' },
      { id: 'u2', role: 'user' as const, content: 'new question' },
      { id: 'a2', role: 'assistant' as const, content: '' },
    ],
    'u2',
    'a2',
    'final streamed answer',
  );

  expect(result.messages.at(-1)?.content).toBe('final streamed answer');
  expect(result.turnMessages.map(({ id, content }) => ({ id, content }))).toEqual([
    { id: 'u2', content: 'new question' },
    { id: 'a2', content: 'final streamed answer' },
  ]);
});

test('normal turns use incremental persistence while explicit snapshots remain available', () => {
  const fullHistory = [{ id: 'old' }, { id: 'user' }, { id: 'assistant' }];
  const currentTurn = fullHistory.slice(-2);

  expect(selectConversationPersistence(fullHistory, currentTurn)).toEqual({
    mode: 'sync',
    messages: currentTurn,
  });
  expect(selectConversationPersistence(fullHistory)).toEqual({
    mode: 'replace',
    messages: fullHistory,
  });
});
