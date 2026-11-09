"""Report configured product features without sending paid or external requests."""

import os
from collections.abc import Mapping
from pathlib import Path

from evowiki.forge.sandbox.runtime import available as wasi_available
from evowiki.service.deps import Services


def capabilities(svc: Services, env: Mapping[str, str] = os.environ) -> dict:
    def feature(name: str, configured: bool, action: str, *, local_resource: bool = False) -> dict:
        detail = ("已就绪" if local_resource else "已配置") if configured else action
        return {"name": name, "configured": configured, "detail": detail}

    deep = getattr(svc, "deep", None)
    library = getattr(svc, "library", None)
    local_layout = env.get("EVOWIKI_LAYOUT_PYTHON", "")
    rows = [
        feature("学习规划与助手", getattr(svc, "gateway", None) is not None,
                "在 .env 填写供应商密钥（ANTHROPIC_API_KEY、DEEPSEEK_API_KEY、OPENAI_API_KEY、GEMINI_API_KEY、MOONSHOT_API_KEY、ZAI_API_KEY 或 EVOWIKI_COMPATIBLE_API_KEY），检查 EVOWIKI_RUNTIME_PROVIDER 后重新启动"),
        feature("页面生成", deep is not None and deep.llm is not None,
                "检查 .env 的模型密钥和后端日志中的页面生成配置错误，然后重新启动"),
        feature("预置交互组件", library is not None and bool(library.list()),
                "运行 uv run --no-sync --env-file .env python -m evowiki.service prepare 初始化组件库",
                local_resource=True),
        feature("联网搜索", bool(env.get("TAVILY_API_KEY")), "选配：在 .env 填写 TAVILY_API_KEY"),
        feature("扫描页识别",
                bool(env.get("ANTHROPIC_API_KEY"))
                or bool(local_layout and Path(local_layout).is_file()),
                "选配：填写 ANTHROPIC_API_KEY；或按 docs/runtime.md 安装本地解析，再设置 EVOWIKI_LAYOUT_PYTHON"),
        feature("新工具运行环境", wasi_available(),
                "选配：运行 uv run --no-sync python scripts/fetch_wasi_python.py", local_resource=True),
    ]
    return {"features": rows, "model_access_checked": False,
            "notice": "配置检查不会外发请求；密钥有效性、模型权限、余额和扫描识别运行情况在首次实际使用时验证"}
