# Knowledge Base Selector UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor KnowledgeBaseSelector to use a shared MultiSelectPanel component with SubAgentSelector, unifying dark theme styling and adding search, badges, and quick actions.

**Architecture:** Create a generic `MultiSelectPanel<T>` component that encapsulates expand/collapse, search, selection, badges, and action callbacks. Both KnowledgeBaseSelector and SubAgentSelector will be refactored to use this shared component.

**Tech Stack:** React, TypeScript, Tailwind CSS, Framer Motion, Lucide Icons

---

## File Structure

```
frontend/src/components/
├── ui/
│   └── MultiSelectPanel.tsx    ← NEW: Generic multi-select panel (250 lines)
├── KnowledgeBaseSelector.tsx   ← REFACTOR: Thin wrapper using MultiSelectPanel (~50 lines)
└── SubAgentSelector.tsx        ← REFACTOR: Use MultiSelectPanel, preserve special features
```

---

## Task 1: Create MultiSelectPanel Component (Core)

**Files:**
- Create: `frontend/src/components/ui/MultiSelectPanel.tsx`

**Goal:** Build the generic multi-select panel component with all core features.

- [ ] **Step 1.1: Create file with TypeScript interfaces**

```typescript
"use client";

import { useState, useMemo, ReactNode } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Search,
  ChevronDown,
  ChevronUp,
  X,
  Loader2,
  Plus,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// Theme color configuration
export type ThemeColor = "emerald" | "indigo" | "blue" | "purple" | "amber" | "red";

export interface Badge {
  label: string;
  variant?: "default" | "primary" | "secondary";
}

export interface MultiSelectPanelProps<T> {
  // Display configuration
  title: string;
  icon: ReactNode;
  color: ThemeColor;
  hint?: string;

  // Data
  items: T[];
  selectedIds: string[];
  onChange: (ids: string[]) => void;

  // Item rendering
  getId: (item: T) => string;
  getTitle: (item: T) => string;
  getDescription: (item: T) => string;
  getBadges?: (item: T) => Badge[];
  getExtraInfo?: (item: T) => string;  // Extra info line (e.g., "🔧 2 skills  🔌 1 service")
  getItemIcon?: (item: T) => ReactNode;

  // Optional features
  searchPlaceholder?: string;
  emptyMessage?: string;
  onCreateNew?: () => void;
  onItemClick?: (item: T) => void;

  // State
  disabled?: boolean;
  loading?: boolean;
  defaultExpanded?: boolean;
}
```

- [ ] **Step 1.2: Add color theme utilities**

```typescript
// Color theme mapping
const colorClasses: Record<ThemeColor, {
  accent: string;
  bg: string;
  border: string;
  text: string;
  iconBg: string;
}> = {
  emerald: {
    accent: "text-emerald-400",
    bg: "bg-emerald-500/10",
    border: "border-emerald-500/20",
    text: "text-emerald-300",
    iconBg: "bg-emerald-500/20",
  },
  indigo: {
    accent: "text-indigo-400",
    bg: "bg-indigo-500/10",
    border: "border-indigo-500/20",
    text: "text-indigo-300",
    iconBg: "bg-indigo-500/20",
  },
  blue: {
    accent: "text-blue-400",
    bg: "bg-blue-500/10",
    border: "border-blue-500/20",
    text: "text-blue-300",
    iconBg: "bg-blue-500/20",
  },
  purple: {
    accent: "text-purple-400",
    bg: "bg-purple-500/10",
    border: "border-purple-500/20",
    text: "text-purple-300",
    iconBg: "bg-purple-500/20",
  },
  amber: {
    accent: "text-amber-400",
    bg: "bg-amber-500/10",
    border: "border-amber-500/20",
    text: "text-amber-300",
    iconBg: "bg-amber-500/20",
  },
  red: {
    accent: "text-red-400",
    bg: "bg-red-500/10",
    border: "border-red-500/20",
    text: "text-red-300",
    iconBg: "bg-red-500/20",
  },
};
```

- [ ] **Step 1.3: Implement main component function**

