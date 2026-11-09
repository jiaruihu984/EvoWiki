# 运行与配置

Windows 11 x64 推荐使用 [README 的安装流程](../README.md#首次安装)。启动器准备锁定依赖、本地 PostgreSQL、浏览器和 Obsidian 插件。

## 常用命令

在项目目录的 PowerShell 中运行：

```powershell
uv run --isolated --no-project --with python-dotenv==1.2.4 python scripts/evowiki_run.py doctor
uv run --isolated --no-project --with python-dotenv==1.2.4 python scripts/evowiki_run.py up
uv run --isolated --no-project --with python-dotenv==1.2.4 python scripts/evowiki_run.py status
uv run --isolated --no-project --with python-dotenv==1.2.4 python scripts/evowiki_run.py down
```

`down --db` 同时停止本实例拥有的独立数据库进程。停止服务不会删除笔记、数据库或配置。升级、备份、恢复与卸载见 [README](../README.md)。

## 配置和数据

`init` 创建项目目录中的 `.env`。可以先启动，然后在 Obsidian 的“首次设置”中配置语言、模型和预算。也可按 `.env.example` 手动填写。不要提交或分享 `.env`。

默认笔记库为 `.evowiki-vault`。要接入已有 Obsidian 库，先用 Obsidian 打开一次，再设置 `EVOWIKI_VAULT` 为绝对路径并重启服务。插件会保留个人笔记；不会覆盖归属另一个实例的托管插件。

每个实例使用独立的数据目录和配置。`/health` 显示实例与提交，`/health/ready` 检查实际就绪条件，`/health/capabilities` 显示可用的模型和选配能力。端口被占用时，启动器会核对进程归属并选择可用端口，将地址同步给插件。

## 模型

支持的供应商、模型选择和价格文件见 [模型配置](multi-api.md)。模型推理需要自己的服务密钥。应用的软预算不替代供应商账户限额。

## CPU 与 GPU

默认使用 CPU 向量检索，不需要 NVIDIA 显卡。GPU 是选配：安装匹配的 NVIDIA 驱动，停止服务，将 `EVOWIKI_EMBED_PROVIDERS` 改为 `cuda`，再启动。启动器选择 GPU 依赖，并检查 ONNX Runtime 实际使用 CUDA；检查失败会报错，不会静默改用 CPU。

CPU 与 GPU 嵌入依赖不能同时安装；由启动器按配置选择。显存占用取决于向量模型与本地 OCR，避免同时常驻超过显存的模型。

## 本地 OCR 与联网能力

纯文本学习不要求 OCR。扫描资料可使用配置的视觉模型，或独立本地 OCR 环境。安装本地 OCR 前先查看当前配置和显存情况，不要将其依赖混入产品 Python 环境。

联网搜索需要 `TAVILY_API_KEY`；没有配置时关闭。轨迹外发默认关闭；可选 Phoenix 配置见 [可观测性](observability-phoenix.md)。

## 其他平台

源码及手动构建工具可用于进一步适配其他平台；当前一键安装支持范围是 Windows 11 x64，macOS/Linux 不承诺相同的安装体验。手动运行时请自行准备兼容的 PostgreSQL 与 pgvector，使用锁文件安装依赖并在项目目录执行迁移与服务命令。
