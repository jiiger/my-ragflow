"""parser_config_utils.py — 解析器配置归一化, 对齐官方 v0.24.0(33 行 1:1)。

normalize_layout_recognizer: 把 layout_recognize 配置里 "模型名@mineru"/"模型名@paddleocr"
 拆成 (识别器名, 模型名), 供 naive chunk 的 pdf 分支选解析器; 无 @ 后缀原样返回。
"""

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

from typing import Any


def normalize_layout_recognizer(layout_recognizer_raw: Any) -> tuple[Any, str | None]:
    parser_model_name: str | None = None
    layout_recognizer = layout_recognizer_raw

    if isinstance(layout_recognizer_raw, str):
        lowered = layout_recognizer_raw.lower()
        if lowered.endswith("@mineru"):
            parser_model_name = layout_recognizer_raw.rsplit("@", 1)[0]
            layout_recognizer = "MinerU"
        elif lowered.endswith("@paddleocr"):
            parser_model_name = layout_recognizer_raw.rsplit("@", 1)[0]
            layout_recognizer = "PaddleOCR"

    return layout_recognizer, parser_model_name