```typescript
export function MultiSelectPanel<T>({
  title,
  icon,
  color,
  hint,
  items,
  selectedIds,
  onChange,
  getId,
  getTitle,
  getDescription,
  getBadges,
  getItemIcon,
  searchPlaceholder = "搜索...",
  emptyMessage = "没有可选项",
  onCreateNew,
  onItemClick,
  disabled = false,
  loading = false,
  defaultExpanded = false,
}: MultiSelectPanelProps<T>) {
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);
  const [searchQuery, setSearchQuery] = useState("");

  const theme = colorClasses[color];

  // Get selected items details
  const selectedItems = useMemo(() => {
    return items.filter((item) => selectedIds.includes(getId(item)));
  }, [items, selectedIds, getId]);

  // Filter available items (exclude selected, apply search)
  const filteredItems = useMemo(() => {
    return items.filter((item) => {
      if (selectedIds.includes(getId(item))) return false;
      if (searchQuery) {
        const query = searchQuery.toLowerCase();
        return (
          getTitle(item).toLowerCase().includes(query) ||
          getDescription(item).toLowerCase().includes(query)
        );
      }
      return true;
    });
  }, [items, selectedIds, searchQuery, getId, getTitle, getDescription]);

  // Handle toggle selection
  const handleToggle = (id: string) => {
    if (disabled) return;
    if (selectedIds.includes(id)) {
      onChange(selectedIds.filter((i) => i !== id));
    } else {
      onChange([...selectedIds, id]);
    }
  };

  // Handle remove from selected
  const handleRemove = (id: string) => {
    if (disabled) return;
    onChange(selectedIds.filter((i) => i !== id));
  };
```

- [ ] **Step 1.4: Implement JSX return**

