/**
 * 初始化引导卡片组件
 *
 * 在智能体环境初始化期间显示，提供进度反馈和可操作项提示。
 */

/**
 * @userGuide
 * @title.en Initialization Guide Card
 * @title.zh 初始化引导卡片
 * @category core
 * @description.en Displays during agent environment initialization with progress bar, estimated time, and actionable configuration items.
 * @description.zh 在智能体环境初始化期间显示，提供进度条、预估时间和可操作的配置项。
 * @steps.en
 *   1. Watch the progress bar as the environment initializes
 *   2. While waiting, configure available options (persona, MCP services)
 *   3. Wait for initialization to complete before starting chat
 * @steps.zh
 *   1. 观察进度条了解初始化进度
 *   2. 等待期间可配置可用选项（人设、MCP 服务）
 *   3. 等待初始化完成后开始对话
 * @tips.en
 *   - Configuration items become available progressively during initialization
 *   - You can set up persona and MCP services while waiting
 * @tips.zh
 *   - 配置项在初始化过程中逐步变为可用
 *   - 可以在等待期间设置人设和 MCP 服务
 * @related EnvironmentBanner
 */

import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { Loader2, Settings, User, Plug } from "lucide-react";
import { cn } from "@/lib/utils";
import { MockProgressCalculator } from "@/lib/progress";

interface InitializationGuideCardProps {
  agentName: string;
  locale?: "zh" | "en";
  estimatedDuration?: number; // 预估时长（秒），默认30
}

interface ActionableItem {
  icon: React.ReactNode;
  zhLabel: string;
  enLabel: string;
  zhDescription: string;
  enDescription: string;
  isAvailable: boolean; // 是否已可用
}

