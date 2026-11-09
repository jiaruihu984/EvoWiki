# EvoWiki for Obsidian

Obsidian 桌面端的个人学习 Wiki：提出目标、生成讲解、操作互动示例、答题、反馈、继续学习和复习。笔记以 Markdown 保存在自己的库中。

## 安装

按照 [项目 README](../README.md#首次安装) 执行 `init`、`doctor`、`up`，然后在 Obsidian 打开启动器打印的库路径，允许社区插件并启用 EvoWiki。首次设置可选择中文或英文，填写模型配置并确认预算。

启动器自动构建并安装版本配套的插件。安装或升级时 Obsidian 已打开，请重新加载插件或重启 Obsidian。

## 手动构建

```powershell
npm ci
npm run build
```

将 `main.js`、`manifest.json`、`styles.css` 放入库的 `.obsidian/plugins/evowiki/`，在 Obsidian 启用插件，并在插件设置中填写正在运行的本地后端地址。默认地址为 `http://127.0.0.1:8002`；一键安装会自动同步实际地址。

## 使用

从 EvoWiki 侧栏选择“开始学习”，输入目标和背景，按提示确认路径并完成摸底。生成页面后，可以阅读、互动、答题，或选中文字向助手提问。页面修改由你确认；复习任务显示在“今天”。

生成、对话和后台记忆整理可能产生模型费用。用量和预算可在设置中查看。模型配置与额外能力见 [项目 README](../README.md)。服务应保持本机访问；本地用户标识用于区分学习记录，不是云端账号认证。

## 构建检查

```powershell
npm run lint
npm run typecheck
npm test
npm run build
```

MIT 授权；依赖许可证见 [第三方声明](../THIRD_PARTY_NOTICES.md)。
