# 模型配置

对话输入框下方的模型菜单分别选择厂商、具体型号、思考强度。选择按账号保存，重开后恢复；对话、页面、记忆和 Bot 使用该具体型号。预算仍需首次确认，密钥保留在后端 `.env`，不会写入插件或返回给界面。

| 厂商 | 密钥 | 接口地址覆盖 | 默认型号 |
|---|---|---|---|
| Anthropic | ANTHROPIC_API_KEY | ANTHROPIC_BASE_URL | claude-sonnet-5-5 |
| DeepSeek | DEEPSEEK_API_KEY | DEEPSEEK_BASE_URL | deepseek-v4-pro |
| OpenAI | OPENAI_API_KEY | OPENAI_BASE_URL | gpt-5.2 |
| Google Gemini | GEMINI_API_KEY | GEMINI_BASE_URL | gemini-2.5-flash |
| Kimi | MOONSHOT_API_KEY | MOONSHOT_BASE_URL | kimi-k3.1-pro |
| 智谱 GLM | ZAI_API_KEY | ZAI_BASE_URL | glm-5.3 |
| OpenAI 兼容服务 | EVOWIKI_COMPATIBLE_API_KEY | EVOWIKI_COMPATIBLE_BASE_URL，必填 | EVOWIKI_COMPATIBLE_MODEL，必填 |

只配置需要的厂商即可。`EVOWIKI_RUNTIME_PROVIDER=auto` 按表中的顺序选择已配置项，也可显式指定 `anthropic/deepseek/openai/gemini/kimi/glm/openai_compatible`；显式指定缺配置时会报错。不同平台、区域、按量付费和 Coding 服务可能有不同地址，使用你的平台提供的地址和授权型号。

增加型号如 `EVOWIKI_OPENAI_MODELS=gpt-5.2,your-other-model`；默认型号用 `EVOWIKI_OPENAI_MODEL`。其余厂商对应 ANTHROPIC、DEEPSEEK、GEMINI、KIMI、GLM、COMPATIBLE 前缀，完整示例在根目录 `.env.example`。已知厂商的中转服务可设置该厂商的 BASE_URL，沿用其协议与模型能力；协议和能力未知的自定义服务只提供 API 默认思考，不擅自发送高强度或图片参数。

思考档位按型号提供。例如 GPT-5.2 有关闭/低/中/高/很高，GLM-5.3 和 Kimi K3 有低/高/最高，后两者不能完全关闭。新型号没有核验能力时只显示“默认”。目标规划和拆分保留原有的最低思考要求；整理、压缩等明确要求关闭思考的步骤使用该型号允许的最低档，不支持关闭的型号仍会产生思考费用。它们保持同一具体型号，不偷偷换厂商。RSI 改进者及明确指定的视觉专业模型继续按固定规则运行。旧的页面“快/好”开关已移除，统一在模型菜单选择。

## 价格和预算

已有价格的默认型号可以直接选择。新增或自定义型号缺价格时会显示原因并拒绝调用，不将未知费用算零，也不会关闭其他可用型号。Kimi 当前官方价格页面未能核验，需先提供价格；供应商实际结算以它的账单为准。

在本地 TOML 文件填写供应商的实际美元/百万 token 价格，然后设置 `EVOWIKI_PRICES_FILE=该文件绝对路径`，重启服务。字段格式见 `src/evowiki/llm/prices.toml`，键为 `provider:实际模型ID`；自定义服务使用 `openai_compatible:实际模型ID`。额外文件覆盖同名价格并补充其他型号。官方价格来源：[OpenAI GPT-5.2](https://developers.openai.com/api/docs/models/gpt-5.2)、[Gemini](https://ai.google.dev/gemini-api/docs/pricing)、[GLM](https://docs.z.ai/guides/overview/pricing)。

升级需正常停止、更新并启动后端，再重载插件。本次接口协议为 2，旧插件与新后端不混用；已保存的旧厂商级选择需要手动重新选具体型号，原会话保留。

## 验证范围

用真实 SDK 连接本地假接口，验证具体模型、思考参数、流式文本、工具往返、图片和用量；真实 PostgreSQL 验证保存、重启读取、只有一家新厂商时的服务构造、价格门控及模型权限错误的隔离。原生 Obsidian 的选择器与持久化另有实际走查记录。本轮不代表七家供应商的真实账户、额度及区域访问均已验证；没有为模型矩阵发付费请求。

2026-11-09 原生走查完成七类接口的 46 项 UI/请求检查，确认具体型号和思考参数实际到达本地接口，并验证重载后的持久化。矩阵初轮使用协议 1；接口变更升为协议 2 后，又实际验证账号门控、插件重载及 Kimi/Gemini 后台提炼。初次后台提炼因思考参数冲突失败，修复后分别发送最低允许档位，两条有效空提炼经 DBOS 发布、无失败 wrapup。有效空提炼只验证链路，不代表记忆提取质量；本轮实际付费调用 0、$0，假接口账目单独标为合成数据。原始证据保存在本机忽略目录 `.evowiki-run/multi-api-20261109/evidence/`。
