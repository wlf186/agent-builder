/**
 * @userGuide
 * @title.en Knowledge Base Management
 * @title.zh 知识库管理
 * @category core
 * @description.en Create and manage knowledge bases for your agents.
 *   Knowledge bases store documents that agents can search for relevant information.
 * @description.zh 创建和管理智能体的知识库。
 *   知识库存储智能体可以搜索的文档，以获取相关信息。
 *
 * @steps.en
 *   1. Click "New Knowledge Base" to create a new knowledge base
 *   2. Enter a name and optional description
 *   3. Click "Save" to create the knowledge base
 *   4. Upload documents to the knowledge base for indexing
 * @steps.zh
 *   1. 点击"新建知识库"创建新的知识库
 *   2. 输入名称和可选描述
 *   3. 点击"保存"创建知识库
 *   4. 向知识库上传文档以进行索引
 *
 * @tips.en
 *   - Use descriptive names to identify knowledge bases easily
 *   - Group related documents in the same knowledge base
 *   - Supported formats: PDF, DOCX, TXT, MD
 * @tips.zh
 *   - 使用描述性名称以便轻松识别知识库
 *   - 将相关文档分组到同一知识库
 *   - 支持的格式：PDF、DOCX、TXT、MD
 *
 * @related KnowledgeBaseSelector, DocumentUploader
 */
"use client";

import { useState } from "react";
import { X } from "lucide-react";
import { kbApi, KnowledgeBase } from "@/lib/kbApi";

interface KnowledgeBaseDialogProps {
  knowledgeBase: KnowledgeBase | null;
  onClose: () => void;
  onSave: () => void;
}

export function KnowledgeBaseDialog({ knowledgeBase, onClose, onSave }: KnowledgeBaseDialogProps) {
  const [name, setName] = useState(() => knowledgeBase?.name ?? "");
  const [description, setDescription] = useState(() => knowledgeBase?.description ?? "");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const isEdit = knowledgeBase !== null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (!name.trim()) {
      setError("请输入知识库名称");
      return;
    }

    setLoading(true);
    try {
      if (isEdit) {
        // TODO: 实现更新 API
        alert("编辑功能暂未实现");
      } else {
        await kbApi.createKnowledgeBase({
          name: name.trim(),
          description: description.trim(),
        });
        onSave();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-[#1a1a2e] rounded-2xl shadow-2xl border border-white/10 w-full max-w-md mx-4">
        <div className="flex items-center justify-between p-4 border-b border-white/10 bg-white/[0.02]">
          <h2 className="text-lg font-semibold text-white">
            {isEdit ? "编辑知识库" : "新建知识库"}
          </h2>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-white hover:bg-white/10 rounded transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-4 space-y-4">
          {error && (
            <div className="p-3 bg-red-500/20 border border-red-500/30 text-red-400 rounded-lg text-sm">
              {error}
            </div>
          )}

          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">
              名称 <span className="text-red-400">*</span>
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="例如：公司制度库"
              className="w-full px-3 py-2 bg-white/5 border border-white/20 text-white placeholder-gray-500 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:border-emerald-500"
              disabled={loading}
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">
              描述
            </label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="描述这个知识库的用途..."
              rows={3}
              className="w-full px-3 py-2 bg-white/5 border border-white/20 text-white placeholder-gray-500 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:border-emerald-500 resize-none"
              disabled={loading}
            />
          </div>

          <div className="bg-white/5 border border-white/10 p-3 rounded-lg">
            <p className="text-sm text-gray-300">
              <strong className="text-white">嵌入模型：</strong> BAAI/bge-small-zh-v1.5
            </p>
            <p className="text-xs text-gray-500 mt-1">
              中文优化，512 维向量，支持 PDF/DOCX/TXT/MD 格式
            </p>
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-gray-300 hover:bg-white/10 rounded-lg transition-colors"
              disabled={loading}
            >
              取消
            </button>
            <button
              type="submit"
              className="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 transition-colors disabled:opacity-50"
              disabled={loading}
            >
              {loading ? "处理中..." : isEdit ? "保存" : "创建"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
