/**
 * @userGuide
 * @title.en Document Uploader
 * @title.zh 文档上传器
 * @category core
 * @description.en Upload documents to a knowledge base for indexing and search.
 *   Drag and drop or click to select files for batch upload.
 * @description.zh 向知识库上传文档以进行索引和搜索。
 *   拖放或点击选择文件进行批量上传。
 *
 * @steps.en
 *   1. Open the document uploader from the knowledge base detail page
 *   2. Drag and drop files onto the upload area, or click to browse
 *   3. Review the file list before uploading
 *   4. Click "Upload" to start uploading all files
 *   5. Wait for all files to complete processing
 * @steps.zh
 *   1. 从知识库详情页打开文档上传器
 *   2. 将文件拖放到上传区域，或点击浏览
 *   3. 上传前查看文件列表
 *   4. 点击"上传"开始上传所有文件
 *   5. 等待所有文件完成处理
 *
 * @tips.en
 *   - Supported formats: PDF, DOCX, TXT, MD
 *   - Maximum file size: 10MB per file
 *   - Files are processed one at a time
 *   - Upload status shows progress for each file
 * @tips.zh
 *   - 支持的格式：PDF、DOCX、TXT、MD
 *   - 最大文件大小：每个文件10MB
 *   - 文件逐个处理
 *   - 上传状态显示每个文件的进度
 *
 * @related KnowledgeBaseDialog, KnowledgeBaseSelector
 */
"use client";

import { useState, useRef, useCallback } from "react";
import { X, Upload, FileText, CheckCircle, AlertCircle, Loader2 } from "lucide-react";
import { kbApi, type Document } from "@/lib/kbApi";

interface DocumentUploaderProps {
  kbId: string;
  onClose: () => void;
  onUploaded: () => void;
}

interface UploadFile {
  file: File;
  status: "pending" | "uploading" | "success" | "error";
  error?: string;
  result?: Document;
}

const SUPPORTED_FORMATS = [".pdf", ".txt", ".md", ".docx"];

export function DocumentUploader({ kbId, onClose, onUploaded }: DocumentUploaderProps) {
  const [files, setFiles] = useState<UploadFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const droppedFiles = Array.from(e.dataTransfer.files).filter((file) => {
      const ext = "." + file.name.split(".").pop()?.toLowerCase();
      return SUPPORTED_FORMATS.includes(ext);
    });

    const newFiles = droppedFiles.map((file) => ({
      file,
      status: "pending" as const,
    }));

    setFiles((prev) => [...prev, ...newFiles]);
  }, []);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
  }, []);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFiles = Array.from(e.target.files || []);
    const newFiles = selectedFiles.map((file) => ({
      file,
      status: "pending" as const,
    }));

    setFiles((prev) => [...prev, ...newFiles]);
    e.target.value = "";
  };

  const handleRemove = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const handleUpload = async () => {
    if (files.length === 0) return;

    setUploading(true);

    // 逐个上传
    for (let i = 0; i < files.length; i++) {
      const fileItem = files[i];
      if (fileItem.status !== "pending") continue;

      setFiles((prev) =>
        prev.map((f, idx) =>
          idx === i ? { ...f, status: "uploading" } : f
        )
      );

      try {
        const result = await kbApi.uploadDocument(kbId, fileItem.file);
        setFiles((prev) =>
          prev.map((f, idx) =>
            idx === i ? { ...f, status: "success", result } : f
          )
        );
      } catch (error) {
        setFiles((prev) =>
          prev.map((f, idx) =>
            idx === i
              ? { ...f, status: "error", error: error instanceof Error ? error.message : "上传失败" }
              : f
          )
        );
      }
    }

    setUploading(false);
  };

  const handleDone = () => {
    onUploaded();
    onClose();
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  };

  const allDone = files.every((f) => f.status === "success" || f.status === "error");
  const hasSuccess = files.some((f) => f.status === "success");

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white rounded-lg shadow-xl w-full max-w-2xl mx-4 max-h-[80vh] flex flex-col">
        <div className="flex items-center justify-between p-4 border-b">
          <h2 className="text-lg font-semibold">上传文档</h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-gray-100 rounded transition-colors"
            disabled={uploading}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-4 flex-1 overflow-auto">
          {/* 拖放区域 */}
          <div
            onDrop={handleDrop}
            onDragOver={handleDragOver}
            onClick={() => fileInputRef.current?.click()}
            className="border-2 border-dashed border-gray-300 rounded-lg p-8 text-center cursor-pointer hover:border-emerald-400 hover:bg-emerald-50/50 transition-colors mb-4"
          >
            <Upload className="w-12 h-12 mx-auto text-gray-400 mb-3" />
            <p className="text-gray-700 font-medium">点击或拖拽文件到此处上传</p>
            <p className="text-sm text-gray-500 mt-1">
              支持 PDF, DOCX, TXT, MD 格式，单个文件最大 10MB
            </p>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".pdf,.docx,.txt,.md"
              onChange={handleFileSelect}
              className="hidden"
            />
          </div>

          {/* 文件列表 */}
          {files.length > 0 && (
            <div className="space-y-2">
              <h3 className="text-sm font-medium text-gray-700">
                待上传文件 ({files.length})
              </h3>
              <div className="space-y-2">
                {files.map((fileItem, index) => (
                  <div
                    key={index}
                    className="flex items-center gap-3 p-3 bg-gray-50 rounded-lg"
                  >
                    <div className="p-2 bg-white rounded-lg">
                      <FileText className="w-5 h-5 text-gray-600" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="font-medium text-gray-900 truncate">{fileItem.file.name}</p>
                      <p className="text-sm text-gray-500">
                        {formatFileSize(fileItem.file.size)}
                      </p>
                      {fileItem.error && (
                        <p className="text-sm text-red-600">{fileItem.error}</p>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      {fileItem.status === "pending" && (
                        <button
                          onClick={() => handleRemove(index)}
                          className="p-1 text-gray-400 hover:text-red-600"
                          disabled={uploading}
                        >
                          <X className="w-5 h-5" />
                        </button>
                      )}
                      {fileItem.status === "uploading" && (
                        <Loader2 className="w-5 h-5 text-emerald-600 animate-spin" />
                      )}
                      {fileItem.status === "success" && (
                        <CheckCircle className="w-5 h-5 text-emerald-600" />
                      )}
                      {fileItem.status === "error" && (
                        <AlertCircle className="w-5 h-5 text-red-600" />
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="flex justify-between p-4 border-t">
          <button
            onClick={onClose}
            className="px-4 py-2 text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
            disabled={uploading}
          >
            取消
          </button>
          <div className="flex gap-3">
            {files.length > 0 && !uploading && (
              <button
                onClick={handleUpload}
                className="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 transition-colors"
              >
                上传 ({files.filter((f) => f.status === "pending").length})
              </button>
            )}
            {allDone && (
              <button
                onClick={handleDone}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
              >
                完成 ({hasSuccess ? `成功 ${files.filter((f) => f.status === "success").length} 个` : "关闭"})
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
