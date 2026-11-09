"""写代码模型的简短导航；说明公开的产品结构和代码安全约定。"""

PROJECT_CONTEXT = """## 项目地图（路径相对 src/evowiki/）
- wikiapp/write_queue.py：写页排队和写入闸门；wikiapp/：应用层协调。
- assistant/tools/：陪读助手工具；assistant/：助手对话循环和上下文。
- bot/actions.py：动作账本、批准和执行；bot/：后台任务和设置。
- deep/：写页面的模型流程和工具；render/：页面渲染。
- content/、sources/：内容结构和来源；wiki/、goals/、nodes/：页面、目标和节点。
- store/：数据库存取与迁移；service/：HTTP 路由。
- harness/：改进者、重放和门控；forge/：工具生成和代码补丁构建。
地图只用于定位，不扩大可修改范围；以工具的路径限制为准。

## 产品代码约定
- 测试默认用假 LLM、假搜索或录制结果，不反复调用付费 API。
- 只跑相关测试，并做 ruff check；不顺手改无关部分。
- 不改模拟器 import 的文件；experiments/、rsib/ 只读，不跑正式实验、不选 seed。
- 失败要明确报错或退回已有结果，不能静默；不要碰用户自己的 Obsidian 库。
- 密钥不写入代码、日志和回复。
"""