export function InitializationGuideCard({
  agentName,
  locale = "zh",
  estimatedDuration = 30,
}: InitializationGuideCardProps) {
  const [progress, setProgress] = useState(0);
  const [estimatedSeconds, setEstimatedSeconds] = useState(estimatedDuration);
  const [phase, setPhase] = useState<"early" | "middle" | "late">("early");

  // 模拟进度计算
  useEffect(() => {
    const calculator = new MockProgressCalculator(estimatedDuration);

    const interval = setInterval(() => {
      const state = calculator.getCurrentState();
      setProgress(state.progress);
      setEstimatedSeconds(state.estimatedSeconds);

      if (state.phase !== "complete") {
        setPhase(state.phase);
      }

      if (state.phase === "complete") {
        clearInterval(interval);
      }
    }, 200);

    return () => clearInterval(interval);
  }, [estimatedDuration]);

  // 可操作项列表
  const actionableItems: ActionableItem[] = [
    {
      icon: <Settings className="w-5 h-5" />,
      zhLabel: "配置模型服务",
      enLabel: "Configure Model Service",
      zhDescription: "选择合适的LLM模型以启用对话能力",
      enDescription: "Select an appropriate LLM model to enable chat",
      isAvailable: false, // 模拟：暂不可用
    },
    {
      icon: <User className="w-5 h-5" />,
      zhLabel: "设置人设",
      enLabel: "Set Persona",
      zhDescription: "定义智能体的角色和行为特征",
      enDescription: "Define the agent's role and behavior",
      isAvailable: true, // 模拟：立即可用
    },
    {
      icon: <Plug className="w-5 h-5" />,
      zhLabel: "添加MCP服务",
      enLabel: "Add MCP Services",
      zhDescription: "扩展智能体的工具能力",
      enDescription: "Extend agent capabilities with tools",
      isAvailable: true, // 模拟：立即可用
    },
  ];

  // 阶段描述
  const phaseTexts = {
    early: {
      zh: "正在准备运行环境...",
      en: "Preparing runtime environment...",
    },
    middle: {
      zh: "正在安装依赖包...",
      en: "Installing dependencies...",
    },
    late: {
      zh: "正在验证环境配置...",
      en: "Verifying environment configuration...",
    },
  };

  // 格式化剩余时间
  const formatTime = (seconds: number): string => {
    if (seconds < 60) {
      return locale === "zh" ? `${seconds}秒` : `${seconds}s`;
    }
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return locale === "zh"
      ? `${mins}分${secs}秒`
      : `${mins}m ${secs}s`;
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -10 }}
      className="w-full max-w-4xl mx-auto mb-4"
    >
      <div className="bg-gradient-to-r from-blue-50 to-indigo-50 dark:from-blue-950/30 dark:to-indigo-950/30 border border-blue-200 dark:border-blue-800 rounded-xl p-5 shadow-sm">
        {/* 标题栏 */}
        <div className="flex items-center gap-3 mb-4">
          <div className="relative">
            <Loader2 className="w-6 h-6 text-blue-600 dark:text-blue-400 animate-spin" />
            <div className="absolute inset-0 w-6 h-6 rounded-full border-2 border-blue-200 dark:border-blue-800 opacity-20 animate-ping" />
          </div>
          <div className="flex-1">
            <h3 className="text-sm font-semibold text-blue-900 dark:text-blue-100">
              {locale === "zh"
                ? `智能体「${agentName}」正在初始化`
                : `Initializing agent "${agentName}"`}
            </h3>
            <p className="text-xs text-blue-700 dark:text-blue-300 mt-0.5">
              {phaseTexts[phase][locale === "zh" ? "zh" : "en"]}
            </p>
          </div>
          <div className="text-right">
            <span className="text-xs text-blue-600 dark:text-blue-400 font-medium">
              {progress}%
            </span>
            <p className="text-xs text-blue-500 dark:text-blue-500">
              {locale === "zh" ? "剩余" : "EST"} {formatTime(estimatedSeconds)}
            </p>
          </div>
        </div>

        {/* 进度条 */}
        <div className="mb-4">
          <div className="h-2 bg-blue-100 dark:bg-blue-900/50 rounded-full overflow-hidden">
            <motion.div
              className="h-full bg-gradient-to-r from-blue-500 to-indigo-500 rounded-full"
              initial={{ width: 0 }}
              animate={{ width: `${progress}%` }}
              transition={{ duration: 0.3, ease: "easeOut" }}
            />
          </div>
        </div>

        {/* 期间可操作项 */}
        <div className="border-t border-blue-200 dark:border-blue-800 pt-3 mt-3">
          <p className="text-xs text-blue-600 dark:text-blue-400 mb-2 font-medium">
            {locale === "zh" ? "📋 初始化期间可配置：" : "📋 Configure while waiting:"}
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
            {actionableItems.map((item, index) => (
              <motion.div
                key={index}
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: index * 0.1 }}
                className={cn(
                  "flex items-start gap-2 p-2 rounded-lg transition-colors",
                  item.isAvailable
                    ? "bg-white/50 dark:bg-white/5 cursor-pointer hover:bg-white/80 dark:hover:bg-white/10"
                    : "bg-gray-100/50 dark:bg-gray-800/30 opacity-60 cursor-not-allowed"
                )}
              >
                <div className={cn(
                  "p-1.5 rounded-md flex-shrink-0",
                  item.isAvailable
                    ? "bg-blue-100 dark:bg-blue-900/50 text-blue-600 dark:text-blue-400"
                    : "bg-gray-200 dark:bg-gray-700/50 text-gray-500 dark:text-gray-500"
                )}>
                  {item.icon}
                </div>
                <div className="min-w-0 flex-1">
                  <p className={cn(
                    "text-xs font-medium",
                    item.isAvailable
                      ? "text-blue-900 dark:text-blue-100"
                      : "text-gray-600 dark:text-gray-400"
                  )}>
                    {locale === "zh" ? item.zhLabel : item.enLabel}
                  </p>
                  <p className="text-xs text-blue-600/70 dark:text-blue-400/70 truncate">
                    {locale === "zh" ? item.zhDescription : item.enDescription}
                  </p>
                </div>
                {item.isAvailable && (
                  <div className="flex items-center">
                    <span className="text-xs text-blue-500 dark:text-blue-500 font-medium">
                      {locale === "zh" ? "配置" : "Setup"}
                    </span>
                  </div>
                )}
              </motion.div>
            ))}
          </div>
        </div>
      </div>
    </motion.div>
  );
}

/**
 * 精简版引导卡片（用于空间受限场景）
 */
export function InitializationGuideCardSimple({
  progress = 0,
  estimatedSeconds = 30,
}: {
  locale?: "zh" | "en";
  progress?: number;
  estimatedSeconds?: number;
}) {
  return (
    <div className="w-full bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 rounded-lg px-4 py-3">
      <div className="flex items-center gap-3">
        <Loader2 className="w-4 h-4 text-blue-600 dark:text-blue-400 animate-spin flex-shrink-0" />
        <div className="flex-1">
          <div className="h-1.5 bg-blue-100 dark:bg-blue-900/50 rounded-full overflow-hidden">
            <div
              className="h-full bg-blue-500 rounded-full transition-all duration-300"
              style={{ width: `${Math.min(95, progress)}%` }}
            />
          </div>
        </div>
        <span className="text-xs text-blue-600 dark:text-blue-400 font-medium whitespace-nowrap">
          ~{estimatedSeconds}s
        </span>
      </div>
    </div>
  );
}
