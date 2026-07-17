/**
 * 文件上传组件
 *
 * 根据 UX 设计稿 (iteration-2603111255) 实现:
 * - 回形针图标上传按钮
 * - 拖拽上传视觉反馈
 * - 文件预览卡片 (64px x 80px)
 * - 悬停显示删除按钮
 * - 上传进度反馈
 * - 支持的文件类型: PDF, DOCX, XLSX, TXT, CSV, JSON, 图片
 *
 * 颜色规范 (UX设计稿 3.1):
 * - PDF: text-red-400 (#f87171)
 * - Word: text-blue-400 (#60a5fa)
 * - Excel: text-green-400 (#4ade80)
 * - 图片: text-purple-400 (#c084fc)
 * - 通用: text-gray-400 (#9ca3af)
 */
'use client';

/**
 * @userGuide
 * @title.en File Uploader
 * @title.zh 文件上传器
 * @category core
 * @description.en Upload files to attach to chat messages. Supports drag-and-drop, file preview cards, and multiple file types.
 * @description.zh 上传文件并附加到聊天消息，支持拖拽上传、文件预览卡片和多种文件类型。
 * @steps.en
 *   1. Click the paperclip icon to open file picker
 *   2. Or drag and drop files directly onto the chat area
 *   3. Preview selected files in the card view
 *   4. Hover over a file card to remove it
 *   5. Send message with attached files
 * @steps.zh
 *   1. 点击回形针图标打开文件选择器
 *   2. 或直接拖拽文件到聊天区域
 *   3. 在卡片视图中预览已选文件
 *   4. 悬停文件卡片可移除文件
 *   5. 发送消息时一并上传附件
 * @tips.en
 *   - Supports PDF, DOCX, XLSX, TXT, CSV, JSON, and images
 *   - Maximum 3 files per message, 100MB per file
 *   - Files are uploaded when the message is sent
 * @tips.zh
 *   - 支持 PDF、DOCX、XLSX、TXT、CSV、JSON 和图片格式
 *   - 每条消息最多 3 个文件，单个文件最大 100MB
 *   - 文件在发送消息时上传
 * @related AgentChat
 */

import { useState, useRef, useCallback, useMemo } from 'react';
import { Paperclip, X, FileText, FileSpreadsheet, Image as ImageIcon, File, Upload } from 'lucide-react';
import {
  PendingFile,
  UploadedFile,
  DEFAULT_FILE_CONFIG,
  formatFileSize,
} from '@/types';
import { isFileTypeAllowed, isFileSizeValid } from '@/lib/fileApi';

interface FileUploaderProps {
  /** 已选择的文件列表 */
  files: PendingFile[];
  /** 文件变化回调 */
  onFilesChange: (files: PendingFile[]) => void;
  /** 最大文件大小，默认 100MB */
  maxFileSize?: number;
  /** 允许的文件类型 */
  allowedTypes?: string[];
  /** 最大文件数量，默认 3 */
  maxFiles?: number;
  /** 禁用状态 */
  disabled?: boolean;
  /** 语言 */
  locale?: 'zh' | 'en';
  /** 上传进度 (0-100) */
  uploadProgress?: number;
  /** 是否正在上传 */
  isUploading?: boolean;
  /** 当前上传中的文件名 */
  uploadingFileName?: string;
  /**
   * 上传完成回调
   * @param uploadedFiles 上传成功的文件列表（包含 file_id）
   * @param failedFiles 上传失败的文件名列表
   */
  onUploadComplete?: (uploadedFiles: UploadedFile[], failedFiles: string[]) => void;
}

/**
 * 获取文件图标组件
 * 根据 UX 设计稿颜色规范
 */
function FileIconComponent({ type, className }: { type: string; className?: string }) {
  if (type === 'application/pdf') {
    return <FileText className={`${className} text-red-400`} />;
  }
  if (type.includes('word') || type.includes('document')) {
    return <FileText className={`${className} text-blue-400`} />;
  }
  if (type.includes('spreadsheet') || type.includes('excel')) {
    return <FileSpreadsheet className={`${className} text-green-400`} />;
  }
  if (type.startsWith('image/')) {
    return <ImageIcon className={`${className} text-purple-400`} />;
  }
  return <File className={`${className} text-gray-400`} />;
}

