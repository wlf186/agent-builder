/**
 * 环境状态轮询Hook
 *
 * 轮询智能体的环境创建状态，当环境就绪或失败时停止。
 */
import { useState, useEffect, useRef, useCallback } from 'react';
import { apiPath } from '@/lib/apiPath';

export type EnvironmentStatus = 'not_found' | 'creating' | 'ready' | 'error';

export interface EnvironmentState {
  status: EnvironmentStatus;
  agentName: string;
  createdAt?: string;
  errorMessage?: string;
}

interface UseEnvironmentStatusOptions {
  agentName: string;
  pollInterval?: number;    // 轮询间隔(ms)，默认2000
  maxAttempts?: number;     // 最大轮询次数，默认30
  enabled?: boolean;        // 是否启用轮询，默认true
  autoStart?: boolean;      // 是否自动开始轮询，默认true
}

export function useEnvironmentStatus({
  agentName,
  pollInterval = 2000,
  maxAttempts = 30,
  enabled = true,
  autoStart = true,
}: UseEnvironmentStatusOptions) {
  const [state, setState] = useState<EnvironmentState>({
    status: 'not_found',
    agentName
  });
  const [isPolling, setIsPolling] = useState(false);
  const attemptsRef = useRef(0);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const isMountedRef = useRef(true);
  // 使用ref跟踪轮询状态，避免依赖isPolling导致无限循环
  const isPollingRef = useRef(false);

  // 停止轮询
  const stopPolling = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    isPollingRef.current = false;
    setIsPolling(false);
  }, []);

  // 轮询环境状态
  const pollStatus = useCallback(async () => {
    if (!isMountedRef.current || !agentName) {
      return false;
    }

    try {
      const response = await fetch(apiPath('agents', agentName, 'environment'));
      if (response.ok) {
        const data = await response.json();

        // 处理两种响应格式
        const environment = data.environment || data;
        const newStatus: EnvironmentStatus = environment.status || 'not_found';

        setState({
          status: newStatus,
          agentName: environment.agent_name || agentName,
          createdAt: environment.created_at,
          errorMessage: environment.error_message
        });

        // 检查是否应该停止轮询
        // ready: 环境就绪；error: 环境创建失败；not_found: 旧智能体无环境（无需等待）
        if (newStatus === 'ready' || newStatus === 'error' || newStatus === 'not_found') {
          stopPolling();
          return false; // 停止轮询
        }
      }
    } catch (error) {
      console.error('Failed to poll environment status:', error);
      // 网络错误不停止轮询，继续尝试
    }

    attemptsRef.current += 1;
    if (attemptsRef.current >= maxAttempts) {
      // 超过最大轮询次数，标记为错误
      setState(prev => ({
        ...prev,
        status: 'error',
        errorMessage: '环境创建超时，请重试'
      }));
      stopPolling();
      return false; // 停止轮询
    }

    return true; // 继续轮询
  }, [agentName, maxAttempts, stopPolling]);

  // 开始轮询
  const startPolling = useCallback(() => {
    if (!enabled || !agentName || isPollingRef.current) {
      return;
    }

    isPollingRef.current = true;
    attemptsRef.current = 0;
    setIsPolling(true);

    // 立即执行一次
    pollStatus().then(shouldContinue => {
      if (!isMountedRef.current) return;

      if (shouldContinue) {
        // 设置定时器继续轮询
        intervalRef.current = setInterval(() => {
          pollStatus().then(shouldContinue => {
            if (!shouldContinue && intervalRef.current) {
              clearInterval(intervalRef.current);
              intervalRef.current = null;
            }
          });
        }, pollInterval);
      }
    });
  }, [agentName, enabled, pollInterval, pollStatus]);

  // 重试函数
  const retry = useCallback(async () => {
    if (!agentName) return;

    try {
      const response = await fetch(apiPath('agents', agentName, 'environment', 'retry'), {
        method: 'POST'
      });
      if (response.ok) {
        // 重置状态并重新开始轮询
        setState({ status: 'creating', agentName });
        attemptsRef.current = 0;
        startPolling();
      } else {
        const data = await response.json();
        setState(prev => ({
          ...prev,
          status: 'error',
          errorMessage: data.detail || '重试失败'
        }));
      }
    } catch (error) {
      console.error('Failed to retry environment creation:', error);
      setState(prev => ({
        ...prev,
        status: 'error',
        errorMessage: '网络错误，请重试'
      }));
    }
  }, [agentName, startPolling]);

  // 自动开始轮询
  useEffect(() => {
    isMountedRef.current = true;

    if (autoStart && enabled && agentName) {
      startPolling();
    }

    return () => {
      isMountedRef.current = false;
      stopPolling();
    };
  }, [autoStart, enabled, agentName, startPolling, stopPolling]);

  return {
    state,
    isPolling,
    isReady: state.status === 'ready',
    isCreating: state.status === 'creating',
    isError: state.status === 'error',
    isNotFound: state.status === 'not_found',
    startPolling,
    stopPolling,
    retry,
  };
}