```typescript
  return (
    <Card className="overflow-hidden border-white/[0.08] bg-white/[0.02]">
      {/* Header */}
      <div
        className={cn(
          "px-5 py-4 border-b border-white/[0.05] flex items-center gap-3 bg-white/[0.02] cursor-pointer transition-colors",
          !disabled && "hover:bg-white/[0.04]"
        )}
        onClick={() => !disabled && setIsExpanded(!isExpanded)}
      >
        <span className={theme.accent}>{icon}</span>
        <span className="font-medium text-sm text-gray-300 flex-1">{title}</span>
        <div className="flex items-center gap-2">
          {selectedIds.length > 0 && (
            <span className={cn("text-xs px-2 py-0.5 rounded-lg", theme.bg, theme.accent)}>
              {selectedIds.length}
            </span>
          )}
          {loading && <Loader2 size={14} className="animate-spin text-gray-400" />}
          {isExpanded ? (
            <ChevronUp size={16} className="text-gray-500" />
          ) : (
            <ChevronDown size={16} className="text-gray-500" />
          )}
        </div>
      </div>

      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
          >
            <CardContent className="p-5 space-y-4">
              {/* Hint */}
              {hint && (
                <div className={cn("flex items-start gap-2 p-3 rounded-lg", theme.bg, theme.border, "border")}>
                  <span className={theme.accent}>💡</span>
                  <div className="text-xs text-gray-400">{hint}</div>
                </div>
              )}

              {/* Selected Items */}
              {selectedItems.length > 0 && (
                <div>
                  <div className="text-xs text-gray-500 mb-2">已选择</div>
                  <div className="space-y-2">
                    {selectedItems.map((item) => {
                      const id = getId(item);
                      return (
                        <div
                          key={id}
                          className={cn("flex items-start gap-3 p-3 rounded-lg", theme.bg, theme.border, "border")}
                        >
                          {getItemIcon && (
                            <div className={cn("w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0", theme.iconBg)}>
                              {getItemIcon(item)}
                            </div>
                          )}
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className={cn("font-medium text-sm", theme.text)}>
                                {getTitle(item)}
                              </span>
                              {getBadges?.(item).map((badge, i) => (
                                <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-white/10 text-gray-400">
                                  {badge.label}
                                </span>
                              ))}
                            </div>
                            <p className="text-xs text-gray-500 line-clamp-2 mt-1">
                              {getDescription(item)}
                            </p>
                          </div>
                          {onItemClick && (
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => onItemClick(item)}
                              disabled={disabled}
                              className="h-7 w-7 p-0 text-gray-400 hover:text-white"
                            >
                              👁️
                            </Button>
                          )}
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => handleRemove(id)}
                            disabled={disabled}
                            className="h-7 w-7 p-0 hover:bg-red-500/20 text-gray-400 hover:text-red-400"
                          >
                            <X size={14} />
                          </Button>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {/* Search */}
              {filteredItems.length > 0 && (
                <div className="relative">
                  <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
                  <Input
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    placeholder={searchPlaceholder}
                    className="pl-9 h-9 bg-white/5 border-white/10 text-sm text-white placeholder:text-gray-600"
                  />
                </div>
              )}

              {/* Available Items */}
              {filteredItems.length === 0 ? (
                <div className="text-center py-8">
                  <div className="w-12 h-12 rounded-xl bg-white/5 flex items-center justify-center mx-auto mb-3">
                    {icon}
                  </div>
                  <p className="text-sm text-gray-500">
                    {searchQuery ? "没有找到匹配项" : emptyMessage}
                  </p>
                </div>
              ) : (
                <div className="space-y-2 max-h-64 overflow-y-auto pr-1 custom-scrollbar">
                  {filteredItems.map((item) => {
                    const id = getId(item);
                    return (
                      <label
                        key={id}
                        className={cn(
                          "flex items-start gap-3 p-3 rounded-xl cursor-pointer transition-all border",
                          disabled ? "opacity-50 cursor-not-allowed" : "hover:bg-white/[0.04]",
                          "bg-white/[0.02] border-white/[0.05]"
                        )}
                      >
                        <input
                          type="checkbox"
                          checked={false}
                          onChange={() => handleToggle(id)}
                          disabled={disabled}
                          className={cn("mt-0.5 w-4 h-4 rounded flex-shrink-0", `accent-${color}-500`)}
                        />
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="font-medium text-sm text-gray-200">
                              {getTitle(item)}
                            </span>
                            {getBadges?.(item).map((badge, i) => (
                              <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-white/10 text-gray-400">
                                {badge.label}
                              </span>
                            ))}
                          </div>
                          <p className="text-xs text-gray-500 line-clamp-2 mt-1">
                            {getDescription(item)}
                          </p>
                          {/* Extra info line (skills, services count) */}
                          {getExtraInfo?.(item) && (
                            <div className="text-[10px] text-gray-600 mt-2">
                              {getExtraInfo(item)}
                            </div>
                          )}
                        </div>
                      </label>
                    );
                  })}
                </div>
              )}

              {/* Create New Button */}
              {onCreateNew && (
                <button
                  onClick={onCreateNew}
                  disabled={disabled}
                  className={cn(
                    "w-full p-3 rounded-lg border border-dashed border-white/10",
                    "text-sm text-gray-500 hover:text-gray-300 hover:border-white/20",
                    "flex items-center justify-center gap-2 transition-colors",
                    disabled && "opacity-50 cursor-not-allowed"
                  )}
                >
                  <Plus size={14} />
                  新建
                </button>
              )}
            </CardContent>
          </motion.div>
        )}
      </AnimatePresence>
    </Card>
  );
}
```

- [ ] **Step 1.5: Verify component compiles**

Run from the repository root: `cd frontend && npm run build 2>&1 | head -50`
Expected: No errors related to MultiSelectPanel.tsx

- [ ] **Step 1.6: Commit**

```bash
git add frontend/src/components/ui/MultiSelectPanel.tsx
git commit -m "feat(ui): add MultiSelectPanel generic component

Add reusable multi-select panel with:
- Expandable/collapsible panel
- Search filtering
- Selected items display
- Badge support
- Theme color variants
- Create new action

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Refactor KnowledgeBaseSelector

**Files:**
- Modify: `frontend/src/components/KnowledgeBaseSelector.tsx`
- Modify: `frontend/src/app/page.tsx` (add callbacks)

**Goal:** Replace current implementation with MultiSelectPanel wrapper.

- [ ] **Step 2.1: Rewrite KnowledgeBaseSelector to use MultiSelectPanel**

```typescript
/**
 * @userGuide
 * @title.en Knowledge Base Selector
 * @title.zh 知识库选择器
 * @category core
 * @description.en Select which knowledge bases your agent can search for relevant information.
 * @description.zh 选择智能体可以搜索的知识库，以获取相关信息。
 * @related KnowledgeBaseDialog, DocumentUploader
 */
