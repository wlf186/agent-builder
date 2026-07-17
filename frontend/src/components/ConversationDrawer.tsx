/**
 * 会话抽屉组件
 * 从右侧滑出显示历史会话列表，支持搜索、新建、选择、删除等操作
 */
/**
 * @userGuide
 * @title.en Conversation History
 * @title.zh 对话历史
 * @category core
 * @description.en Access your conversation history in a slide-out drawer.
 *   Search, rename, and manage past conversations with your agent.
 * @description.zh 在滑出抽屉中访问对话历史。搜索、重命名和管理与智能体的历史对话。
 *
 * @steps.en
 *   1. Click the history icon in the chat interface to open the drawer
 *   2. Use the search box to find specific conversations
 *   3. Click on a conversation to resume it
 *   4. Hover over a conversation to rename or delete it
 *   5. Click "New Conversation" to start fresh
 * @steps.zh
 *   1. 点击聊天界面的历史图标打开抽屉
 *   2. 使用搜索框查找特定对话
 *   3. 点击对话以恢复它
 *   4. 悬停在对话上可重命名或删除
 *   5. 点击"新对话"开始新会话
 *
 * @tips.en
 *   - Conversations are grouped by agent
 *   - Search works on both titles and message content
 *   - Deleting a conversation is permanent
 * @tips.zh
 *   - 对话按智能体分组
 *   - 搜索支持标题和消息内容
 *   - 删除对话是不可恢复的
 *
 * @related AgentChat
 */
"use client";

import { useState, useEffect, useMemo, useRef, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, Plus, Search, Clock } from "lucide-react";
import { useLocale } from "@/lib/LocaleContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ConversationList, Conversation } from "./ConversationList";
import { apiPath } from "@/lib/apiPath";

export interface ConversationDrawerProps {
  open: boolean;
  onClose: () => void;
  agentName: string;
  currentConversationId: string | null;
  onSelectConversation: (id: string) => void;
  onNewConversation: () => void;
}

export function ConversationDrawer({
  open,
  onClose,
  agentName,
  currentConversationId,
  onSelectConversation,
  onNewConversation,
}: ConversationDrawerProps) {
  const { t } = useLocale();
  const [searchQuery, setSearchQuery] = useState("");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [isLoading, setIsLoading] = useState(false);

  // REQ-1.2: 跟踪上一个 agentName，在变化时先清空会话列表
  const prevAgentNameRef = useRef<string | null>(null);

  // REQ-3.2: 加载会话列表时禁用浏览器缓存
  const loadConversations = useCallback(async () => {
    setIsLoading(true);
    try {
      const res = await fetch(apiPath('agents', agentName, 'conversations'), {
        cache: 'no-store',  // 禁用缓存，确保每次都获取最新数据
        headers: {
          'Cache-Control': 'no-cache',
          'Pragma': 'no-cache',
        },
      });
      const data = await res.json();
      setConversations(data.conversations || []);
    } catch (error) {
      console.error("Failed to load conversations:", error);
    } finally {
      setIsLoading(false);
    }
  }, [agentName]);

  // 加载会话列表
  useEffect(() => {
    if (!open || !agentName) return;

    const agentChanged = prevAgentNameRef.current !== agentName;
    prevAgentNameRef.current = agentName;
    const timeout = window.setTimeout(() => {
      // REQ-1.2: agentName 变化时先清空，避免显示旧数据
      if (agentChanged) setConversations([]);
      void loadConversations();
    }, 0);

    return () => window.clearTimeout(timeout);
  }, [open, agentName, loadConversations]);

  // 过滤会话
  const filteredConversations = useMemo(() => {
    if (!searchQuery.trim()) return conversations;
    return conversations.filter((conv) => {
      const preview = conv.preview || "";
      const title = (conv.title || "").toLowerCase();
      return preview.toLowerCase().includes(searchQuery.toLowerCase()) ||
             title.includes(searchQuery.toLowerCase());
    });
  }, [conversations, searchQuery]);

  const handleRename = async (id: string, newTitle: string) => {
    try {
      const res = await fetch(apiPath('agents', agentName, 'conversations', id), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: newTitle }),
      });

      if (res.ok) {
        // 更新本地状态
        setConversations((prev) =>
          prev.map((conv) =>
            conv.id === id
              ? { ...conv, title: newTitle }
              : conv
          )
        );
      }
    } catch (error) {
      console.error("Failed to rename conversation:", error);
    }
  };

  const handleDelete = async (id: string) => {
    try {
      const res = await fetch(apiPath('agents', agentName, 'conversations', id), {
        method: "DELETE",
      });

      if (res.ok) {
        setConversations((prev) => prev.filter((conv) => conv.id !== id));
      }
    } catch (error) {
      console.error("Failed to delete conversation:", error);
    }
  };

  return (
    <AnimatePresence>
      {open && (
        <>
          {/* 遮罩层 */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="fixed inset-0 bg-black/30 z-40"
            onClick={onClose}
          />

          {/* 抽屉面板 */}
          <motion.div
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ type: "spring", damping: 25, stiffness: 300 }}
            className="fixed right-0 top-0 h-full w-[360px] max-w-[90vw] bg-[#0a0f14] border-l border-white/5 z-50 flex flex-col"
          >
            {/* 头部 */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
              <h2 className="text-sm font-medium text-gray-200">
                {t("historyConversations")}
              </h2>
              <div className="flex items-center gap-2">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={onNewConversation}
                  className="text-xs text-emerald-400 hover:text-emerald-300"
                >
                  <Plus className="w-3.5 h-3.5 mr-1" />
                  {t("newConversation")}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={onClose}
                  className="text-gray-500 hover:text-white"
                >
                  <X className="w-4 h-4" />
                </Button>
              </div>
            </div>

            {/* 搜索框 */}
            <div className="px-4 py-3">
              <div className="relative">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" />
                <Input
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder={t("searchConversations")}
                  className="pl-9 bg-white/5 border-white/10 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-white/10"
                />
              </div>
            </div>

            {/* 会话列表 */}
            <div className="flex-1 overflow-y-auto">
              <ConversationList
                conversations={conversations}
                currentId={currentConversationId}
                isLoading={isLoading}
                searchQuery={searchQuery}
                onSelect={(id) => {
                  onSelectConversation(id);
                  onClose();
                }}
                onRename={handleRename}
                onDelete={handleDelete}
              />
            </div>

            {/* 空状态 */}
            {!isLoading && filteredConversations.length === 0 && (
              <div className="text-center py-12">
                <div className="w-12 h-12 rounded-xl bg-white/5 flex items-center justify-center mx-auto mb-4">
                  <Clock className="w-6 h-6 text-gray-600" />
                </div>
                <p className="text-sm text-gray-500 mt-3">
                  {t("noConversations")}
                </p>
                <p className="text-xs text-gray-600 mt-2">
                  {t("noConversationsDesc")}
                </p>
              </div>
            )}
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
