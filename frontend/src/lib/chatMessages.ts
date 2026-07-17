export interface HistoryMessage {
  role: 'user' | 'assistant';
  content: string;
}

export function buildHistoryMessages<T extends HistoryMessage>(
  messagesBeforeTurn: readonly T[],
  shortTermMemory: number,
): HistoryMessage[] {
  const historyLimit = Math.max(0, Math.floor(shortTermMemory) * 2);
  if (historyLimit === 0) return [];

  return messagesBeforeTurn
    .filter((message) => message.role !== 'assistant' || message.content.trim().length > 0)
    .slice(-historyLimit)
    .map(({ role, content }) => ({ role, content }));
}

export function finalizeTurnMessages<T extends { id: string; content: string }>(
  messages: readonly T[],
  userMessageId: string,
  assistantMessageId: string,
  streamedContent: string,
): { messages: T[]; turnMessages: T[] } {
  const finalizedMessages = messages.map((message) =>
    message.id === assistantMessageId
      ? { ...message, content: streamedContent || message.content }
      : message,
  );
  const turnMessages = finalizedMessages.filter(
    (message) => message.id === userMessageId || message.id === assistantMessageId,
  );
  return { messages: finalizedMessages, turnMessages };
}

export function selectConversationPersistence<T>(
  allMessages: readonly T[],
  turnMessages?: readonly T[],
): { mode: 'sync' | 'replace'; messages: readonly T[] } {
  if (turnMessages && turnMessages.length > 0) {
    return { mode: 'sync', messages: turnMessages };
  }
  return { mode: 'replace', messages: allMessages };
}