"use client";

import { useState, useEffect } from "react";
import { Database } from "lucide-react";
import { MultiSelectPanel } from "@/components/ui/MultiSelectPanel";
import { kbApi, KnowledgeBase } from "@/lib/kbApi";

interface KnowledgeBaseSelectorProps {
  selectedIds: string[];
  onChange: (ids: string[]) => void;
  disabled?: boolean;
  /** Callback to create new knowledge base */
  onCreateNew?: () => void;
  /** Callback to view knowledge base details */
  onItemClick?: (kb: KnowledgeBase) => void;
}

export function KnowledgeBaseSelector({
  selectedIds,
  onChange,
  disabled = false,
  onCreateNew,
  onItemClick,
}: KnowledgeBaseSelectorProps) {
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const loadKnowledgeBases = async () => {
      setLoading(true);
      try {
        const data = await kbApi.listKnowledgeBases();
        setKnowledgeBases(data);
      } catch (error) {
        console.error("加载知识库失败:", error);
      } finally {
        setLoading(false);
      }
    };
    loadKnowledgeBases();
  }, []);

  return (
    <MultiSelectPanel<KnowledgeBase>
      title="知识库配置"
      icon={<Database size={16} />}
      color="emerald"
      hint="选择知识库后，智能体将基于私有文档内容回答问题"
      items={knowledgeBases}
      selectedIds={selectedIds}
      onChange={onChange}
      getId={(kb) => kb.kb_id}
      getTitle={(kb) => kb.name}
      getDescription={(kb) => kb.description || "暂无描述"}
      getBadges={(kb) => [
        { label: `${kb.doc_count} 文档` },
        { label: `${kb.chunk_count} 分块` },
      ]}
      getItemIcon={() => <Database size={14} className="text-emerald-400" />}
      searchPlaceholder="搜索知识库..."
      emptyMessage="还没有知识库，请先创建"
      onCreateNew={onCreateNew}
      onItemClick={onItemClick}
      disabled={disabled}
      loading={loading}
      defaultExpanded={selectedIds.length > 0}
    />
  );
}
```

- [ ] **Step 2.2: Update page.tsx to pass new callbacks**

Find the KnowledgeBaseSelector usage in page.tsx and update:

```typescript
// In the config panel section, replace:
<KnowledgeBaseSelector
  selectedIds={selectedKnowledgeBases}
  onChange={setSelectedKnowledgeBases}
  disabled={isEnvironmentCreating || isSaving}
/>

// With:
<KnowledgeBaseSelector
  selectedIds={selectedKnowledgeBases}
  onChange={setSelectedKnowledgeBases}
  disabled={isEnvironmentCreating || isSaving}
  onCreateNew={() => { setEditingKb(null); setKbDialogOpen(true); }}
  onItemClick={(kb) => { setSelectedKb(kb); setKbDetailOpen(true); }}
/>
```

- [ ] **Step 2.3: Remove the old confirmation banner**

Remove this redundant section after KnowledgeBaseSelector in page.tsx:

```typescript
// DELETE this block - it's now redundant with the panel's built-in display:
{selectedKnowledgeBases.length > 0 && (
  <div className="mt-3 p-3 bg-emerald-500/10 border border-emerald-500/20 rounded-lg">
    <p className="text-xs text-emerald-400">
      {locale === "zh"
        ? `✓ 已挂载 ${selectedKnowledgeBases.length} 个知识库`
        : `✓ ${selectedKnowledgeBases.length} knowledge base(s) mounted`}
    </p>
  </div>
)}
```

- [ ] **Step 2.4: Verify build succeeds**

Run from the repository root: `cd frontend && npm run build 2>&1 | head -50`
Expected: Build succeeds

- [ ] **Step 2.5: Test in browser**

Run: `./start.sh` (if not already running)
Open: http://localhost:20880
Test:
1. Create/edit agent page
2. Knowledge base panel expands
3. Search works
4. Select/deselect works
5. Create new button opens dialog
6. View details opens KbDetailPanel

- [ ] **Step 2.6: Commit**

```bash
git add frontend/src/components/KnowledgeBaseSelector.tsx frontend/src/app/page.tsx
git commit -m "refactor(kb): use MultiSelectPanel for KnowledgeBaseSelector

