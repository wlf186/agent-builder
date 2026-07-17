/**
 * @userGuide
 * @title.en Environment Status Banner
 * @title.zh 环境状态横幅
 * @category reference
 * @description.en Displays the skill execution environment initialization status at the top of the page. Shows a loading indicator during initialization and an error message with retry button if initialization fails.
 * @description.zh 在页面顶部显示技能执行环境的初始化状态。初始化期间显示加载指示器，初始化失败时显示错误信息和重试按钮。
 * @tips.en
 *   - The banner automatically disappears when the environment is ready
 *   - Click the retry button to attempt environment initialization again
 *   - Skill features are unavailable during initialization (10-30 seconds)
 * @tips.zh
 *   - 环境就绪后横幅会自动消失
 *   - 点击重试按钮可以重新尝试环境初始化
 *   - 初始化期间（10-30秒）技能功能暂时不可用
 */
import { Loader2, AlertCircle, RefreshCw } from 'lucide-react';
import { useEnvironmentStatus } from '@/hooks/useEnvironmentStatus';
import { cn } from '@/lib/utils';

interface EnvironmentBannerProps {
  agentName: string;
  enabled?: boolean;
  locale?: 'zh' | 'en';
}

export function EnvironmentBanner({ agentName, enabled = true, locale = 'zh' }: EnvironmentBannerProps) {
  const { state, isPolling, isReady, isCreating, isError, retry } = useEnvironmentStatus({
    agentName,
    enabled
  });

  // 就绪或未找到时不显示横幅
  if (isReady || state.status === 'not_found') {
    return null;
  }

  const texts = {
    creating: {
      title: locale === 'zh' ? 'Skill执行环境正在初始化' : 'Initializing Skill Execution Environment',
      description: locale === 'zh'
        ? '预计需要 10-30 秒，期间 Skill 相关功能暂时不可用'
        : 'Estimated 10-30 seconds, Skill features will be unavailable during this time',
    },
    error: {
      title: locale === 'zh' ? '环境初始化失败' : 'Environment Initialization Failed',
      retry: locale === 'zh' ? '重试' : 'Retry',
    }
  };

  return (
    <div className={cn(
      "fixed top-0 left-0 right-0 z-50 px-4 py-3 transition-all duration-300",
      isCreating ? 'bg-blue-50 dark:bg-blue-950/50 border-b border-blue-200 dark:border-blue-800' :
      'bg-red-50 dark:bg-red-950/50 border-b border-red-200 dark:border-red-800'
    )}>
      <div className="max-w-4xl mx-auto flex items-center justify-between gap-4">
        <div className="flex items-center gap-3 flex-1">
          {isCreating && (
            <>
              <Loader2 className="w-5 h-5 text-blue-600 dark:text-blue-400 animate-spin flex-shrink-0" />
              <div>
                <p className="text-sm font-medium text-blue-900 dark:text-blue-100">
                  {texts.creating.title}
                </p>
                <p className="text-xs text-blue-700 dark:text-blue-300 mt-0.5">
                  {texts.creating.description}
                </p>
              </div>
            </>
          )}

          {isError && (
            <>
              <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400 flex-shrink-0" />
              <div className="flex-1">
                <p className="text-sm font-medium text-red-900 dark:text-red-100">
                  {texts.error.title}
                </p>
                {state.errorMessage && (
                  <p className="text-xs text-red-700 dark:text-red-300 mt-0.5">
                    {state.errorMessage}
                  </p>
                )}
              </div>
            </>
          )}
        </div>

        {isError && (
          <button
            onClick={retry}
            disabled={isPolling}
            className="flex items-center gap-2 px-3 py-1.5 text-sm font-medium text-red-700 dark:text-red-300 bg-red-100 dark:bg-red-900/50 hover:bg-red-200 dark:hover:bg-red-900 rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex-shrink-0"
          >
            <RefreshCw className={cn("w-4 h-4", isPolling && "animate-spin")} />
            {texts.error.retry}
          </button>
        )}
      </div>
    </div>
  );
}

// 导出一个不带自动轮询的纯展示组件版本
interface EnvironmentBannerPropsSimple {
  status: 'creating' | 'ready' | 'error';
  errorMessage?: string;
  locale?: 'zh' | 'en';
}

export function EnvironmentBannerSimple({ status, errorMessage, locale = 'zh' }: EnvironmentBannerPropsSimple) {
  if (status === 'ready') {
    return null;
  }

  const texts = {
    creating: {
      title: locale === 'zh' ? 'Skill执行环境正在初始化' : 'Initializing Skill Execution Environment',
      description: locale === 'zh'
        ? '预计需要 10-30 秒，期间 Skill 相关功能暂时不可用'
        : 'Estimated 10-30 seconds, Skill features will be unavailable',
    },
    error: {
      title: locale === 'zh' ? '环境初始化失败' : 'Environment Initialization Failed',
      unknown: locale === 'zh' ? '未知错误' : 'Unknown error',
    }
  };

  const isError = status === 'error';
  const isCreating = status === 'creating';

  return (
    <div className={cn(
      "fixed top-0 left-0 right-0 z-50 px-4 py-3 transition-all duration-300",
      isCreating ? 'bg-blue-50 dark:bg-blue-950/50 border-b border-blue-200 dark:border-blue-800' :
      'bg-red-50 dark:bg-red-950/50 border-b border-red-200 dark:border-red-800'
    )}>
      <div className="max-w-4xl mx-auto flex items-center justify-between gap-4">
        <div className="flex items-center gap-3 flex-1">
          {isCreating && (
            <>
              <Loader2 className="w-5 h-5 text-blue-600 dark:text-blue-400 animate-spin flex-shrink-0" />
              <div>
                <p className="text-sm font-medium text-blue-900 dark:text-blue-100">
                  {texts.creating.title}
                </p>
                <p className="text-xs text-blue-700 dark:text-blue-300 mt-0.5">
                  {texts.creating.description}
                </p>
              </div>
            </>
          )}

          {isError && (
            <>
              <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400 flex-shrink-0" />
              <div className="flex-1">
                <p className="text-sm font-medium text-red-900 dark:text-red-100">
                  {texts.error.title}
                </p>
                <p className="text-xs text-red-700 dark:text-red-300 mt-0.5">
                  {errorMessage || texts.error.unknown}
                </p>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
