/**
 * 环境就绪通知组件
 *
 * 当智能体环境初始化完成时显示，提供"开始调试"按钮引导用户进入对话。
 */
import { motion, AnimatePresence } from "framer-motion";
import { CheckCircle, Play, X } from "lucide-react";
import { Button } from "@/components/ui/button";

interface EnvironmentReadyNotificationProps {
  agentName: string;
  locale?: "zh" | "en";
  onStartDebug?: () => void;
  onDismiss?: () => void;
}

export function EnvironmentReadyNotification({
  agentName,
  locale = "zh",
  onStartDebug,
  onDismiss,
}: EnvironmentReadyNotificationProps) {
  const handleStartDebug = () => {
    // 滚动到调试对话区域
    const chatCard = document.querySelector('[data-chat-card="true"]');
    if (chatCard) {
      chatCard.scrollIntoView({ behavior: "smooth", block: "center" });
    }

    // 聚焦到输入框（尝试多种选择器）
    setTimeout(() => {
      const inputSelectors = [
        'textarea[placeholder*="消息"]',
        'textarea[placeholder*="message"]',
        'textarea[placeholder*="Message"]',
        'input[type="text"]',
        '.chat-input',
      ];

      for (const selector of inputSelectors) {
        const input = document.querySelector(selector) as HTMLInputElement | HTMLTextAreaElement;
        if (input) {
          input.focus();
          break;
        }
      }
    }, 300);

    onStartDebug?.();
  };

  const handleDismiss = () => {
    onDismiss?.();
  };

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: -20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: -20 }}
        transition={{ type: "spring", stiffness: 300, damping: 25 }}
        className="fixed top-20 left-1/2 -translate-x-1/2 z-40"
      >
        <div className="bg-gradient-to-r from-emerald-50 to-teal-50 dark:from-emerald-950/50 dark:to-teal-950/50 border border-emerald-200 dark:border-emerald-800 rounded-2xl shadow-2xl shadow-emerald-500/10 px-6 py-4 flex items-center gap-4 max-w-md">
          {/* 成功图标 */}
          <div className="relative">
            <motion.div
              initial={{ scale: 0 }}
              animate={{ scale: 1 }}
              transition={{ type: "spring", delay: 0.1, stiffness: 200 }}
              className="w-12 h-12 rounded-full bg-emerald-100 dark:bg-emerald-900/50 flex items-center justify-center"
            >
              <CheckCircle className="w-6 h-6 text-emerald-600 dark:text-emerald-400" />
            </motion.div>
            <motion.div
              initial={{ scale: 0, opacity: 0 }}
              animate={{ scale: 1, opacity: 0 }}
              exit={{ scale: 1.5, opacity: 0 }}
              transition={{ duration: 0.5 }}
              className="absolute inset-0 w-12 h-12 rounded-full bg-emerald-400/30"
            />
          </div>

          {/* 文本内容 */}
          <div className="flex-1 min-w-0">
            <p className="text-sm font-semibold text-emerald-900 dark:text-emerald-100">
              {locale === "zh"
                ? `「${agentName}」环境就绪！`
                : `"${agentName}" is ready!`}
            </p>
            <p className="text-xs text-emerald-700 dark:text-emerald-300 mt-0.5">
              {locale === "zh"
                ? "现在可以开始调试对话了"
                : "Ready to start debugging"}
            </p>
          </div>

          {/* 操作按钮 */}
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              onClick={handleStartDebug}
              className="bg-emerald-600 hover:bg-emerald-700 text-white gap-1.5 shadow-lg shadow-emerald-500/30"
            >
              <Play size={14} />
              {locale === "zh" ? "开始调试" : "Start"}
            </Button>
            <button
              onClick={handleDismiss}
              className="p-1.5 rounded-lg hover:bg-emerald-100 dark:hover:bg-emerald-900/50 text-emerald-600 dark:text-emerald-400 transition-colors"
              aria-label={locale === "zh" ? "关闭" : "Dismiss"}
            >
              <X size={16} />
            </button>
          </div>
        </div>
      </motion.div>
    </AnimatePresence>
  );
}

/**
 * 精简版通知（用于空间受限场景）
 */
export function EnvironmentReadyNotificationSimple({
  locale = "zh",
  onDismiss,
}: {
  locale?: "zh" | "en";
  onDismiss?: () => void;
}) {
  return (
    <div className="bg-emerald-50 dark:bg-emerald-950/30 border border-emerald-200 dark:border-emerald-800 rounded-lg px-4 py-3 flex items-center gap-3">
      <CheckCircle className="w-5 h-5 text-emerald-600 dark:text-emerald-400 flex-shrink-0" />
      <div className="flex-1">
        <p className="text-sm font-medium text-emerald-900 dark:text-emerald-100">
          {locale === "zh" ? "环境已就绪" : "Environment ready"}
        </p>
      </div>
      {onDismiss && (
        <button
          onClick={onDismiss}
          className="p-1 rounded-md hover:bg-emerald-100 dark:hover:bg-emerald-900/50 text-emerald-600 dark:text-emerald-400"
        >
          <X size={14} />
        </button>
      )}
    </div>
  );
}