- Replace dropdown with expandable dark theme panel
- Add search functionality
- Show document/chunk count badges
- Add create new and view details actions
- Remove redundant confirmation banner

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Refactor SubAgentSelector

**Files:**
- Modify: `frontend/src/components/SubAgentSelector.tsx`

**Goal:** Use MultiSelectPanel while preserving SubAgentSelector's unique features.

- [ ] **Step 3.1: Identify SubAgentSelector unique features to preserve**

Features not in MultiSelectPanel that must be preserved:
1. `currentAgentName` prop - exclude self from selection
2. `cycleError` prop - display cycle dependency error
3. Skills/MCP services count display
4. Model service badge
5. Footer warning message

- [ ] **Step 3.2: Refactor SubAgentSelector to use MultiSelectPanel with extensions**

```typescript
/**
 * @userGuide
 * @title.en Sub-Agent Selector
 * @title.zh 子智能体选择器
 * @category core
 * @description.en Configure which sub-agents this agent can call for task delegation.
 * @description.zh 配置此智能体可以调用的子智能体，实现任务委派。
 * @related AgentChat, KnowledgeBaseSelector
 */
"use client";

import { useMemo } from "react";
import { Bot, AlertTriangle } from "lucide-react";
import { MultiSelectPanel, Badge } from "@/components/ui/MultiSelectPanel";
import { useLocale } from "@/lib/LocaleContext";
import { SubAgentInfo, CycleDependencyError } from "@/types";

interface SubAgentSelectorProps {
  availableAgents: SubAgentInfo[];
  currentAgentName?: string;
  selectedAgents: string[];
  onSelectionChange: (agents: string[]) => void;
  cycleError?: CycleDependencyError | null;
  disabled?: boolean;
}

export function SubAgentSelector({
  availableAgents,
  currentAgentName = "",
  selectedAgents,
  onSelectionChange,
  cycleError,
  disabled = false,
}: SubAgentSelectorProps) {
  const { locale } = useLocale();
  const zh = locale === "zh";

  // Filter out current agent from available list
  const filteredAgents = useMemo(() => {
    return availableAgents.filter((agent) => agent.name !== currentAgentName);
  }, [availableAgents, currentAgentName]);

  // Get badges for an agent
  const getAgentBadges = (agent: SubAgentInfo): Badge[] => {
    const badges: Badge[] = [];
    if (agent.model_service) {
      badges.push({ label: agent.model_service, variant: "primary" });
    } else {
      badges.push({ label: zh ? "无模型" : "No model" });
    }
    if (agent.sub_agents && agent.sub_agents.length > 0) {
      badges.push({ label: `${agent.sub_agents.length} ${zh ? "个子" : "sub"}` });
    }
    return badges;
  };

  // Get extra info line (skills/services count)
  const getAgentExtraInfo = (agent: SubAgentInfo): string => {
    const parts: string[] = [];
    if (agent.skills.length > 0) {
      parts.push(`🔧 ${agent.skills.length} ${zh ? "技能" : "skills"}`);
    }
    if (agent.mcp_services.length > 0) {
      parts.push(`🔌 ${agent.mcp_services.length} ${zh ? "服务" : "services"}`);
    }
    return parts.join("  ");
  };

  return (
    <div className="space-y-3">
      {/* Cycle Error - displayed above panel */}
      {cycleError && (
        <div className="flex items-start gap-2 p-3 rounded-lg bg-red-500/10 border border-red-500/20">
          <AlertTriangle size={14} className="text-red-400 mt-0.5 flex-shrink-0" />
          <div className="flex-1">
            <div className="text-sm text-red-400 font-medium mb-1">
              {cycleError.message || (zh ? "检测到循环依赖" : "Circular dependency detected")}
            </div>
            {cycleError.cycle_path && cycleError.cycle_path.length > 0 && (
              <div className="text-xs text-gray-500 font-mono">
                {cycleError.cycle_path.join(" → ")}
              </div>
            )}
          </div>
        </div>
      )}

      <MultiSelectPanel<SubAgentInfo>
        title={zh ? "子 Agent 配置" : "Sub-Agent Configuration"}
        icon={<Bot size={16} />}
        color="indigo"
        hint={zh
          ? "选择此 Agent 可以调用的子 Agent。主 Agent 可以将任务委派给子 Agent 处理，适合多角色协作场景。"
          : "Select sub-agents that this agent can call. The main agent can delegate tasks to sub-agents for multi-role collaboration."
        }
        items={filteredAgents}
        selectedIds={selectedAgents}
        onChange={onSelectionChange}
        getId={(agent) => agent.name}
        getTitle={(agent) => agent.name}
        getDescription={(agent) => agent.persona}
        getBadges={getAgentBadges}
        getItemIcon={() => <Bot size={14} className="text-indigo-400" />}
        getExtraInfo={getAgentExtraInfo}
        searchPlaceholder={zh ? "搜索 Agent 名称或人设..." : "Search agent name or persona..."}
        emptyMessage={zh ? "没有可用的 Agent" : "No available agents"}
        disabled={disabled}
        defaultExpanded={selectedAgents.length > 0}
      />

      {/* Footer warning */}
      <div className="text-xs text-gray-600 flex items-center gap-1.5">
        <AlertTriangle size={12} className="text-amber-500/60" />
        {zh
          ? "注意：子 Agent 的工具和技能将作为主 Agent 的扩展能力可用"
          : "Note: Sub-agent tools and skills will be available to the main agent"}
      </div>
    </div>
  );
}
```

