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

import re


class RAGFlowPdfParser:
    """PDF 解析器 — 学习版最小占位。

    ⚠️ 官方 v0.24.0 全量 1800+ 行(OCR / 版面识别 / 表格抽取 / 章节切分 / crop 等),
    本文件仅移植 rag/nlp naive_merge 家族依赖的 remove_tag 静态方法;
    其余方法见官方源码, 等 PDF 解析链需要时再逐方法补齐。
    """

    def __init__(self, **kwargs):
        """⚠️ 官方在此初始化 OCR/版面识别模型(见 v0.24.0 pdf_parser.py:56), 学习版空实现。"""
        pass

    @staticmethod
    def remove_tag(txt):
        """去除文本中的 PDF 位置标记(形如 "@@页码,x0,y0,x1,y1##"), 只保留正文。

        位置标记由 crop/add_positions 追加到文本尾部; naive_merge 在拼接重叠块前
        调用本方法, 防止上一块尾部的位置串被截半后残留进新块开头。
        """
        return re.sub(r"@@[\t0-9.-]+?##", "", txt)