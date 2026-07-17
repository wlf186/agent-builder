"use client";

/**
 * @userGuide
 * @title.en MCP Diagnostics
 * @title.zh MCP 诊断
 * @category advanced
 * @description.en Diagnose MCP service connection issues with multi-layer health checks. View detailed results for config, DNS, network, TLS, and MCP protocol layers.
 * @description.zh 通过多层健康检查诊断 MCP 服务连接问题。查看配置、DNS、网络、TLS 和 MCP 协议层的详细结果。
 * @steps.en
 *   1. Open MCP Service Configuration in edit mode
 *   2. Click the "Diagnose" button to start diagnostics
 *   3. View the overall status: Healthy, Degraded, or Down
 *   4. Check each layer result for detailed information
 *   5. Expand "Details" to see raw diagnostic data
 *   6. Follow the recommendation section for fix suggestions
 * @steps.zh
 *   1. 在编辑模式下打开 MCP 服务配置
 *   2. 点击"诊断"按钮开始诊断
 *   3. 查看整体状态：正常、降级或不可用
 *   4. 检查每层结果获取详细信息
 *   5. 展开"详细信息"查看原始诊断数据
 *   6. 参考修复建议部分获取解决方案
 * @tips.en
 *   - Each layer tests a specific aspect: config validation, DNS resolution, network connectivity, TLS/SSL, and MCP protocol
 *   - Latency is measured in milliseconds for each layer
 *   - Use recommendations to identify and fix common issues
 * @tips.zh
 *   - 每层测试特定方面：配置验证、DNS解析、网络连接、TLS/SSL 和 MCP 协议
 *   - 每层延迟以毫秒为单位测量
 *   - 使用修复建议识别和解决常见问题
 * @related MCPServiceDialog
 */

import { motion, AnimatePresence } from "framer-motion";
import { X, CheckCircle, XCircle, AlertCircle, Loader2, Activity } from "lucide-react";
import { useLocale } from "@/lib/LocaleContext";
import { apiPath } from "@/lib/apiPath";

/**
 * 单层诊断结果
 */
export interface DiagnosticLayer {
  layer: string;                    // 诊断层名称: config, dns, network, tls, mcp
  status: "pass" | "fail" | "skip"; // 通过/失败/跳过
  message: string;                  // 状态消息
  latency_ms?: number | null;       // 延迟（毫秒）
  details?: Record<string, unknown> | null; // 详细信息
}

/**
 * 完整诊断报告
 */
export interface MCPDiagnosticReport {
  service_name: string;
  overall_status: "healthy" | "degraded" | "down";
  timestamp: string;
  layers: DiagnosticLayer[];
  recommendation: string;           // 修复建议
}

interface MCPDiagnosticResultProps {
  isOpen: boolean;
  onClose: () => void;
  serviceName: string;
  report: MCPDiagnosticReport | null;
  isLoading: boolean;
}

/**
 * 获取诊断层的显示名称
 */
function getLayerDisplayName(layer: string, locale: string): string {
  const layerNames: Record<string, { zh: string; en: string }> = {
    config: { zh: "配置验证", en: "Config Validation" },
    dns: { zh: "DNS解析", en: "DNS Resolution" },
    network: { zh: "网络连接", en: "Network Connectivity" },
    tls: { zh: "TLS/SSL", en: "TLS/SSL" },
    http: { zh: "HTTP/SSE", en: "HTTP/SSE" },
    mcp: { zh: "MCP协议", en: "MCP Protocol" },
  };
  const layerConfig = layerNames[layer];
  if (!layerConfig) return layer;
  return locale === "zh" ? layerConfig.zh : layerConfig.en;
}

/**
 * 获取诊断层的图标
 */
function getLayerIcon(layer: string) {
  const icons: Record<string, React.ComponentType<{ className?: string }>> = {
    config: Activity,
    dns: Activity,
    network: Activity,
    tls: Activity,
    http: Activity,
    mcp: Activity,
  };
  return icons[layer] || Activity;
}

