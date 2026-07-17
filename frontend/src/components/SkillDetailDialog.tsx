"use client";

/**
 * @userGuide
 * @title.en Skill Details
 * @title.zh 技能详情
 * @category advanced
 * @description.en View detailed information about a skill including its files, documentation, and metadata. Browse skill content with a file tree navigation.
 * @description.zh 查看技能的详细信息，包括文件列表、文档和元数据。通过文件树导航浏览技能内容。
 * @steps.en
 *   1. Click on a skill card to open the detail dialog
 *   2. View skill name, description, and source (Official/Custom)
 *   3. Browse the file list on the left panel
 *   4. Click on any file to view its content
 *   5. Markdown files are auto-selected for initial display
 *   6. Check version and author information in the footer
 * @steps.zh
 *   1. 点击技能卡片打开详情对话框
 *   2. 查看技能名称、描述和来源（官方/自定义）
 *   3. 浏览左侧面板的文件列表
 *   4. 点击任意文件查看其内容
 *   5. Markdown 文件会自动选中并优先展示
 *   6. 在底部查看版本和作者信息
 * @tips.en
 *   - SKILL.md is the main documentation file for each skill
 *   - Official skills are built-in and maintained by the platform
 *   - Custom skills are user-uploaded packages
 * @tips.zh
 *   - SKILL.md 是每个技能的主要文档文件
 *   - 官方技能是平台内置和维护的
 *   - 自定义技能是用户上传的技能包
 * @related SkillUploadDialog
 */

import { useState, useEffect, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  X,
  FileText,
  Code,
  File,
} from "lucide-react";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { useLocale } from "@/lib/LocaleContext";
import { apiPath } from "@/lib/apiPath";

