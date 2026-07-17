"use client";

/**
 * @userGuide
 * @title.en Model Service Configuration
 * @title.zh 模型服务配置
 * @category advanced
 * @description.en Configure LLM model services for AI agents. Supports multiple providers including Zhipu AI, Alibaba Bailian, and local Ollama.
 * @description.zh 为 AI 智能体配置大语言模型服务。支持智谱AI、阿里云百炼和本地 Ollama 等多种供应商。
 * @steps.en
 *   1. Click the settings icon or "Add Model Service" button
 *   2. Enter a unique name for the model service
 *   3. Select a provider (Zhipu AI, Alibaba Bailian, or Ollama)
 *   4. Enter the service URL (auto-filled for known providers)
 *   5. Enter the API Key (required for cloud providers)
 *   6. Click "Test Connection" to verify connectivity and fetch available models
 *   7. Select a model from the dropdown list
 *   8. Toggle the enable switch and click "Save"
 * @steps.zh
 *   1. 点击设置图标或"添加模型服务"按钮
 *   2. 输入模型服务的唯一名称
 *   3. 选择供应商（智谱AI、阿里云百炼或 Ollama）
 *   4. 输入服务地址（已知供应商会自动填充）
 *   5. 输入 API Key（云服务商必填）
 *   6. 点击"测试连接"验证连通性并获取可用模型
 *   7. 从下拉列表中选择一个模型
 *   8. 切换启用开关并点击"保存"
 * @tips.en
 *   - API keys submitted through this password field are stored server-side and are never returned in plaintext
 *   - Test connection before saving to ensure the service is accessible
 *   - Ollama does not require an API key for local installations
 * @tips.zh
 *   - 通过密码输入框提交的 API Key 仅保存在服务端，后续不会以明文返回
 *   - 保存前先测试连接以确保服务可访问
 *   - 本地 Ollama 不需要 API Key
 * @related AgentChat
 */

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, Loader2, CheckCircle, XCircle, Info } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useLocale } from "@/lib/LocaleContext";
import { t } from "@/lib/i18n";
import { apiPath } from "@/lib/apiPath";

const API_BASE = "/api";

interface ModelServiceDialogProps {
  open: boolean;
  onClose: () => void;
  onSave: () => void;
  service?: ModelService | null;
}

export interface ModelService {
  name: string;
  description: string;
  provider: string;
  base_url: string;
  api_key?: string;
  selected_model: string;
  available_models: string[];
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

const PROVIDER_DEFAULT_URLS: Record<string, string> = {
  zhipu: "https://open.bigmodel.cn/api/coding/paas/v4",
  alibaba_bailian: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  ollama: "http://localhost:11434/v1",
};

export function ModelServiceDialog({ open, onClose, onSave, service }: ModelServiceDialogProps) {
  const { locale } = useLocale();
  const isEdit = !!service;

  const [name, setName] = useState(() => service?.name ?? "");
  const [description, setDescription] = useState(() => service?.description ?? "");
  const [provider, setProvider] = useState(() => service?.provider ?? "zhipu");
  const [baseUrl, setBaseUrl] = useState(
    () => service?.base_url ?? PROVIDER_DEFAULT_URLS.zhipu,
  );
  const [apiKey, setApiKey] = useState("");
  const [selectedModel, setSelectedModel] = useState(() => service?.selected_model ?? "");
  const [availableModels, setAvailableModels] = useState<string[]>(
    () => service?.available_models ?? [],
  );
  const [enabled, setEnabled] = useState(() => service?.enabled ?? true);

  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);
  const [saving, setSaving] = useState(false);

