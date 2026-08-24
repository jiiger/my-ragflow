"""
rag.prompts.template — 提示词模板加载器。

PROMPT_DIR 指向本目录, load_prompt(name) 按 <name>.md 读取并缓存;
generator.py 顶部用它对全部 34 个模板做一次性加载。
来源: 官方 rag/prompts/template.py @ v0.24.0
"""
import os

PROMPT_DIR = os.path.dirname(__file__)

_loaded_prompts = {}


def load_prompt(name: str) -> str:
    """读取 prompts/<name>.md 的内容(带内存缓存, 重复调用直接返回)。"""
    if name in _loaded_prompts:
        return _loaded_prompts[name]

    path = os.path.join(PROMPT_DIR, f"{name}.md")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Prompt file '{name}.md' not found in prompts/ directory.")

    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        _loaded_prompts[name] = content
        return content