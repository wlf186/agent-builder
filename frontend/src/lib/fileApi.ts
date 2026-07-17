/**
 * 文件上传 API 封装
 *
 * 提供文件上传、列表、删除等 API 操作
 * 支持单文件上传（后端限制）
 *
 * 后端 API 响应格式 (backend.py 第1293-1302行):
 * {
 *   "success": true,
 *   "file": {
 *     "file_id": "uuid",
 *     "filename": "report.pdf",
 *     "file_size": 2457600,
 *     "mime_type": "application/pdf",
 *     "uploaded_at": "2026-03-11T12:00:00Z"
 *   }
 * }
 */

import { UploadedFile } from '@/types';
import { apiPath } from '@/lib/apiPath';

/**
 * 后端上传响应格式
 */
interface BackendFile {
  file_id: string;
  filename: string;
  file_size: number;
  mime_type: string;
  uploaded_at: string;
}

interface UploadResponse {
  success: boolean;
  file: BackendFile;
}

interface ListFilesResponse {
  files?: BackendFile[];
}

/**
 * 上传文件到指定 Agent（单个文件）
 *
 * @param agentName Agent 名称
 * @param file 要上传的文件
 * @returns 上传后的文件信息
 */
export async function uploadFile(
  agentName: string,
  file: File
): Promise<UploadedFile> {
  const formData = new FormData();
  formData.append('file', file);

  const response = await fetch(
    apiPath('agents', agentName, 'files'),
    {
      method: 'POST',
      body: formData,
    }
  );

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`Upload failed: ${response.status} ${errorText}`);
  }

  const data: UploadResponse = await response.json();

  // 转换后端响应为前端 UploadedFile 格式
  return {
    id: data.file.file_id,
    filename: data.file.filename,
    size: data.file.file_size,
    mimeType: data.file.mime_type,
    uploadedAt: data.file.uploaded_at,
  };
}

/**
 * 批量上传文件（串行上传，因为后端只支持单文件）
 *
 * @param agentName Agent 名称
 * @param files 要上传的文件列表
 * @param onProgress 进度回调 (current, total, file, progress)
 * @returns 上传结果列表
 */
export async function uploadFiles(
  agentName: string,
  files: File[],
  onProgress?: (current: number, total: number, file: File, progress: number) => void
): Promise<UploadedFile[]> {
  const results: UploadedFile[] = [];

  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    // 计算总体进度：每个文件占 (100 / total) 的比例
    const baseProgress = (i / files.length) * 100;
    if (onProgress) {
      onProgress(i + 1, files.length, file, Math.round(baseProgress));
    }
    try {
      const result = await uploadFile(agentName, file);
      results.push(result);
      // 上传完成后更新进度
      if (onProgress) {
        onProgress(i + 1, files.length, file, Math.round(((i + 1) / files.length) * 100));
      }
    } catch (error) {
      console.error(`Failed to upload ${file.name}:`, error);
      throw error;
    }
  }

  return results;
}

/**
 * 获取 Agent 的所有上传文件
 *
 * 后端响应格式 (backend.py 第1314-1325行):
 * {
 *   "files": [
 *     {
 *       "file_id": "uuid",
 *       "filename": "report.pdf",
 *       "file_size": 2457600,
 *       "mime_type": "application/pdf",
 *       "uploaded_at": "2026-03-11T12:00:00Z"
 *     }
 *   ]
 * }
 *
 * @param agentName Agent 名称
 * @returns 文件列表
 */
export async function listFiles(
  agentName: string
): Promise<UploadedFile[]> {
  const response = await fetch(
    apiPath('agents', agentName, 'files'),
    {
      method: 'GET',
    }
  );

  if (!response.ok) {
    throw new Error(`Failed to list files: ${response.statusText}`);
  }

  const data: ListFilesResponse = await response.json();

  // 转换后端响应为前端 UploadedFile 格式
  return (data.files || []).map((f) => ({
    id: f.file_id,
    filename: f.filename,
    size: f.file_size,
    mimeType: f.mime_type,
    uploadedAt: f.uploaded_at,
  }));
}

/**
 * 删除指定文件
 *
 * @param agentName Agent 名称
 * @param fileId 文件 ID
 */
export async function deleteFile(
  agentName: string,
  fileId: string
): Promise<void> {
  const response = await fetch(
    apiPath('agents', agentName, 'files', fileId),
    {
      method: 'DELETE',
    }
  );

  if (!response.ok) {
    throw new Error(`Failed to delete file: ${response.statusText}`);
  }
}

/**
 * 获取文件下载 URL
 *
 * @param agentName Agent 名称
 * @param fileId 文件 ID
 * @returns 下载 URL
 */
export function getFileDownloadUrl(
  agentName: string,
  fileId: string
): string {
  return apiPath('agents', agentName, 'files', fileId, 'download');
}

/**
 * 检查文件是否为允许的类型
 *
 * @param file 文件对象
 * @param allowedTypes 允许的 MIME 类型列表
 * @returns 是否允许
 */
export function isFileTypeAllowed(
  file: File,
  allowedTypes: string[]
): boolean {
  // 检查 MIME 类型
  if (allowedTypes.includes(file.type)) {
    return true;
  }

  // 某些浏览器可能不识别特定的 MIME 类型，通过扩展名检查
  const extension = file.name.split('.').pop()?.toLowerCase() || '';

  // 扩展名到 MIME 类型的映射
  const extensionToMime: Record<string, string[]> = {
    'pdf': ['application/pdf'],
    'docx': ['application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
    'doc': ['application/msword', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
    'xlsx': ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'],
    'xls': ['application/vnd.ms-excel', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'],
    'txt': ['text/plain'],
    'csv': ['text/csv', 'text/plain'],
    'json': ['application/json', 'text/plain'],
    'png': ['image/png'],
    'jpg': ['image/jpeg'],
    'jpeg': ['image/jpeg'],
  };

  // 检查扩展名对应的 MIME 类型是否在允许列表中
  const mimeTypes = extensionToMime[extension];
  if (mimeTypes) {
    for (const mimeType of mimeTypes) {
      if (allowedTypes.includes(mimeType)) {
        return true;
      }
    }
  }

  return false;
}

/**
 * 检查文件大小是否在限制内
 *
 * @param file 文件对象
 * @param maxSize 最大大小（bytes）
 * @returns 是否符合限制
 */
export function isFileSizeValid(
  file: File,
  maxSize: number
): boolean {
  return file.size <= maxSize;
}

/**
 * 获取文件类型显示名称
 *
 * @param mimeType MIME 类型
 * @returns 显示名称
 */
export function getFileTypeName(mimeType: string): string {
  const typeNames: Record<string, string> = {
    'application/pdf': 'PDF',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'Word',
    'application/msword': 'Word',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'Excel',
    'application/vnd.ms-excel': 'Excel',
    'text/plain': 'Text',
    'text/csv': 'CSV',
    'application/json': 'JSON',
    'image/png': 'PNG',
    'image/jpeg': 'JPEG',
  };

  return typeNames[mimeType] || 'File';
}