interface SkillDetail {
  name: string;
  description: string;
  source: string;
  version: string;
  author?: string;
  tags: string[];
  files: string[];
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

interface SkillFileContent {
  content: string;
  file_type: string;
  filepath: string;
}

interface SkillDetailDialogProps {
  isOpen: boolean;
  onClose: () => void;
  skillName: string | null;
}

export function SkillDetailDialog({
  isOpen,
  onClose,
  skillName,
}: SkillDetailDialogProps) {
  const { locale } = useLocale();
  const [skill, setSkill] = useState<SkillDetail | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<SkillFileContent | null>(null);
  const [isLoading, setIsLoading] = useState(() => Boolean(isOpen && skillName));

  const fetchSkillDetail = useCallback(async (): Promise<SkillDetail | null> => {
    if (!skillName) return null;
    try {
      const res = await fetch(apiPath('skills', skillName));
      if (res.ok) {
        return await res.json() as SkillDetail;
      }
    } catch (e) {
      console.error("Failed to load skill detail:", e);
    }
    return null;
  }, [skillName]);

  const fetchFileContent = useCallback(async (filepath: string): Promise<SkillFileContent | null> => {
    if (!skillName) return null;
    try {
      const res = await fetch(apiPath('skills', skillName, 'files', filepath));
      if (res.ok) {
        return await res.json() as SkillFileContent;
      }
    } catch (e) {
      console.error("Failed to load file content:", e);
    }
    return null;
  }, [skillName]);

  useEffect(() => {
    if (!isOpen || !skillName) return;
    let cancelled = false;
    void fetchSkillDetail().then((data) => {
      if (cancelled) return;
      setSkill(data);
      setIsLoading(false);
      if (!data) return;

      // Auto-select first md file
      const mdFile = data.files.find((file) => file.endsWith('.md'));
      setSelectedFile(mdFile ?? data.files[0] ?? null);
    });
    return () => {
      cancelled = true;
    };
  }, [isOpen, skillName, fetchSkillDetail]);

  useEffect(() => {
    if (!selectedFile || !skillName) return;
    let cancelled = false;
    void fetchFileContent(selectedFile).then((data) => {
      if (!cancelled) setFileContent(data);
    });
    return () => {
      cancelled = true;
    };
  }, [selectedFile, skillName, fetchFileContent]);

  const getFileIcon = (filename: string) => {
    const ext = filename.split('.').pop()?.toLowerCase();
    switch (ext) {
      case 'md':
        return <FileText size={14} className="text-blue-400" />;
      case 'py':
        return <Code size={14} className="text-green-400" />;
      case 'js':
      case 'ts':
      case 'jsx':
      case 'tsx':
        return <Code size={14} className="text-yellow-400" />;
      default:
        return <File size={14} className="text-gray-400" />;
    }
  };

  if (!isOpen || !skillName) return null;

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
        onClick={onClose}
      >
        <motion.div
          initial={{ scale: 0.95, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.95, opacity: 0 }}
          onClick={(e) => e.stopPropagation()}
          className="w-full max-w-4xl max-h-[80vh] bg-[#1a1a2e] rounded-2xl shadow-2xl overflow-hidden border border-white/10"
        >
          {/* Header */}
          <div className="px-6 py-4 border-b border-white/10 flex items-center justify-between bg-white/[0.02]">
            <div>
              <h2 className="text-lg font-semibold text-white">
                {skill?.name || skillName}
              </h2>
              <p className="text-sm text-gray-400 mt-0.5">
                {skill?.description || ""}
              </p>
            </div>
            <div className="flex items-center gap-3">
              <span
                className={cn(
                  "text-xs px-2.5 py-1 rounded-lg",
                  skill?.source === "builtin"
                    ? "bg-blue-500/20 text-blue-400"
                    : "bg-purple-500/20 text-purple-400"
                )}
              >
                {skill?.source === "builtin"
                  ? locale === "zh"
                    ? "官方"
                    : "Official"
                  : locale === "zh"
                  ? "自定义"
                  : "Custom"}
              </span>
              <button
                onClick={onClose}
                className="p-2 rounded-lg hover:bg-white/10 transition-colors text-gray-400 hover:text-white"
              >
                <X size={20} />
              </button>
            </div>
          </div>

          {/* Content */}
          <div className="flex h-[calc(80vh-80px)]">
            {/* Left: File Tree */}
            <div className="w-64 border-r border-white/10 p-4 overflow-auto">
              <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">
                {locale === "zh" ? "文件列表" : "Files"}
              </div>
              {isLoading ? (
                <div className="text-sm text-gray-500">
                  {locale === "zh" ? "加载中..." : "Loading..."}
                </div>
              ) : (
                <div className="space-y-1">
                  {skill?.files.map((file) => (
                    <button
                      key={file}
                      onClick={() => setSelectedFile(file)}
                      className={cn(
                        "w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm transition-colors text-left",
                        selectedFile === file
                          ? "bg-blue-500/20 text-blue-400"
                          : "hover:bg-white/5 text-gray-300"
                      )}
                    >
                      {getFileIcon(file)}
                      <span className="truncate">{file}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* Right: File Content */}
            <div className="flex-1 p-4 overflow-auto">
              <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">
                {locale === "zh" ? "内容预览" : "Content Preview"}
              </div>
              {fileContent ? (
                <Card className="p-4 bg-white/[0.02] border-white/10">
                  <pre className="text-sm text-gray-300 whitespace-pre-wrap font-mono overflow-auto max-h-[60vh]">
                    {fileContent.content}
                  </pre>
                </Card>
              ) : (
                <div className="text-sm text-gray-500 flex items-center justify-center h-32">
                  {locale === "zh"
                    ? "选择文件查看内容"
                    : "Select a file to view content"}
                </div>
              )}
            </div>
          </div>

          {/* Footer */}
          <div className="px-6 py-4 border-t border-white/10 flex items-center justify-between bg-white/[0.02]">
            <div className="flex items-center gap-4 text-xs text-gray-500">
              {skill?.version && (
                <span>v{skill.version}</span>
              )}
              {skill?.author && (
                <span>
                  {locale === "zh" ? "作者" : "Author"}: {skill.author}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              {skill?.tags && skill.tags.length > 0 && (
                <div className="flex items-center gap-1.5">
                  {skill.tags.map((tag) => (
                    <span
                      key={tag}
                      className="text-xs px-2 py-0.5 rounded bg-white/10 text-gray-400"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
