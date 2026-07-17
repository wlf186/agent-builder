/**
 * 知识库 API 客户端
 */

import { apiPath } from '@/lib/apiPath';

// ============================================================================
// 数据模型
// ============================================================================

export interface KnowledgeBase {
  kb_id: string;
  name: string;
  description: string;
  embedding_model: string;
  created_at: string;
  updated_at: string;
  doc_count: number;
  chunk_count: number;
  total_size: number;
}

export interface Document {
  doc_id: string;
  filename: string;
  file_size: number;
  mime_type: string;
  chunk_count: number;
  char_count: number;
  status: 'processing' | 'ready' | 'failed';
  uploaded_at: string;
  processed_at?: string;
  error_message?: string;
}

export interface RetrievalResult {
  content: string;
  doc_id: string;
  filename: string;
  score: number;
  chunk_index: number;
}

export interface CreateKBRequest {
  name: string;
  description?: string;
  embedding_model?: string;
}

export interface SearchRequest {
  query: string;
  top_k?: number;
  score_threshold?: number;
}

// ============================================================================
// API 客户端
// ============================================================================

export const kbApi = {
  /**
   * 列出所有知识库
   */
  async listKnowledgeBases(): Promise<KnowledgeBase[]> {
    const res = await fetch(apiPath('knowledge-bases'));
    if (!res.ok) throw new Error('获取知识库列表失败');
    const data = await res.json();
    return data.knowledge_bases;
  },

  /**
   * 创建知识库
   */
  async createKnowledgeBase(req: CreateKBRequest): Promise<KnowledgeBase> {
    const res = await fetch(apiPath('knowledge-bases'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    });
    if (!res.ok) throw new Error('创建知识库失败');
    return await res.json();
  },

  /**
   * 获取知识库详情
   */
  async getKnowledgeBase(kbId: string): Promise<KnowledgeBase> {
    const res = await fetch(apiPath('knowledge-bases', kbId));
    if (!res.ok) throw new Error('获取知识库详情失败');
    return await res.json();
  },

  /**
   * 删除知识库
   */
  async deleteKnowledgeBase(kbId: string): Promise<void> {
    const res = await fetch(apiPath('knowledge-bases', kbId), {
      method: 'DELETE',
    });
    if (!res.ok) throw new Error('删除知识库失败');
  },

  /**
   * 列出知识库中的文档
   */
  async listDocuments(kbId: string): Promise<Document[]> {
    const res = await fetch(apiPath('knowledge-bases', kbId, 'documents'));
    if (!res.ok) throw new Error('获取文档列表失败');
    const data = await res.json();
    return data.documents;
  },

  /**
   * 上传文档到知识库
   */
  async uploadDocument(kbId: string, file: File): Promise<Document> {
    const formData = new FormData();
    formData.append('file', file);

    const res = await fetch(apiPath('knowledge-bases', kbId, 'documents'), {
      method: 'POST',
      body: formData,
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || '上传文档失败');
    }
    return await res.json();
  },

  /**
   * 删除文档
   */
  async deleteDocument(kbId: string, docId: string): Promise<void> {
    const res = await fetch(
      apiPath('knowledge-bases', kbId, 'documents', docId),
      { method: 'DELETE' }
    );
    if (!res.ok) throw new Error('删除文档失败');
  },

  /**
   * 检索知识库
   */
  async search(kbId: string, req: SearchRequest): Promise<RetrievalResult[]> {
    const res = await fetch(apiPath('knowledge-bases', kbId, 'search'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    });
    if (!res.ok) throw new Error('检索失败');
    const data = await res.json();
    return data.results;
  },

  /**
   * 获取知识库统计信息
   */
  async getStats(kbId: string): Promise<{ kb_id: string; doc_count: number; chunk_count: number; total_size: number }> {
    const res = await fetch(apiPath('knowledge-bases', kbId, 'stats'));
    if (!res.ok) throw new Error('获取统计信息失败');
    return await res.json();
  },
};