/**
 * 生成唯一 ID
 */
function generateId(): string {
  return `file-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

/**
 * 文件预览卡片
 *
 * 尺寸规范 (UX设计稿 3.2):
 * - 卡片宽度: 64px (w-16)
 * - 卡片高度: 80px (h-20)
 * - 图标大小: 24px (w-6 h-6)
 * - 删除按钮: 16px (w-4 h-4)
 */
function FilePreviewCard({
  file,
  onRemove,
  locale = 'zh',
  disabled = false,
}: {
  file: PendingFile;
  onRemove: () => void;
  locale?: 'zh' | 'en';
  disabled?: boolean;
}) {
  const t = {
    removeFile: locale === 'zh' ? '移除文件' : 'Remove file',
    fileName: file.name.length > 8 ? `${file.name.slice(0, 7)}...` : file.name,
  };

  return (
    <div
      role="listitem"
      aria-label={`${file.name}, ${formatFileSize(file.size)}`}
      className={`
        file-card w-16 h-20 bg-white/5 border border-white/10 rounded-lg p-1.5
        relative flex flex-col items-center justify-center
        group transition-colors
        ${disabled ? 'opacity-50' : 'hover:bg-white/10 cursor-pointer'}
      `}
    >
      {/* 删除按钮 - 悬停时显示 */}
      {!disabled && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
          aria-label={t.removeFile}
          className="delete-btn absolute -top-1 -right-1 w-4 h-4 bg-red-500/80 hover:bg-red-500 rounded-full flex items-center justify-center text-white opacity-0 group-hover:opacity-100 transition-opacity"
          title={t.removeFile}
        >
          <X className="w-2.5 h-2.5" />
        </button>
      )}

      {/* 文件图标 */}
      <FileIconComponent type={file.type} className="w-6 h-6" />

      {/* 文件名 - 最多8字符 */}
      <span className="text-[8px] text-gray-300 truncate max-w-full mt-1 text-center">
        {t.fileName}
      </span>

      {/* 文件大小 */}
      <span className="text-[7px] text-gray-500">
        {formatFileSize(file.size)}
      </span>
    </div>
  );
}

/**
 * 文件上传组件
 */
export function FileUploader({
  files,
  onFilesChange,
  maxFileSize = DEFAULT_FILE_CONFIG.maxFileSize,
  allowedTypes = DEFAULT_FILE_CONFIG.allowedTypes,
  maxFiles = DEFAULT_FILE_CONFIG.maxFiles,
  disabled = false,
  locale = 'zh',
  uploadProgress,
  isUploading = false,
  uploadingFileName,
}: FileUploaderProps) {
  const [isDragging, setIsDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // 翻译文本 - 符合 UX 设计稿 7.1 国际化支持
  const t = useMemo(() => ({
    uploadFile: locale === 'zh' ? '上传文件' : 'Upload File',
    pendingFiles: locale === 'zh' ? '待发送文件' : 'Pending Files',
    addMoreFiles: locale === 'zh' ? '添加更多' : 'Add More',
    dragDropHint: locale === 'zh' ? '拖放文件到此处上传' : 'Drop files here to upload',
    supportedTypes: locale === 'zh' ? '支持 PDF、DOCX、XLSX、TXT、CSV、JSON、图片文件' : 'Supports PDF, DOCX, XLSX, TXT, CSV, JSON, images',
    fileSizeExceeded: locale === 'zh' ? '文件大小超过限制' : 'File size exceeds limit',
    unsupportedFileType: locale === 'zh' ? '不支持的文件类型' : 'Unsupported file type',
    maxFilesExceeded: locale === 'zh' ? `单次最多上传 ${maxFiles} 个文件` : `Maximum ${maxFiles} files per upload`,
    maxFileSizeHint: locale === 'zh' ? `最大 ${Math.round(maxFileSize / (1024 * 1024))}MB` : `Max ${Math.round(maxFileSize / (1024 * 1024))}MB`,
    uploading: locale === 'zh' ? '正在上传文件...' : 'Uploading files...',
  }), [locale, maxFiles, maxFileSize]);

  /**
   * 处理文件验证和添加
   */
  const handleFiles = useCallback(
    (newFiles: FileList | File[]) => {
      if (disabled || isUploading) return;

      setError(null);
      const fileArray = Array.from(newFiles);
      const validFiles: PendingFile[] = [];
      const errors: string[] = [];

      // 检查总文件数量
      const totalFiles = files.length + fileArray.length;
      if (totalFiles > maxFiles) {
        setError(t.maxFilesExceeded);
        return;
      }

      for (const file of fileArray) {
        // 检查文件类型
        if (!isFileTypeAllowed(file, allowedTypes)) {
          errors.push(`${file.name}: ${t.unsupportedFileType}`);
          continue;
        }

        // 检查文件大小
        if (!isFileSizeValid(file, maxFileSize)) {
          errors.push(`${file.name}: ${t.fileSizeExceeded} (${t.maxFileSizeHint})`);
          continue;
        }

        // 添加到有效文件列表
        validFiles.push({
          id: generateId(),
          file,
          name: file.name,
          size: file.size,
          type: file.type,
        });
      }

      // 显示错误（如果有）
      if (errors.length > 0) {
        setError(errors.join('\n'));
      }

      // 更新文件列表
      if (validFiles.length > 0) {
        onFilesChange([...files, ...validFiles]);
      }
    },
    [files, onFilesChange, allowedTypes, maxFileSize, maxFiles, disabled, isUploading, t]
  );

  /**
   * 处理文件选择
   */
  const handleFileSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const selectedFiles = e.target.files;
      if (selectedFiles && selectedFiles.length > 0) {
        handleFiles(selectedFiles);
      }
      // 重置 input，允许重复选择同一文件
      e.target.value = '';
    },
    [handleFiles]
  );

  /**
   * 移除文件
   */
  const handleRemoveFile = useCallback(
    (fileId: string) => {
      onFilesChange(files.filter((f) => f.id !== fileId));
    },
    [files, onFilesChange]
  );

  /**
   * 打开文件选择器
   */
  const handleUploadClick = useCallback(() => {
    if (!disabled && !isUploading) {
      fileInputRef.current?.click();
    }
  }, [disabled, isUploading]);

  /**
   * 拖拽事件处理
   */
  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (!disabled && !isUploading) {
      setIsDragging(true);
    }
  }, [disabled, isUploading]);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setIsDragging(false);

      if (disabled || isUploading) return;

      const droppedFiles = e.dataTransfer.files;
      if (droppedFiles.length > 0) {
        handleFiles(droppedFiles);
      }
    },
    [disabled, isUploading, handleFiles]
  );

  // 构建accept属性 - 将MIME类型转换为文件扩展名
  const acceptTypes = allowedTypes.map(type => {
    if (type === 'application/pdf') return '.pdf';
    if (type.includes('wordprocessingml')) return '.docx';
    if (type === 'application/msword') return '.doc';
    if (type.includes('spreadsheetml')) return '.xlsx';
    if (type === 'application/vnd.ms-excel') return '.xls';
    if (type === 'text/plain') return '.txt';
    if (type === 'text/csv') return '.csv';
    if (type === 'application/json') return '.json';
    if (type === 'image/png') return '.png';
    if (type === 'image/jpeg') return '.jpg,.jpeg';
    return type;
  }).join(',');

  return (
    <div
      className={`
        relative transition-colors rounded-lg
        ${isDragging ? 'bg-blue-400/10 border-2 border-dashed border-blue-400' : ''}
      `}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {/* 上传进度条 - 仅在上传时显示 (UX设计稿 2.3.1) */}
      {isUploading && uploadProgress !== undefined && (
        <div className="mb-2 p-3 bg-blue-500/10 border border-blue-500/30 rounded-lg">
          <div className="flex items-center gap-2 mb-2">
            <Upload className="w-4 h-4 text-blue-400 animate-pulse" />
            <span className="text-xs text-blue-400">{t.uploading}</span>
          </div>
          <div className="w-full bg-white/10 rounded-full h-1.5">
            <div
              className="bg-blue-500 h-1.5 rounded-full transition-all duration-300"
              style={{ width: `${uploadProgress}%` }}
            />
          </div>
          <div className="flex justify-between mt-1">
            <span className="text-[10px] text-gray-500">
              {uploadingFileName || (files.length > 0 ? files[0].name : '')}
            </span>
            <span className="text-[10px] text-blue-400">{uploadProgress}%</span>
          </div>
        </div>
      )}

      {/* 文件预览栏 - 当有文件时显示 (UX设计稿 2.2.2) */}
      {files.length > 0 && !isUploading && (
        <div className="mb-2 p-3 bg-white/5 border border-white/10 rounded-lg">
          {/* 标题栏 */}
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-2">
              <Paperclip className="w-4 h-4 text-blue-400" />
              <span className="text-xs text-gray-400">
                {t.pendingFiles} ({files.length})
              </span>
            </div>
            {files.length < maxFiles && (
              <button
                type="button"
                onClick={handleUploadClick}
                disabled={disabled || isUploading}
                className="text-xs text-blue-400 hover:text-blue-300 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {t.addMoreFiles}
              </button>
            )}
          </div>

          {/* 文件列表 - 可水平滚动 */}
          <div role="list" aria-label={t.pendingFiles} className="flex gap-2 overflow-x-auto pb-1 scrollbar-thin">
            {files.map((file) => (
              <FilePreviewCard
                key={file.id}
                file={file}
                onRemove={() => handleRemoveFile(file.id)}
                locale={locale}
                disabled={disabled || isUploading}
              />
            ))}
          </div>
        </div>
      )}

      {/* 拖拽提示 - 仅在拖拽时显示 (UX设计稿 2.1.2) */}
      {isDragging && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/50 border-2 border-dashed border-blue-400 rounded-lg">
          <div className="text-center p-4">
            <Paperclip className="w-8 h-8 text-blue-400 mx-auto mb-2" />
            <span className="text-sm text-blue-400 font-medium">{t.dragDropHint}</span>
            <p className="text-xs text-gray-500 mt-1">{t.supportedTypes}</p>
          </div>
        </div>
      )}

      {/* 错误提示 (UX设计稿 2.3.2) */}
      {error && (
        <div className="mb-2 p-2 bg-red-500/20 border border-red-500/30 text-red-400 rounded-lg text-xs whitespace-pre-wrap animate-fadeIn">
          <div className="flex items-start gap-2">
            <span className="flex-shrink-0">⚠️</span>
            <span className="flex-1">{error}</span>
            <button
              type="button"
              onClick={() => setError(null)}
              className="text-red-400 hover:text-red-300 flex-shrink-0"
              aria-label="Close"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        </div>
      )}

      {/* 隐藏的文件输入 */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={acceptTypes}
        onChange={handleFileSelect}
        className="hidden"
        disabled={disabled || isUploading}
        aria-label={t.uploadFile}
      />
    </div>
  );
}

/**
 * 上传按钮组件（独立使用）
 *
 * 样式规范 (UX设计稿 3.2.1):
 * - 无文件: text-gray-400
 * - 有文件: text-blue-400
 * - 禁用: opacity-50
 */
export function UploadButton({
  onClick,
  hasFiles = false,
  disabled = false,
  title,
}: {
  onClick: () => void;
  hasFiles?: boolean;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={title || 'Upload file'}
      aria-describedby="upload-hint"
      className={`
        p-2 rounded-lg transition-colors
        ${hasFiles ? 'text-blue-400' : 'text-gray-400 hover:text-blue-400'}
        ${disabled ? 'opacity-50 cursor-not-allowed' : ''}
      `}
      title={title || 'Upload file'}
    >
      <Paperclip className="w-5 h-5" />
    </button>
  );
}

export default FileUploader;
