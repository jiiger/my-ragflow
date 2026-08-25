# -*- coding: utf-8 -*-
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
"""Markdown 解析器:两个协作的工具类,把 Markdown 文本拆成"表格 + 正文块"。

移植自官方 RAGFlow v0.24.0 deepdoc/parser/markdown_parser.py(逻辑 1:1)。
注意:官方这两个类本身没有 __call__ 入口,真正的主流程在 rag/app/naive.py 的
class Markdown(MarkdownParser)(读取 → 抽表格 → 按行提元素 → 产出 (文本, "") 二元组),
本文件只负责两类原子能力:

1. RAGFlowMarkdownParser.extract_tables_and_remainder:
   - 正则识别三类表格:标准 Markdown 表(| 表头 | 分隔行 | 数据行)、无边框表、
     HTML 表(<table> 及其 html/body 包装),原样收进 tables 列表;
   - separate_tables=True(默认)时表格从正文里挖掉,只留 "\n\n" 占位;
     False 时标准 Markdown 表用 markdown 库渲染成 HTML 塞回正文;
   - 顺带把 <table>/<td>/<tr> 等带属性标签归一成裸标签。

2. MarkdownElementExtractor.extract_elements:
   - 传 delimiter 时按自定义分隔符(反引号 `xxx` 包裹,多分隔符按长度降序)整体切段;
   - 不传时逐行扫描,把行流分成 header(#{1,6})/code block(```)/列表/引用/普通文本
     五种块元素;include_meta=True 时每个元素带 start_line/end_line 行号,
     供 naive.py 按行号把图片引用挂到对应块上。
"""

import re

from markdown import markdown


class RAGFlowMarkdownParser:
    def __init__(self, chunk_token_num=128):
        # 官方此处保存了 chunk_token_num,但 extract_tables_and_remainder 并不消费它,
        # 1:1 保留,由 naive.py 的 Markdown 子类在解析链里使用
        self.chunk_token_num = int(chunk_token_num)

    def extract_tables_and_remainder(self, markdown_text, separate_tables=True):
        """抽走正文里的表格,返回 (剩余正文, 表格列表)。

        separate_tables=True 时表格从正文中删除(避免下游重复切块),False 时
        标准 Markdown 表会被渲染成 HTML 保留在正文里(仍会拷一份进 tables)。
        """
        tables = []
        working_text = markdown_text

        def replace_tables_with_rendered_html(pattern, table_list, render=True):
            """按 pattern 找到表格,收进 table_list;separate_tables 决定正文里留不留。"""
            new_text = ""
            last_end = 0
            for match in pattern.finditer(working_text):
                raw_table = match.group()
                table_list.append(raw_table)
                if separate_tables:
                    # 从正文里挖掉表格,留双换行占位
                    new_text += working_text[last_end: match.start()] + "\n\n"
                else:
                    # 用 markdown 库渲染成 HTML 表格塞回原位
                    html_table = markdown(raw_table, extensions=["markdown.extensions.tables"]) if render else raw_table
                    new_text += working_text[last_end: match.start()] + html_table + "\n\n"
                last_end = match.end()
            new_text += working_text[last_end:]
            return new_text

        if "|" in markdown_text:  # 没竖线直接跳过两个 md 表格正则以省性能
            # 标准 Markdown 表格:表头行 + 分隔行(| --- |)+ 至少一行数据
            border_table_pattern = re.compile(
                r"""
                (?:\n|^)
                (?:\|.*?\|.*?\|.*?\n)
                (?:\|(?:\s*[:-]+[-| :]*\s*)\|.*?\n)
                (?:\|.*?\|.*?\|.*?\n)+
            """,
                re.VERBOSE,
            )
            working_text = replace_tables_with_rendered_html(border_table_pattern, tables)

            # 无边框 Markdown 表格:行内只要含 | 即可,同样需要分隔行
            no_border_table_pattern = re.compile(
                r"""
                (?:\n|^)
                (?:\S.*?\|.*?\n)
                (?:(?:\s*[:-]+[-| :]*\s*).*?\n)
                (?:\S.*?\|.*?\n)+
                """,
                re.VERBOSE,
            )
            working_text = replace_tables_with_rendered_html(no_border_table_pattern, tables)

        # 把 <table class="..."> 这类带属性的标签归一成裸 <table>
        TAGS = ["table", "td", "tr", "th", "tbody", "thead", "div"]
        table_with_attributes_pattern = re.compile(rf"<(?:{'|'.join(TAGS)})[^>]*>", re.IGNORECASE)

        def replace_tag(m):
            matched = re.match(r"<(\w+)", m.group())
            assert matched is not None  # 外层正则已保证以 <标签 开头
            return "<{}>".format(matched.group(1))

        working_text = re.sub(table_with_attributes_pattern, replace_tag, working_text)

        if "<table>" in working_text.lower():  # 优化:无 <table> 就跳过 HTML 表正则
            # HTML 表格:兼容 <html><body> 包装、<body> 包装、裸 <table> 三种形态
            html_table_pattern = re.compile(
                r"""
            (?:\n|^)
            \s*
            (?:
                # case1: <html><body><table>...</table></body></html>
                (?:<html[^>]*>\s*<body[^>]*>\s*<table[^>]*>.*?</table>\s*</body>\s*</html>)
                |
                # case2: <body><table>...</table></body>
                (?:<body[^>]*>\s*<table[^>]*>.*?</table>\s*</body>)
                |
                # case3: only<table>...</table>
                (?:<table[^>]*>.*?</table>)
            )
            \s*
            (?=\n|$)
            """,
                re.VERBOSE | re.DOTALL | re.IGNORECASE,
            )

            def replace_html_tables():
                nonlocal working_text  # 这里才真正改写外层 working_text
                new_text = ""
                last_end = 0
                for match in html_table_pattern.finditer(working_text):
                    raw_table = match.group()
                    tables.append(raw_table)
                    if separate_tables:
                        new_text += working_text[last_end: match.start()] + "\n\n"
                    else:
                        new_text += working_text[last_end: match.start()] + raw_table + "\n\n"
                    last_end = match.end()
                new_text += working_text[last_end:]
                working_text = new_text

            replace_html_tables()

        return working_text, tables