  const handleTestConnection = async () => {
    if (!baseUrl) {
      setTestResult({ success: false, message: locale === "zh" ? "请输入服务地址" : "Please enter service URL" });
      return;
    }

    if (provider !== "ollama" && !apiKey) {
      setTestResult({ success: false, message: locale === "zh" ? "请输入 API Key" : "Please enter API Key" });
      return;
    }

    setTesting(true);
    setTestResult(null);

    try {
      const response = await fetch(`${API_BASE}/model-services/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider,
          base_url: baseUrl,
          api_key: apiKey || null,
        }),
      });

      const data = await response.json();

      if (data.success) {
        setTestResult({ success: true, message: data.message || (locale === "zh" ? "连接成功" : "Connection successful") });
        setAvailableModels(data.models || []);
        // 如果只有一个模型，自动选中
        if (data.models && data.models.length === 1) {
          setSelectedModel(data.models[0]);
        }
      } else {
        setTestResult({ success: false, message: data.message || (locale === "zh" ? "连接失败" : "Connection failed") });
        setAvailableModels([]);
      }
    } catch {
      setTestResult({ success: false, message: locale === "zh" ? "请求失败，请检查网络" : "Request failed, please check network" });
    } finally {
      setTesting(false);
    }
  };

  const handleSave = async () => {
    if (!name.trim()) {
      alert(locale === "zh" ? "请输入服务名称" : "Please enter service name");
      return;
    }

    if (!selectedModel) {
      alert(locale === "zh" ? "请先测试连接并选择一个模型" : "Please test connection and select a model first");
      return;
    }

    setSaving(true);

    try {
      const url = isEdit ? apiPath('model-services', service.name) : apiPath('model-services');
      const method = isEdit ? "PUT" : "POST";

      const body: Record<string, unknown> = {
        description,
        provider,
        base_url: baseUrl,
        selected_model: selectedModel,
        available_models: availableModels,
        enabled,
      };

      if (!isEdit) {
        body.name = name.trim();
      }

      // 只有在提供了 API key 时才发送
      if (apiKey) {
        body.api_key = apiKey;
      }

      const response = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      if (response.ok) {
        onSave();
        onClose();
      } else {
        const data = await response.json();
        alert(data.detail || (locale === "zh" ? "保存失败" : "Save failed"));
      }
    } catch {
      alert(locale === "zh" ? "保存失败，请检查网络" : "Save failed, please check network");
    } finally {
      setSaving(false);
    }
  };

  const getProviderLabel = (p: string) => {
    switch (p) {
      case "zhipu":
        return locale === "zh" ? "智谱AI" : "Zhipu AI";
      case "alibaba_bailian":
        return locale === "zh" ? "阿里云百炼" : "Alibaba Bailian";
      case "ollama":
        return "Ollama";
      default:
        return p;
    }
  };

  if (!open) return null;

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
        onClick={(e) => {
          if (e.target === e.currentTarget) onClose();
        }}
      >
        <motion.div
          initial={{ scale: 0.95, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.95, opacity: 0 }}
          className="bg-white dark:bg-gray-800 rounded-xl shadow-xl max-w-lg w-full max-h-[90vh] overflow-y-auto"
        >
          <div className="p-6 border-b border-gray-200 dark:border-gray-700">
            <div className="flex items-center justify-between">
              <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
                {isEdit
                  ? t(locale, "editModelService")
                  : t(locale, "createModelService")}
              </h2>
              <button
                onClick={onClose}
                className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
          </div>

          <div className="p-6 space-y-4">
            {/* 服务名称 */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t(locale, "modelServiceName")}
              </label>
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t(locale, "modelServiceNamePlaceholder")}
                disabled={isEdit}
              />
            </div>

            {/* 描述 */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t(locale, "modelServiceDescription")}
              </label>
              <Input
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder={locale === "zh" ? "可选描述" : "Optional description"}
              />
            </div>

            {/* 供应商 */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t(locale, "modelProvider")}
              </label>
              <select
                value={provider}
                onChange={(e) => {
                  const nextProvider = e.target.value;
                  setProvider(nextProvider);
                  if (!isEdit && PROVIDER_DEFAULT_URLS[nextProvider]) {
                    setBaseUrl(PROVIDER_DEFAULT_URLS[nextProvider]);
                  }
                }}
                className="w-full h-10 rounded-xl border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-4 text-sm text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                <option value="zhipu">{getProviderLabel("zhipu")}</option>
                <option value="alibaba_bailian">{getProviderLabel("alibaba_bailian")}</option>
                <option value="ollama">{getProviderLabel("ollama")}</option>
              </select>
            </div>

            {/* 服务地址 */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t(locale, "modelServiceUrl")}
              </label>
              <Input
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://api.example.com/v1"
              />
            </div>

            {/* API Key - 仅智谱和百炼需要 */}
            {provider !== "ollama" && (
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  API Key
                </label>
                <Input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={isEdit ? (locale === "zh" ? "留空保持不变" : "Leave empty to keep unchanged") : (locale === "zh" ? "请输入 API Key" : "Enter API Key")}
                />
                {/* 安全提示 */}
                <div className="mt-2 p-3 rounded-lg bg-blue-50 dark:bg-blue-500/10 border border-blue-200 dark:border-blue-500/20">
                  <div className="flex items-start gap-2">
                    <Info className="w-4 h-4 text-blue-500 dark:text-blue-400 flex-shrink-0 mt-0.5" />
                    <div className="flex-1 text-xs text-gray-600 dark:text-gray-400">
                      <p className="font-medium text-blue-700 dark:text-blue-300 mb-1">
                        {locale === "zh" ? "API Key 仅保存在服务端" : "API Key stays server-side"}
                      </p>
                      <p className="text-gray-500 dark:text-gray-400">
                        {locale === "zh"
                          ? "密码字段经鉴权的同源代理提交；已保存的值不会以明文返回。"
                          : "The password field uses the authenticated same-origin proxy; saved values are never returned in plaintext."}
                      </p>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* 测试按钮 */}
            <div className="flex items-center gap-3">
              <Button
                variant="outline"
                onClick={handleTestConnection}
                disabled={testing}
              >
                {testing ? (
                  <>
                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                    {t(locale, "testing")}
                  </>
                ) : (
                  t(locale, "testConnection")
                )}
              </Button>

              {testResult && (
                <div
                  className={`flex items-center gap-1 text-sm ${
                    testResult.success
                      ? "text-green-600 dark:text-green-400"
                      : "text-red-600 dark:text-red-400"
                  }`}
                >
                  {testResult.success ? (
                    <CheckCircle className="w-4 h-4" />
                  ) : (
                    <XCircle className="w-4 h-4" />
                  )}
                  {testResult.message}
                </div>
              )}
            </div>

            {/* 模型选择 - 测试成功后显示 */}
            {availableModels.length > 0 && (
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  {t(locale, "selectModel")}
                </label>
                <select
                  value={selectedModel}
                  onChange={(e) => setSelectedModel(e.target.value)}
                  className="w-full h-10 rounded-xl border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-4 text-sm text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  <option value="">{t(locale, "pleaseSelectModel")}</option>
                  {availableModels.map((model) => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))}
                </select>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  {t(locale, "availableModelsCount").replace("{count}", String(availableModels.length))}
                </p>
              </div>
            )}

            {/* 启用开关 */}
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="enabled"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
                className="rounded border-gray-300"
              />
              <label
                htmlFor="enabled"
                className="text-sm text-gray-700 dark:text-gray-300"
              >
                {t(locale, "enableService")}
              </label>
            </div>
          </div>

          {/* 底部按钮 */}
          <div className="p-6 border-t border-gray-200 dark:border-gray-700 flex justify-end gap-3">
            <Button variant="outline" onClick={onClose}>
              {t(locale, "cancel")}
            </Button>
            <Button onClick={handleSave} disabled={saving || !selectedModel}>
              {saving ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  {t(locale, "save")}
                </>
              ) : (
                t(locale, "save")
              )}
            </Button>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
