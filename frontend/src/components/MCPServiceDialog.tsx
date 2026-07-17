"use client";

/**
 * @userGuide
 * @title.en MCP Service Configuration
 * @title.zh MCP 服务配置
 * @category advanced
 * @description.en Configure MCP (Model Context Protocol) services to extend agent capabilities with external tools. Supports SSE connections with optional authentication.
 * @description.zh 配置 MCP（模型上下文协议）服务以扩展智能体的外部工具能力。支持 SSE 连接和可选认证。
 * @steps.en
 *   1. Click "Add MCP Service" to open the configuration dialog
 *   2. Enter a unique service name (e.g., weather-api)
 *   3. Enter the MCP service URL endpoint
 *   4. Select authentication type: None, Bearer Token, or API Key
 *   5. If authentication is required, enter the credentials
 *   6. Optionally add custom headers in JSON format
 *   7. Toggle the enable switch and click "Save"
 *   8. Use "Diagnose" button (edit mode) to check connection health
 * @steps.zh
 *   1. 点击"添加 MCP 服务"打开配置对话框
 *   2. 输入唯一的服务名称（如：weather-api）
 *   3. 输入 MCP 服务 URL 端点
 *   4. 选择认证类型：无认证、Bearer Token 或 API Key
 *   5. 如需认证，输入凭据信息
 *   6. 可选添加 JSON 格式的自定义请求头
 *   7. 切换启用开关并点击"保存"
 *   8. 使用"诊断"按钮（编辑模式）检查连接健康状态
 * @tips.en
 *   - MCP services use SSE (Server-Sent Events) for real-time communication
 *   - Use the diagnostic tool to troubleshoot connection issues
 *   - Custom headers must be valid JSON format
 * @tips.zh
 *   - MCP 服务使用 SSE（服务器推送事件）进行实时通信
 *   - 使用诊断工具排查连接问题
 *   - 自定义请求头必须是有效的 JSON 格式
 * @related MCPDiagnosticResult
 */

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, Loader2, Server, Link, Key, Settings, Activity, AlertCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Card } from "@/components/ui/card";
import { useLocale } from "@/lib/LocaleContext";
import { MCPDiagnosticResult, diagnoseMCPService, MCPDiagnosticReport } from "@/components/MCPDiagnosticResult";
import { apiPath } from "@/lib/apiPath";

interface MCPService {
  name: string;
  description: string;
  connection_type: string;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
  url?: string;
  auth_type?: string;
  headers?: Record<string, string>;
}

interface MCPServiceDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: () => void;
  service?: MCPService | null; // 如果有值则为编辑模式
}