- [ ] **Step 3.3: Verify build succeeds**

Run from the repository root: `cd frontend && npm run build 2>&1 | head -50`
Expected: Build succeeds

- [ ] **Step 3.4: Test SubAgentSelector in browser**

Test:
1. Expand/collapse panel
2. Search works
3. Select/deselect agents
4. Self (currentAgentName) not in list
5. Cycle error displays correctly
6. Skills/MCP count shown
7. Footer warning visible

- [ ] **Step 3.5: Commit**

```bash
git add frontend/src/components/SubAgentSelector.tsx
git commit -m "refactor(agents): use MultiSelectPanel for SubAgentSelector

- Replace custom Card with MultiSelectPanel
- Preserve cycle error handling above panel
- Preserve currentAgentName exclusion
- Preserve footer warning message
- Maintain all existing functionality

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Final Testing & Polish

**Goal:** Ensure both selectors work correctly and visual consistency is achieved.

- [ ] **Step 4.1: Visual comparison test**

Open both selectors side by side:
1. Knowledge base selector (emerald theme)
2. Sub-agent selector (indigo theme)

Verify:
- Same structure and layout
- Color themes applied correctly
- Animations consistent

- [ ] **Step 4.2: Interaction test matrix**

| Action | KB Selector | SubAgent Selector |
|--------|-------------|-------------------|
| Expand/collapse | ✓ | ✓ |
| Search filter | ✓ | ✓ |
| Select item | ✓ | ✓ |
| Deselect from selected | ✓ | ✓ |
| Create new (KB only) | ✓ | N/A |
| View details (KB only) | ✓ | N/A |
| Cycle error (Sub only) | N/A | ✓ |

- [ ] **Step 4.3: Accessibility check**

- Keyboard navigation works (Tab, Enter, Space)
- Focus indicators visible
- Screen reader labels present

- [ ] **Step 4.4: Final commit**

```bash
git add -A
git commit -m "chore: verify knowledge base selector redesign complete

Both selectors now use shared MultiSelectPanel with:
- Dark theme glassmorphism styling
- Search functionality
- Badge support
- Theme color variants

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Summary

| Task | Files | Lines Changed |
|------|-------|---------------|
| 1. MultiSelectPanel | `ui/MultiSelectPanel.tsx` (new) | +250 |
| 2. KnowledgeBaseSelector | `KnowledgeBaseSelector.tsx`, `page.tsx` | -100, +30 |
| 3. SubAgentSelector | `SubAgentSelector.tsx` | -200, +80 |
| **Total** | 4 files | ~200 net reduction |

**Risk mitigated by:**
- SubAgentSelector preserves all unique features (cycle error, self-exclusion, footer)
- Incremental commits allow easy rollback
- Visual testing before each commit
