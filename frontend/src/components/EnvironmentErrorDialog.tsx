"use client";

/**
 * @userGuide
 * @title.en Environment Error Dialog
 * @title.zh 环境错误对话框
 * @category reference
 * @description.en Displays detailed error information when environment initialization fails. Shows problem description, recommended solutions with step-by-step instructions, and expandable technical details for debugging.
 * @description.zh 环境初始化失败时显示详细错误信息。展示问题描述、带有分步说明的推荐解决方案，以及可展开的技术详情用于调试。
 * @tips.en
 *   - Solutions include copyable terminal commands
 *   - Technical details section can be expanded for debugging
 *   - Links to external documentation are provided
 * @tips.zh
 *   - 解决方案包含可复制的终端命令
 *   - 技术详情部分可展开查看用于调试
 *   - 提供外部文档链接
 */
import { useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  Copy,
  Check,
  ExternalLink,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EnvironmentError } from "@/lib/systemApi";

interface EnvironmentErrorDialogProps {
  isOpen: boolean;
  onClose: () => void;
  error: EnvironmentError | null;
}

/**
 * 环境错误详情弹窗组件
 *
 * 展示结构化的错误信息，包括：
 * - 问题描述
 * - 推荐解决方案
 * - 技术详情（可折叠）
 */
export function EnvironmentErrorDialog({
  isOpen,
  onClose,
  error,
}: EnvironmentErrorDialogProps) {
  const [showTechnical, setShowTechnical] = useState(false);
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);

  if (!error) return null;

  const handleCopyCommands = async (commands: string[], index: number) => {
    const text = commands.join("\n");
    await navigator.clipboard.writeText(text);
    setCopiedIndex(index);
    setTimeout(() => setCopiedIndex(null), 2000);
  };

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-5 w-5 text-orange-500" />
            <DialogTitle>环境初始化失败</DialogTitle>
          </div>
          <DialogDescription className="pt-2">
            系统无法创建智能体运行环境，请参考以下解决方案
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 mt-4">
          {/* 问题描述 */}
          <div className="bg-orange-50 dark:bg-orange-950/20 border border-orange-200 dark:border-orange-800 rounded-lg p-4">
            <h3 className="font-medium text-orange-900 dark:text-orange-100 mb-2">
              问题描述
            </h3>
            <p className="text-sm text-orange-800 dark:text-orange-200">
              {error.user_message}
            </p>
          </div>

          {/* 解决方案 */}
          <div>
            <h3 className="font-medium text-gray-900 dark:text-gray-100 mb-3">
              推荐解决方案
            </h3>
            <div className="space-y-3">
              {error.solutions.map((solution, idx) => (
                <div
                  key={idx}
                  className="border border-gray-200 dark:border-gray-700 rounded-lg p-4 hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors"
                >
                  <div className="flex items-start justify-between mb-3">
                    <h4 className="font-medium text-gray-900 dark:text-gray-100">
                      {idx + 1}. {solution.title}
                    </h4>
                    <span className="text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">
                      预计 {solution.estimated_time}
                    </span>
                  </div>

                  {/* 步骤列表 */}
                  <ol className="text-sm text-gray-700 dark:text-gray-300 space-y-1.5 mb-3">
                    {solution.steps.map((step, stepIdx) => (
                      <li key={stepIdx} className="flex gap-2">
                        <span className="text-gray-400 dark:text-gray-500 shrink-0">
                          {stepIdx + 1}.
                        </span>
                        <span>{step}</span>
                      </li>
                    ))}
                  </ol>

                  {/* 命令复制 */}
                  {solution.commands && solution.commands.length > 0 && (
                    <div className="mt-3">
                      <div className="bg-gray-900 dark:bg-gray-950 rounded-lg p-3 font-mono text-xs text-gray-100">
                        {solution.commands.map((cmd, cmdIdx) => (
                          <div key={cmdIdx} className="flex items-center justify-between gap-2">
                            <span className="text-green-400">$</span>
                            <span className="flex-1 overflow-x-auto">{cmd}</span>
                          </div>
                        ))}
                      </div>
                      <Button
                        variant="outline"
                        size="sm"
                        className="mt-2 w-full"
                        onClick={() => handleCopyCommands(solution.commands!, idx)}
                      >
                        {copiedIndex === idx ? (
                          <>
                            <Check className="h-4 w-4 mr-2" />
                            已复制
                          </>
                        ) : (
                          <>
                            <Copy className="h-4 w-4 mr-2" />
                            复制命令
                          </>
                        )}
                      </Button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>

          {/* 技术详情（可折叠） */}
          {error.technical_details && (
            <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
              <button
                onClick={() => setShowTechnical(!showTechnical)}
                className="w-full px-4 py-3 flex items-center justify-between text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors"
              >
                <span>技术详情</span>
                {showTechnical ? (
                  <ChevronUp className="h-4 w-4" />
                ) : (
                  <ChevronDown className="h-4 w-4" />
                )}
              </button>
              {showTechnical && (
                <div className="px-4 pb-4">
                  <div className="bg-gray-900 dark:bg-gray-950 rounded-lg p-3 font-mono text-xs text-gray-100 overflow-x-auto">
                    <div className="text-red-400 mb-2">
                      {error.technical_details.error_message}
                    </div>
                    {error.technical_details.stack_trace && (
                      <pre className="text-gray-400 whitespace-pre-wrap">
                        {error.technical_details.stack_trace}
                      </pre>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* 操作按钮 */}
          <div className="flex flex-col sm:flex-row gap-2 pt-2">
            <Button
              variant="outline"
              className="flex-1"
              onClick={() => {
                window.open("/docs/zh/help/troubleshooting", "_blank");
              }}
            >
              <ExternalLink className="h-4 w-4 mr-2" />
              查看项目故障排除指南
            </Button>
            <Button
              className="flex-1"
              onClick={onClose}
            >
              我知道了
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