export function MCPServiceDialog({ isOpen, onClose, onSave, service }: MCPServiceDialogProps) {
  const { locale } = useLocale();
  const isEdit = !!service;

  const [name, setName] = useState(() => service?.name ?? "");
  const [description, setDescription] = useState(() => service?.description ?? "");
  const [enabled, setEnabled] = useState(() => service?.enabled ?? true);

  // SSE 配置 (唯一支持的连接方式)
  const [url, setUrl] = useState(() => service?.url ?? "");
  const [authType, setAuthType] = useState<"none" | "bearer" | "apikey">(
    () => (service?.auth_type as "none" | "bearer" | "apikey" | undefined) ?? "none",
  );
  const [authValue, setAuthValue] = useState("");
  const [headers, setHeaders] = useState(() => JSON.stringify(service?.headers ?? {}, null, 2));

  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");

  // MCP诊断相关状态
  const [showDiagnostic, setShowDiagnostic] = useState(false);
  const [isDiagnosing, setIsDiagnosing] = useState(false);
  const [diagnosticReport, setDiagnosticReport] = useState<MCPDiagnosticReport | null>(null);
  const [diagnosticError, setDiagnosticError] = useState<string | null>(null);

  const handleSave = async () => {
    if (!name.trim()) {
      setError(locale === "zh" ? "请输入服务名称" : "Service name is required");
      return;
    }

    if (!url.trim()) {
      setError(locale === "zh" ? "请输入服务URL" : "URL is required");
      return;
    }

    // 验证 JSON 格式
    let headersObj = {};
    try {
      headersObj = headers.trim() ? JSON.parse(headers) : {};
    } catch {
      setError(locale === "zh" ? "请求头JSON格式错误" : "Invalid JSON format for headers");
      return;
    }

    setIsLoading(true);
    setError("");

    try {
      const payload = {
        name: name.trim(),
        description,
        connection_type: "sse",  // 只支持 SSE
        enabled,
        url: url,
        auth_type: authType,
        auth_value: authValue || null,
        headers: headersObj,
      };

      const urlPath = isEdit ? apiPath('mcp-services', service.name) : apiPath('mcp-services');
      const method = isEdit ? "PUT" : "POST";

      const res = await fetch(urlPath, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      const data = await res.json();
      if (res.ok) {
        onSave();
        onClose();
      } else {
        setError(data.detail || (locale === "zh" ? "保存失败" : "Save failed"));
      }
    } catch {
      setError(locale === "zh" ? "网络错误" : "Network error");
    } finally {
      setIsLoading(false);
    }
  };

  // MCP诊断处理
  const handleDiagnose = async () => {
    if (!name.trim()) {
      setError(locale === "zh" ? "请先输入服务名称" : "Please enter service name first");
      return;
    }

    setIsDiagnosing(true);
    setDiagnosticReport(null);
    setDiagnosticError(null);
    setShowDiagnostic(true);

    try {
      const report = await diagnoseMCPService(name.trim());
      setDiagnosticReport(report);
    } catch (err) {
      setDiagnosticError(err instanceof Error ? err.message : (locale === "zh" ? "诊断失败" : "Diagnosis failed"));
    } finally {
      setIsDiagnosing(false);
    }
  };

  return (
    <AnimatePresence>
      {isOpen && (
        <motion.div
          key="mcp-dialog-overlay"
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
          className="w-full max-w-lg"
        >
          <Card className="overflow-hidden">
            {/* Header */}
            <div className="bg-gradient-to-r from-emerald-500 to-teal-600 px-6 py-5">
              <div className="flex justify-between items-center">
                <div className="flex items-center gap-3">
                  <Server size={20} className="text-white" />
                  <div>
                    <h2 className="text-lg font-semibold text-white">
                      {isEdit
                        ? (locale === "zh" ? "编辑 MCP 服务" : "Edit MCP Service")
                        : (locale === "zh" ? "创建 MCP 服务" : "Create MCP Service")}
                  </h2>
                </div>
                </div>
                <button
                  onClick={onClose}
                  className="text-white/70 hover:text-white transition-colors p-1"
                >
                  <X size={20} />
                </button>
              </div>
            </div>

            {/* Form */}
            <div className="p-6 space-y-5 max-h-[70vh] overflow-y-auto">
              {/* 基本信息 */}
              <div className="space-y-4">
                <div className="flex items-center gap-2 text-sm text-gray-400">
                  <Settings size={14} />
                  <span>{locale === "zh" ? "基本信息" : "Basic Info"}</span>
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-300 mb-2">
                    {locale === "zh" ? "服务名称" : "Service Name"} *
                  </label>
                  <Input
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder={locale === "zh" ? "例如：weather-api" : "e.g., weather-api"}
                    disabled={isEdit}
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-300 mb-2">
                    {locale === "zh" ? "描述" : "Description"}
                  </label>
                  <Textarea
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder={locale === "zh" ? "描述这个服务的功能..." : "Describe this service..."}
                    rows={2}
                  />
                </div>

                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={enabled}
                    onChange={(e) => setEnabled(e.target.checked)}
                    className="w-4 h-4 rounded accent-emerald-500"
                  />
                  <span className="text-sm text-gray-300">
                    {locale === "zh" ? "启用此服务" : "Enable this service"}
                  </span>
                </label>
              </div>

              {/* SSE 配置 - 唯一支持的连接方式 */}
              <div className="space-y-4 pt-2 border-t border-white/5">
                <div className="flex items-center gap-2 text-sm text-gray-400">
                  <Link size={14} />
                  <span>{locale === "zh" ? "SSE 连接配置" : "SSE Connection Config"}</span>
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-300 mb-2">
                    {locale === "zh" ? "服务 URL" : "Service URL"} *
                  </label>
                  <Input
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    placeholder="http://localhost:20882/calculator"
                  />
                  <p className="text-xs text-gray-500 mt-1">
                    {locale === "zh"
                      ? "MCP 服务的 HTTP 端点地址"
                      : "HTTP endpoint URL of the MCP service"}
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-300 mb-2">
                    {locale === "zh" ? "认证类型" : "Auth Type"}
                  </label>
                  <select
                    value={authType}
                    onChange={(e) => setAuthType(e.target.value as "none" | "bearer" | "apikey")}
                    className="w-full h-10 rounded-xl border border-white/10 bg-white/5 px-4 text-sm text-white focus:outline-none focus:border-blue-500/50"
                  >
                    <option value="none">{locale === "zh" ? "无认证" : "None"}</option>
                    <option value="bearer">Bearer Token</option>
                    <option value="apikey">API Key</option>
                  </select>
                </div>

                {authType !== "none" && (
                  <div>
                    <label className="block text-sm font-medium text-gray-300 mb-2">
                      <div className="flex items-center gap-2">
                        <Key size={14} />
                        {authType === "bearer" ? "Bearer Token" : "API Key"}
                      </div>
                    </label>
                    <Input
                      type="password"
                      value={authValue}
                      onChange={(e) => setAuthValue(e.target.value)}
                      placeholder={isEdit ? (locale === "zh" ? "留空保持不变" : "Leave empty to keep") : ""}
                    />
                  </div>
                )}

                <div>
                  <label className="block text-sm font-medium text-gray-300 mb-2">
                    {locale === "zh" ? "自定义请求头 (JSON)" : "Custom Headers (JSON)"}
                  </label>
                  <Textarea
                    value={headers}
                    onChange={(e) => setHeaders(e.target.value)}
                    placeholder='{"X-Custom-Header": "value"}'
                    rows={3}
                    className="font-mono text-sm"
                  />
                </div>
              </div>

              {/* 诊断连接按钮和状态 */}
              {isEdit && (
                <div className="pt-2 border-t border-white/5">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2 text-sm text-gray-400">
                      <Activity size={14} />
                      <span>{locale === "zh" ? "连接诊断" : "Connection Diagnostic"}</span>
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleDiagnose}
                      disabled={isDiagnosing}
                      className="h-8 px-3 text-xs"
                    >
                      {isDiagnosing ? (
                        <>
                          <Loader2 size={14} className="animate-spin mr-1" />
                          {locale === "zh" ? "诊断中..." : "Diagnosing..."}
                        </>
                      ) : (
                        <>
                          <Activity size={14} className="mr-1" />
                          {locale === "zh" ? "诊断连接" : "Diagnose"}
                        </>
                      )}
                    </Button>
                  </div>

                  {/* 诊断结果摘要 */}
                  {diagnosticReport && !showDiagnostic && (
                    <div className={`mt-2 p-2 rounded-lg text-xs flex items-center gap-2 ${
                      diagnosticReport.overall_status === "healthy"
                        ? "bg-emerald-500/10 text-emerald-300"
                        : diagnosticReport.overall_status === "degraded"
                        ? "bg-amber-500/10 text-amber-300"
                        : "bg-red-500/10 text-red-300"
                    }`}>
                      {diagnosticReport.overall_status === "healthy" ? (
                        <span className="font-medium">
                          {locale === "zh" ? "连接正常" : "Connection OK"}
                        </span>
                      ) : diagnosticReport.overall_status === "degraded" ? (
                        <span className="font-medium">
                          {locale === "zh" ? "服务降级" : "Service Degraded"}
                        </span>
                      ) : (
                        <span className="font-medium">
                          {locale === "zh" ? "连接失败" : "Connection Failed"}
                        </span>
                      )}
                      <button
                        onClick={() => setShowDiagnostic(true)}
                        className="underline hover:no-underline"
                      >
                        {locale === "zh" ? "查看详情" : "View Details"}
                      </button>
                    </div>
                  )}

                  {/* 诊断错误 */}
                  {diagnosticError && (
                    <div className="mt-2 p-2 rounded-lg bg-red-500/10 text-red-300 text-xs flex items-center gap-2">
                      <AlertCircle size={12} />
                      <span>{diagnosticError}</span>
                    </div>
                  )}
                </div>
              )}

              {error && (
                <div className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-xl text-sm">
                  {error}
                </div>
              )}

              <div className="flex gap-3 pt-2">
                <Button variant="outline" onClick={onClose} className="flex-1">
                  {locale === "zh" ? "取消" : "Cancel"}
                </Button>
                <Button onClick={handleSave} disabled={isLoading} className="flex-1">
                  {isLoading ? (
                    <Loader2 size={16} className="animate-spin" />
                  ) : (
                    <Server size={16} />
                  )}
                  {locale === "zh" ? "保存" : "Save"}
                </Button>
              </div>
            </div>
          </Card>
        </motion.div>
      </motion.div>
      )}

      {/* 诊断结果弹窗 */}
      <MCPDiagnosticResult
        isOpen={showDiagnostic}
        onClose={() => setShowDiagnostic(false)}
        serviceName={name}
        report={diagnosticReport}
        isLoading={isDiagnosing}
      />
    </AnimatePresence>
  );
}
