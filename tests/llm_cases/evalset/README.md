# RSI 回归评测

该目录提供 RSI 的回归用例和评测工具。评测与普通学习使用不同数据库；不会把缺少的数据或没有运行的用例计为通过。

## 环境

在当前仓库安装独立评测环境：

```powershell
uv venv .eval-venv
uv pip install --python .eval-venv/Scripts/python.exe -e ".[llm,embed]" -r tests/llm_cases/evalset/requirements.txt
```

设置 `EVOWIKI_EVALSET_PYTHON` 为该解释器的绝对路径，才会启用需要模型的评测门控。单独安装依赖不会自动运行付费评测。需要自己的模型配置、独立测试 PostgreSQL/pgvector，以及准备好的 `audit_base` 测试模板。测试端口通过 `EVOWIKI_EVALSET_TEST_PORT` 指定，不能指向学习数据库。

## 数据与保护

默认数据放在当前仓库 `.evowiki-run/evalset/`；可用 `EVOWIKI_EVALSET_DATA_DIR` 指定绝对路径。密封集独立放在 `.evowiki-run/evalset-sealed/`，可用 `EVOWIKI_SEALED_ROOT` 指定。评测器与代码检查子进程使用同一保护路径。

评测数据、基线、模型轨迹与密封集不随源码分发。需要的本地材料缺失时明确报告未运行；没有可比基线或未完成轨迹时，门控不会通过。回归通过也不等于真实上线效果已经得到证明。

## 命令

```powershell
.eval-venv/Scripts/python.exe -m tests.llm_cases.evalset.run --help
.eval-venv/Scripts/python.exe -m tests.llm_cases.evalset.run run --note baseline
.eval-venv/Scripts/python.exe -m tests.llm_cases.evalset.run compare
```

`run` 会调用付费模型。运行前先确认用例、模型和费用；不要为了得到更漂亮的结果重复运行。