/**
 * 获取整体状态的颜色和图标
 */
function getOverallStatusConfig(status: MCPDiagnosticReport["overall_status"]) {
  switch (status) {
    case "healthy":
      return {
        color: "text-emerald-400",
        bgColor: "bg-emerald-500/10",
        borderColor: "border-emerald-500/20",
        icon: CheckCircle,
        label: { zh: "服务正常", en: "Healthy" }
      };
    case "degraded":
      return {
        color: "text-amber-400",
        bgColor: "bg-amber-500/10",
        borderColor: "border-amber-500/20",
        icon: AlertCircle,
        label: { zh: "服务降级", en: "Degraded" }
      };
    case "down":
      return {
        color: "text-red-400",
        bgColor: "bg-red-500/10",
        borderColor: "border-red-500/20",
        icon: XCircle,
        label: { zh: "服务不可用", en: "Down" }
      };
  }
}

/**
 * MCP诊断结果展示组件
 *
 * 功能：
 * - 分层展示诊断结果
 * - 显示每层的状态、消息、延迟
 * - 提供修复建议
 */
export function MCPDiagnosticResult({
  isOpen,
  onClose,
  serviceName,
  report,
  isLoading,
}: MCPDiagnosticResultProps) {
  const { locale } = useLocale();

  return (
    <AnimatePresence>
      {isOpen && (
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
            className="w-full max-w-2xl"
          >
            {/* 背景卡片 */}
            <div className="rounded-2xl border border-white/[0.08] bg-white/[0.03] backdrop-blur-2xl shadow-2xl overflow-hidden">
              {/* Header */}
              <div className="flex items-center justify-between px-6 py-4 border-b border-white/[0.08]">
                <div className="flex items-center gap-3">
                  <Activity className="w-5 h-5 text-emerald-400" />
                  <div>
                    <h2 className="text-lg font-semibold text-white">
                      {locale === "zh" ? "诊断结果" : "Diagnostic Result"}
                    </h2>
                    <p className="text-sm text-gray-400">{serviceName}</p>
                  </div>
                </div>
                <button
                  onClick={onClose}
                  className="text-gray-400 hover:text-white transition-colors p-1"
                >
                  <X size={20} />
                </button>
              </div>

              {/* Content */}
              <div className="p-6 max-h-[70vh] overflow-y-auto">
                {isLoading && (
                  <div className="flex flex-col items-center justify-center py-12">
                    <Loader2 className="w-10 h-10 text-emerald-400 animate-spin mb-4" />
                    <p className="text-gray-300">
                      {locale === "zh" ? "正在诊断..." : "Diagnosing..."}
                    </p>
                  </div>
                )}

                {!isLoading && report && (
                  <div className="space-y-4">
                    {/* Overall Status */}
                    <div className={`p-4 rounded-xl border ${getOverallStatusConfig(report.overall_status).bgColor} ${getOverallStatusConfig(report.overall_status).borderColor}`}>
                      <div className="flex items-center gap-3">
                        {(() => {
                          const StatusIcon = getOverallStatusConfig(report.overall_status).icon;
                          return <StatusIcon className={`w-6 h-6 ${getOverallStatusConfig(report.overall_status).color}`} />;
                        })()}
                        <div>
                          <p className={`font-medium ${getOverallStatusConfig(report.overall_status).color}`}>
                            {getOverallStatusConfig(report.overall_status).label[locale]}
                          </p>
                          <p className="text-xs text-gray-400">
                            {new Date(report.timestamp).toLocaleString(locale === "zh" ? "zh-CN" : "en-US")}
                          </p>
                        </div>
                      </div>
                    </div>

                    {/* Layer Results */}
                    <div className="space-y-2">
                      <p className="text-sm font-medium text-gray-300">
                        {locale === "zh" ? "诊断详情" : "Diagnostic Details"}
                      </p>
                      {report.layers.map((layerResult, index) => {
                        const LayerIcon = getLayerIcon(layerResult.layer);
                        const isPass = layerResult.status === "pass";
                        const isFail = layerResult.status === "fail";

                        return (
                          <motion.div
                            key={layerResult.layer}
                            initial={{ opacity: 0, x: -20 }}
                            animate={{ opacity: 1, x: 0 }}
                            transition={{ delay: index * 0.1 }}
                            className={`p-3 rounded-lg border ${
                              isPass
                                ? "bg-emerald-500/5 border-emerald-500/10"
                                : isFail
                                ? "bg-red-500/5 border-red-500/10"
                                : "bg-gray-500/5 border-gray-500/10"
                            }`}
                          >
                            <div className="flex items-start gap-3">
                              <LayerIcon className={`w-4 h-4 mt-0.5 ${isPass ? "text-emerald-400" : isFail ? "text-red-400" : "text-gray-400"}`} />
                              <div className="flex-1 min-w-0">
                                <div className="flex items-center justify-between gap-2">
                                  <p className="text-sm font-medium text-gray-200">
                                    {getLayerDisplayName(layerResult.layer, locale)}
                                  </p>
                                  <div className="flex items-center gap-2">
                                    {layerResult.latency_ms !== null && layerResult.latency_ms !== undefined && (
                                      <span className="text-xs text-gray-500">
                                        {layerResult.latency_ms}ms
                                      </span>
                                    )}
                                    {isPass ? (
                                      <CheckCircle className="w-4 h-4 text-emerald-400 flex-shrink-0" />
                                    ) : isFail ? (
                                      <XCircle className="w-4 h-4 text-red-400 flex-shrink-0" />
                                    ) : (
                                      <AlertCircle className="w-4 h-4 text-gray-400 flex-shrink-0" />
                                    )}
                                  </div>
                                </div>
                                <p className="text-sm text-gray-400 mt-1">{layerResult.message}</p>
                                {layerResult.details && Object.keys(layerResult.details).length > 0 && (
                                  <details className="mt-2">
                                    <summary className="text-xs text-gray-500 cursor-pointer hover:text-gray-300">
                                      {locale === "zh" ? "详细信息" : "Details"}
                                    </summary>
                                    <pre className="text-xs text-gray-500 mt-2 bg-black/20 p-2 rounded overflow-x-auto">
                                      {JSON.stringify(layerResult.details, null, 2)}
                                    </pre>
                                  </details>
                                )}
                              </div>
                            </div>
                          </motion.div>
                        );
                      })}
                    </div>

                    {/* Recommendation */}
                    {report.recommendation && (
                      <div className="mt-4 p-4 rounded-xl bg-blue-500/10 border border-blue-500/20">
                        <p className="text-sm font-medium text-blue-300 mb-1">
                          {locale === "zh" ? "修复建议" : "Recommendation"}
                        </p>
                        <p className="text-sm text-gray-300">{report.recommendation}</p>
                      </div>
                    )}
                  </div>
                )}

                {!isLoading && !report && (
                  <div className="text-center py-12 text-gray-400">
                    <AlertCircle className="w-12 h-12 mx-auto mb-4 text-gray-500" />
                    <p>{locale === "zh" ? "无诊断结果" : "No diagnostic results"}</p>
                  </div>
                )}
              </div>

              {/* Footer */}
              <div className="px-6 py-4 border-t border-white/[0.08] flex justify-end">
                <button
                  onClick={onClose}
                  className="px-4 py-2 rounded-xl border border-white/10 bg-transparent hover:bg-white/5 text-sm text-white transition-colors"
                >
                  {locale === "zh" ? "关闭" : "Close"}
                </button>
              </div>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/**
 * 调用MCP诊断API的辅助函数
 */
export async function diagnoseMCPService(serviceName: string): Promise<MCPDiagnosticReport> {
  const response = await fetch(apiPath('mcp-services', serviceName, 'diagnose'), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }

  return response.json();
}
