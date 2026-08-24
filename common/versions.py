"""
common.versions — 版本号获取。

优先读仓库根的 VERSION 文件, 不存在则用 git describe 取最近的 v* tag 与提交计数。
来源: 官方 common/versions.py @ v0.24.0
"""
import os
import subprocess

RAGFLOW_VERSION_INFO = "unknown"


def get_ragflow_version() -> str:
    """返回 RAGFlow 版本号(带缓存, 首次读取后不再变)。"""
    global RAGFLOW_VERSION_INFO
    if RAGFLOW_VERSION_INFO != "unknown":
        return RAGFLOW_VERSION_INFO
    version_path = os.path.abspath(
        os.path.join(
            os.path.dirname(os.path.realpath(__file__)), os.pardir, "VERSION"
        )
    )
    if os.path.exists(version_path):
        with open(version_path, "r") as f:
            RAGFLOW_VERSION_INFO = f.read().strip()
    else:
        RAGFLOW_VERSION_INFO = get_closest_tag_and_count()
    return RAGFLOW_VERSION_INFO


def get_closest_tag_and_count():
    """git describe 兜底: v<tag>-<count>-g<commit> 或 unknown。"""
    try:
        # Get the current commit hash
        version_info = (
            subprocess.check_output(["git", "describe", "--tags", "--match=v*", "--first-parent", "--always"])
            .strip()
            .decode("utf-8")
        )
        return version_info
    except Exception:
        return "unknown"