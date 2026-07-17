"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { kbApi, KnowledgeBase, Document, RetrievalResult } from "@/lib/kbApi";
import { ArrowLeft, Upload, Trash2, FileText, Search, Loader2 } from "lucide-react";
import { DocumentUploader } from "@/components/DocumentUploader";

export default function KnowledgeBaseDetailPage() {
  const params = useParams();
  const kbId = params.id as string;

  const [knowledgeBase, setKnowledgeBase] = useState<KnowledgeBase | null>(null);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploadDialogOpen, setUploadDialogOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchResults, setSearchResults] = useState<RetrievalResult[]>([]);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [kb, docs] = await Promise.all([
        kbApi.getKnowledgeBase(kbId),
        kbApi.listDocuments(kbId),
      ]);
      setKnowledgeBase(kb);
      setDocuments(docs);
    } catch (error) {
      console.error("加载失败:", error);
    } finally {
      setLoading(false);
    }
  }, [kbId]);

  useEffect(() => {
    const timeout = window.setTimeout(() => void loadData(), 0);
    return () => window.clearTimeout(timeout);
  }, [loadData]);

  const handleDeleteDocument = async (docId: string) => {
    if (!confirm("确定要删除这个文档吗？")) return;
    try {
      await kbApi.deleteDocument(kbId, docId);
      await loadData();
    } catch (error) {
      alert("删除失败: " + (error instanceof Error ? error.message : "未知错误"));
    }
  };

  const handleSearch = async () => {
    if (!searchQuery.trim()) return;
    setSearching(true);
    try {
      const results = await kbApi.search(kbId, {
        query: searchQuery,
        top_k: 3,
        score_threshold: 0.6,
      });
      setSearchResults(results);
    } catch (error) {
      alert("检索失败: " + (error instanceof Error ? error.message : "未知错误"));
    } finally {
      setSearching(false);
    }
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <Loader2 className="w-8 h-8 animate-spin text-emerald-600" />
      </div>
    );
  }

  if (!knowledgeBase) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center">
          <h2 className="text-xl font-semibold text-gray-900">知识库不存在</h2>
          <Link href="/knowledge-bases" className="text-emerald-600 hover:underline">
            返回列表
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50 p-6">
      <div className="max-w-4xl mx-auto">
        {/* 头部 */}
        <div className="flex items-center gap-4 mb-6">
          <Link
            href="/knowledge-bases"
            className="p-2 hover:bg-gray-200 rounded-lg transition-colors"
          >
            <ArrowLeft className="w-5 h-5" />
          </Link>
          <div className="flex-1">
            <h1 className="text-2xl font-bold text-gray-900">{knowledgeBase.name}</h1>
            <p className="text-gray-500 mt-1">{knowledgeBase.description || "暂无描述"}</p>
          </div>
          <button
            onClick={() => setUploadDialogOpen(true)}
            className="flex items-center gap-2 px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 transition-colors"
          >
            <Upload className="w-5 h-5" />
            上传文档
          </button>
        </div>

        {/* 统计卡片 */}
        <div className="grid grid-cols-3 gap-4 mb-6">
          <div className="bg-white p-4 rounded-lg border border-gray-200">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-emerald-100 rounded-lg">
                <FileText className="w-5 h-5 text-emerald-600" />
              </div>
              <div>
                <p className="text-sm text-gray-500">文档数</p>
                <p className="text-2xl font-semibold">{knowledgeBase.doc_count}</p>
              </div>
            </div>
          </div>
          <div className="bg-white p-4 rounded-lg border border-gray-200">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-blue-100 rounded-lg">
                <Search className="w-5 h-5 text-blue-600" />
              </div>
              <div>
                <p className="text-sm text-gray-500">文档块</p>
                <p className="text-2xl font-semibold">{knowledgeBase.chunk_count}</p>
              </div>
            </div>
          </div>
          <div className="bg-white p-4 rounded-lg border border-gray-200">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-purple-100 rounded-lg">
                <FileText className="w-5 h-5 text-purple-600" />
              </div>
              <div>
                <p className="text-sm text-gray-500">总大小</p>
                <p className="text-2xl font-semibold">
                  {formatFileSize(knowledgeBase.total_size)}
                </p>
              </div>
            </div>
          </div>
        </div>

        {/* 检索测试 */}
        <div className="bg-white p-4 rounded-lg border border-gray-200 mb-6">
          <h3 className="font-semibold text-gray-900 mb-3">检索测试</h3>
          <div className="flex gap-2">
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
              placeholder="输入测试问题..."
              className="flex-1 px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500"
            />
            <button
              onClick={handleSearch}
              disabled={searching}
              className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
            >
              {searching ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Search className="w-4 h-4" />
              )}
              检索
            </button>
          </div>

          {searchResults.length > 0 && (
            <div className="mt-4 space-y-3">
              <h4 className="text-sm font-medium text-gray-700">检索结果：</h4>
              {searchResults.map((result, i) => (
                <div key={i} className="p-3 bg-gray-50 rounded-lg">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs text-gray-500">{result.filename}</span>
                    <span className="text-xs bg-emerald-100 text-emerald-700 px-2 py-0.5 rounded">
                      相关度: {(result.score * 100).toFixed(0)}%
                    </span>
                  </div>
                  <p className="text-sm text-gray-700">{result.content}</p>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 文档列表 */}
        <div className="bg-white rounded-lg border border-gray-200">
          <div className="p-4 border-b border-gray-200">
            <h3 className="font-semibold text-gray-900">文档列表 ({documents.length})</h3>
          </div>
          {documents.length === 0 ? (
            <div className="p-8 text-center text-gray-500">
              <FileText className="w-12 h-12 mx-auto mb-3 text-gray-300" />
              <p>还没有上传文档</p>
              <button
                onClick={() => setUploadDialogOpen(true)}
                className="mt-3 text-emerald-600 hover:underline"
              >
                上传第一个文档
              </button>
            </div>
          ) : (
            <div className="divide-y divide-gray-100">
              {documents.map((doc) => (
                <div
                  key={doc.doc_id}
                  className="p-4 flex items-center justify-between hover:bg-gray-50"
                >
                  <div className="flex items-center gap-3 flex-1 min-w-0">
                    <div className="p-2 bg-gray-100 rounded-lg">
                      <FileText className="w-5 h-5 text-gray-600" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="font-medium text-gray-900 truncate">{doc.filename}</p>
                      <p className="text-sm text-gray-500">
                        {formatFileSize(doc.file_size)} · {doc.chunk_count} 个文档块
                      </p>
                    </div>
                  </div>
                  <button
                    onClick={() => handleDeleteDocument(doc.doc_id)}
                    className="p-2 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                  >
                    <Trash2 className="w-5 h-5" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 上传对话框 */}
      {uploadDialogOpen && (
        <DocumentUploader
          kbId={kbId}
          onClose={() => setUploadDialogOpen(false)}
          onUploaded={loadData}
        />
      )}
    </div>
  );
}
