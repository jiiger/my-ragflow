#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#


def get_float(v):
    """把任意值转成 float, None/异常时返回负无穷(排序兜底)。

    官方原函数(v0.24.0 common/float_utils.py), 逻辑与 docstring 一致。
    """
    if v is None:
        return float("-inf")
    try:
        return float(v)
    except Exception:
        return float("-inf")


def normalize_overlapped_percent(overlapped_percent):
    """归一化块间重叠率: 0~1 的小数放大为 0~100 的百分数, 并夹在 [0, 90]。

    参数:
        overlapped_percent: 用户配置的重叠率(可传 "20"、"0.2" 等字符串
            —— 前端表单常传字符串)
    返回:
        int: 0~90 的重叠百分数; 非法输入返回 0
    注意: 官方上限 90 —— 重叠率不会超过 90%, 防止切块退化成整篇复制。
    """
    try:
        value = float(overlapped_percent)
    except (TypeError, ValueError):
        return 0
    if 0 < value < 1:
        value *= 100
    value = int(value)
    return max(0, min(value, 90))