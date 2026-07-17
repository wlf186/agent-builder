import { defineConfig } from 'vitepress'

export default defineConfig({
  title: 'Agent Builder User Guide',

  // 必须设置 base 路径，因为 docs-site 被代理到 /docs 路径
  base: '/docs/',

  locales: {
    en: {
      label: 'English',
      lang: 'en',
      link: '/en/',
    },
    zh: {
      label: '简体中文',
      lang: 'zh-CN',
      link: '/zh/',
    },
  },

  themeConfig: {
    logo: '/logo.svg',
    siteTitle: 'Agent Builder',

    nav: [
      { text: 'Getting Started', link: '/en/getting-started' },
      { text: 'Core Features', link: '/en/core/' },
      { text: 'Advanced', link: '/en/advanced/' },
      { text: 'Help', link: '/en/help/faq' },
    ],

    sidebar: {
      '/en/': [
        {
          text: 'Getting Started',
          link: '/en/getting-started',
        },
        {
          text: 'Core Features',
          collapsed: false,
          items: [
            { text: 'Chat Interface', link: '/en/core/agent-chat' },
            { text: 'Managing Conversations', link: '/en/core/conversation-drawer' },
            { text: 'Creating Agents', link: '/en/core/sub-agent-selector' },
            { text: 'Knowledge Bases', link: '/en/core/knowledge-base-selector' },
            { text: 'Uploading Documents', link: '/en/core/document-uploader' },
            { text: 'File Attachments', link: '/en/core/file-uploader' },
          ],
        },
        {
          text: 'Advanced',
          collapsed: false,
          items: [
            { text: 'Model Services', link: '/en/advanced/model-service-dialog' },
            { text: 'MCP Services', link: '/en/advanced/mcp-service-dialog' },
            { text: 'Skills', link: '/en/advanced/skill-detail-dialog' },
            { text: 'Local Observability', link: '/en/advanced/observability' },
          ],
        },
        {
          text: 'Reference',
          collapsed: true,
          items: [
            { text: 'Sub-Agent Call Card', link: '/en/reference/sub-agent-call-card' },
            { text: 'Environment Status', link: '/en/reference/environment-banner' },
          ],
        },
        {
          text: 'Help',
          collapsed: false,
          items: [
            { text: 'FAQ', link: '/en/help/faq' },
            { text: 'Troubleshooting', link: '/en/help/troubleshooting' },
          ],
        },
      ],
      '/zh/': [
        {
          text: '快速开始',
          link: '/zh/getting-started',
        },
        {
          text: '核心功能',
          collapsed: false,
          items: [
            { text: '聊天界面', link: '/zh/core/agent-chat' },
            { text: '管理对话', link: '/zh/core/conversation-drawer' },
            { text: '创建智能体', link: '/zh/core/sub-agent-selector' },
            { text: '知识库', link: '/zh/core/knowledge-base-selector' },
            { text: '上传文档', link: '/zh/core/document-uploader' },
            { text: '文件附件', link: '/zh/core/file-uploader' },
          ],
        },
        {
          text: '高级功能',
          collapsed: false,
          items: [
            { text: '模型服务', link: '/zh/advanced/model-service-dialog' },
            { text: 'MCP服务', link: '/zh/advanced/mcp-service-dialog' },
            { text: '技能', link: '/zh/advanced/skill-detail-dialog' },
            { text: '本地可观测性', link: '/zh/advanced/observability' },
          ],
        },
        {
          text: '参考',
          collapsed: true,
          items: [
            { text: '子智能体调用卡', link: '/zh/reference/sub-agent-call-card' },
            { text: '环境状态', link: '/zh/reference/environment-banner' },
          ],
        },
        {
          text: '帮助',
          collapsed: false,
          items: [
            { text: '常见问题', link: '/zh/help/faq' },
            { text: '故障排除', link: '/zh/help/troubleshooting' },
          ],
        },
      ],
    },

    socialLinks: [
      { icon: 'github', link: 'https://github.com/wlf186/agent-builder' },
    ],
  },
})