class MarkdownElementExtractor:
    """按行扫描 Markdown,把它切成 header/code/list/blockquote/text 五种块元素。"""

    def __init__(self, markdown_content):
        self.markdown_content = markdown_content
        self.lines = markdown_content.split("\n")

    def get_delimiters(self, delimiters):
        """解析自定义分隔符参数:反引号 `xxx` 里的内容各算一条,长的优先,| 连接成正则。

        例如 "`##` `---`" → "##|---"(均已 re.escape),供 extract_elements 整体切分。
        """
        toks = re.findall(r"`([^`]+)`", delimiters)
        toks = sorted(set(toks), key=lambda x: -len(x))
        return "|".join(re.escape(t) for t in toks if t)

    def extract_elements(self, delimiter=None, include_meta=False) -> list:
        """提取元素(标题、代码块、列表等)。delimiter 存在就走正则整体切分,否则逐行扫描。"""
        sections = []

        i = 0
        dels = ""
        if delimiter:
            dels = self.get_delimiters(delimiter)
        if len(dels) > 0:
            # 自定义分隔符路径:按正则切整篇文本
            text = "\n".join(self.lines)
            if include_meta:
                # 保留每个切段的行号范围(start_line/end_line)
                pattern = re.compile(dels)
                last_end = 0
                for m in pattern.finditer(text):
                    part = text[last_end: m.start()]
                    if part and part.strip():
                        sections.append(
                            {
                                "content": part.strip(),
                                "start_line": text.count("\n", 0, last_end),
                                "end_line": text.count("\n", 0, m.start()),
                            }
                        )
                    last_end = m.end()

                part = text[last_end:]
                if part and part.strip():
                    sections.append(
                        {
                            "content": part.strip(),
                            "start_line": text.count("\n", 0, last_end),
                            "end_line": text.count("\n", 0, len(text)),
                        }
                    )
            else:
                # 官方语义:re.split 不带捕获组,分隔符本身会被丢进切分结果外
                parts = re.split(dels, text)
                sections = [p.strip() for p in parts if p and p.strip()]
            return sections
        # 逐行扫描路径
        while i < len(self.lines):
            line = self.lines[i]

            if re.match(r"^#{1,6}\s+.*$", line):
                # header
                element = self._extract_header(i)
                sections.append(element if include_meta else element["content"])
                i = element["end_line"] + 1
            elif line.strip().startswith("```"):
                # code block
                element = self._extract_code_block(i)
                sections.append(element if include_meta else element["content"])
                i = element["end_line"] + 1
            elif re.match(r"^\s*[-*+]\s+.*$", line) or re.match(r"^\s*\d+\.\s+.*$", line):
                # list block
                element = self._extract_list_block(i)
                sections.append(element if include_meta else element["content"])
                i = element["end_line"] + 1
            elif line.strip().startswith(">"):
                # blockquote
                element = self._extract_blockquote(i)
                sections.append(element if include_meta else element["content"])
                i = element["end_line"] + 1
            elif line.strip():
                # text block(普通段落,一直到下一个块元素为止)
                element = self._extract_text_block(i)
                sections.append(element if include_meta else element["content"])
                i = element["end_line"] + 1
            else:
                i += 1

        if include_meta:
            sections = [section for section in sections if section["content"].strip()]
        else:
            sections = [section for section in sections if section.strip()]
        return sections

    def _extract_header(self, start_pos):
        """标题:单行,与 markdown 语法一致。"""
        return {
            "type": "header",
            "content": self.lines[start_pos],
            "start_line": start_pos,
            "end_line": start_pos,
        }

    def _extract_code_block(self, start_pos):
        """代码块:从 ``` 开始,一直到下一个 ``` 结束(含围栏行)。"""
        end_pos = start_pos
        content_lines = [self.lines[start_pos]]

        # Find the end of the code block
        for i in range(start_pos + 1, len(self.lines)):
            content_lines.append(self.lines[i])
            end_pos = i
            if self.lines[i].strip().startswith("```"):
                break

        return {
            "type": "code_block",
            "content": "\n".join(content_lines),
            "start_line": start_pos,
            "end_line": end_pos,
        }

    def _extract_list_block(self, start_pos):
        """列表块:连续的列表项;允许中间空行、缩进子项、缩进续行。"""
        end_pos = start_pos
        content_lines = []

        i = start_pos
        while i < len(self.lines):
            line = self.lines[i]
            # check if this line is a list item or continuation of a list
            if (
                re.match(r"^\s*[-*+]\s+.*$", line)
                or re.match(r"^\s*\d+\.\s+.*$", line)
                or (i > start_pos and not line.strip())
                or (i > start_pos and re.match(r"^\s{2,}[-*+]\s+.*$", line))
                or (i > start_pos and re.match(r"^\s{2,}\d+\.\s+.*$", line))
                or (i > start_pos and re.match(r"^\s+\w+.*$", line))
            ):
                content_lines.append(line)
                end_pos = i
                i += 1
            else:
                break

        return {
            "type": "list_block",
            "content": "\n".join(content_lines),
            "start_line": start_pos,
            "end_line": end_pos,
        }

    def _extract_blockquote(self, start_pos):
        """引用块:连续的 > 行,允许中间空行。"""
        end_pos = start_pos
        content_lines = []

        i = start_pos
        while i < len(self.lines):
            line = self.lines[i]
            if line.strip().startswith(">") or (i > start_pos and not line.strip()):
                content_lines.append(line)
                end_pos = i
                i += 1
            else:
                break

        return {
            "type": "blockquote",
            "content": "\n".join(content_lines),
            "start_line": start_pos,
            "end_line": end_pos,
        }

    def _extract_text_block(self, start_pos):
        """普通文本块:收集到下一个块元素(标题/代码/列表/引用)为止。

        空行本身不算块元素——它后面跟的还是文本就继续收,跟的是块元素才停。
        """
        end_pos = start_pos
        content_lines = [self.lines[start_pos]]

        i = start_pos + 1
        while i < len(self.lines):
            line = self.lines[i]
            # stop if we encounter a block element
            if re.match(r"^#{1,6}\s+.*$", line) or line.strip().startswith("```") or re.match(r"^\s*[-*+]\s+.*$", line) or re.match(r"^\s*\d+\.\s+.*$", line) or line.strip().startswith(">"):
                break
            elif not line.strip():
                # check if the next line is a block element
                if i + 1 < len(self.lines) and (
                    re.match(r"^#{1,6}\s+.*$", self.lines[i + 1])
                    or self.lines[i + 1].strip().startswith("```")
                    or re.match(r"^\s*[-*+]\s+.*$", self.lines[i + 1])
                    or re.match(r"^\s*\d+\.\s+.*$", self.lines[i + 1])
                    or self.lines[i + 1].strip().startswith(">")
                ):
                    break
                else:
                    content_lines.append(line)
                    end_pos = i
                    i += 1
            else:
                content_lines.append(line)
                end_pos = i
                i += 1

        return {
            "type": "text_block",
            "content": "\n".join(content_lines),
            "start_line": start_pos,
            "end_line": end_pos,
        }