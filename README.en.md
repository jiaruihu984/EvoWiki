# EvoWiki

[中文](README.md) · English

EvoWiki is a personal learning wiki for Obsidian Desktop. Set a learning goal, describe your background, and study explanations, examples and questions in local Markdown notes. Learning records are stored in local PostgreSQL.

The one-click launcher supports Windows 11 x64. CPU operation is the default. GPU, web search and local OCR are optional. Learning uses your model provider account and incurs that provider's charges.

## Install and start

Install [Git for Windows](https://git-scm.com/install/windows), [uv](https://docs.astral.sh/uv/getting-started/installation/), [Node.js 22 or newer supported LTS with npm](https://nodejs.org/en/download), [Obsidian Desktop 1.15 or newer](https://obsidian.md/download), and the [Microsoft Visual C++ v14 x64 runtime](https://aka.ms/vc14/vc_redist.x64.exe). Use the complete official Git installer: local PostgreSQL needs its runtime DLLs. Reopen PowerShell after installing.

Choose a project folder, for example on drive D:

```powershell
git clone https://github.com/jiaruihu984/EvoWiki-product.git EvoWiki
cd EvoWiki
uv run --isolated --no-project --with python-dotenv==1.2.4 python scripts/evowiki_run.py init --language en
uv run --isolated --no-project --with python-dotenv==1.2.4 python scripts/evowiki_run.py doctor --language en
uv run --isolated --no-project --with python-dotenv==1.2.4 python scripts/evowiki_run.py up --language en
```

Use `--language en` or `--language zh` to choose launcher messages; without it, the launcher follows the system locale. The chosen language is inherited by its child control processes. Chinese characters and spaces in folder names are supported. Configuration files use UTF-8. The launcher downloads Python dependencies, PostgreSQL and Chromium when needed; expect several GB of disk use. The local embedding model downloads on first use. Installation checks and setup checks do not make paid model requests.

`init` creates `.env` and an independent empty Obsidian vault at `.evowiki-vault`, preserving existing files. You can start without an API key. When the local service is ready, open the printed vault path in Obsidian and enable EvoWiki. Complete **Initial setup** to choose English or Chinese, configure a model provider, review your background, budget and automation permissions. A missing model key means setup is unfinished; it does not mean the local installation is broken.

Supported providers are Anthropic, DeepSeek, OpenAI, Gemini, Kimi, GLM and custom OpenAI-compatible services. The model menu selects the concrete model and reasoning level. An API key is sent to the provider address you configure, so check that address carefully. Custom model prices must be supplied before the model can be used. See [local setup and recovery](docs/first-use-setup.en.md) and [model configuration](docs/multi-api.md).

New accounts start with a $5 daily and $30 monthly application budget, which must be reviewed and confirmed before paid work. Existing budgets remain unchanged. These limits use recorded charges and cannot replace provider account limits: requests already in progress can finish after a limit is reached. Automatic RSI, idle page writing and generated code are initially disabled. Background extraction and organization can also use the selected model and incur charges.

## Daily use

Double-click **EvoWiki.exe** to start or reuse the owned healthy service. Choose **Stop service and exit** from its tray menu to drain and stop it. Restart or reload the Obsidian plugin after installation or an update.

```powershell
uv run --isolated --no-project --with python-dotenv==1.2.4 python scripts/evowiki_run.py status --language en
uv run --isolated --no-project --with python-dotenv==1.2.4 python scripts/evowiki_run.py down --language en
git pull --ff-only
uv run --isolated --no-project --with python-dotenv==1.2.4 python scripts/evowiki_run.py up --language en
```

The launcher verifies process ownership before stopping or replacing a runtime. It does not stop other applications. The normal backend port is 8002; if it is occupied, the launcher selects another available port and updates the plugin address.

## Data and backups

Notes and the database stay local. Planning, explanations, assistant responses, feedback interpretation and background organization send the task's relevant context to the selected model service. Web search sends search terms to Tavily when configured. Embedding inference runs locally. Remote tracing is off in new configuration. The service is for personal local use; a local username selects records and is not an Internet authentication mechanism.

Database backups run every 24 hours and retain the latest 14 copies by default. Notes require their own backup. Configuration secrets are excluded from database backups. Keep API keys only in the instance's main `.env`; do not paste them into notes, issues, screenshots or chat.

```powershell
uv run --isolated --no-project --with python-dotenv==1.2.4 python scripts/evowiki_run.py backup
uv run --isolated --no-project --with python-dotenv==1.2.4 python scripts/evowiki_run.py backups
```

Use `doctor` for installation checks, `status` for runtime ownership and versions, and the plugin's usage and backup settings for account state. Logs are in `.evowiki-run/backend.log`; share only relevant redacted errors. For advanced operation and restore procedures, see [runtime documentation](docs/runtime.md) and the [Chinese README](README.md).

Double-click `EvoWiki.exe` to run EvoWiki in the Windows notification area. Right-click its icon to open the vault, inspect status and logs, start, stop or restart. **Stop service and exit** drains the backend and stops this installation's owned database.

See [desktop source and build provenance](desktop/README.md) for source, build instructions and file hashes. `EvoWiki.exe` is unsigned; use normal security review without disabling protection or adding exclusions.
