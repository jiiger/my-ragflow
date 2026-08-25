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
"""HTML 解析器:把网页 DOM 树按"块(block)"语义抽成纯文本 + 表格,再按 token 数切块。

移植自官方 RAGFlow v0.24.0 deepdoc/parser/html_parser.py(逻辑 1:1)。

核心思路:
- 先用 html5lib 解析成 DOM,清掉 style/script/注释/内联样式等噪音;
- 递归遍历 DOM:遇到 <table> 整体保留 HTML 原文(后面单独作为一块),
  其余文本按 BLOCK_TAGS(h1~h6/p/div/li/table 等)划分 block_id;
- 同一 block_id 内的文本合并成一段(标题标签自动加 '# '~'######' 前缀),
  跨块的文本再按 token 数聚成最终 chunk;
- 所有"块大小"都用 rag_tokenizer.tokenize 分词后的 token 数衡量。
"""

import html
import uuid

import chardet
from bs4 import BeautifulSoup
from bs4.element import Comment, NavigableString, Tag

from rag.nlp import find_codec, rag_tokenizer


def get_encoding(file):
    """用 chardet 探测文件编码,供 __call__ 读文件时用。"""
    with open(file, 'rb') as f:
        tmp = chardet.detect(f.read())
    return tmp['encoding']


# 命中这些标签视为"块边界":每遇到一个就开一个新 block_id
BLOCK_TAGS = [
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "div", "article", "section", "aside",
    "ul", "ol", "li",
    "table", "pre", "code", "blockquote",
    "figure", "figcaption"
]
# 标题标签 → chunk 里的 Markdown 级前缀(官方原样,注意 h4/h5 都是 5 个 #)
TITLE_TAGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "#####", "h5": "#####", "h6": "######"}


class RAGFlowHtmlParser:
    def __call__(self, fnm, binary=None, chunk_token_num=512):
        """入口:传文件路径或二进制;自动探测编码读出 HTML 文本再交给 parser_txt。"""
        if binary:
            encoding = find_codec(binary)
            txt = binary.decode(encoding, errors="ignore")
        else:
            with open(fnm, "r", encoding=get_encoding(fnm)) as f:
                txt = f.read()
        return self.parser_txt(txt, chunk_token_num)

    @classmethod
    def parser_txt(cls, txt, chunk_token_num):
        """纯文本入口:清洗 DOM → 递归取文本 → 合并块 → 切块,返回字符串列表。"""
        if not isinstance(txt, str):
            raise TypeError("txt type should be string!")

        # 一级"段落"暂存区:read_text_recursively 的产物,每个元素形如
        # {"content": 文本, "tag_name": 标签名, "metadata": {"block_id": ...}} 或表格信息
        temp_sections = []
        soup = BeautifulSoup(txt, "html5lib")
        # 删 <style>/<script> 标签
        for style_tag in soup.find_all(["style", "script"]):
            style_tag.decompose()
        # 再删 <div> 内残留的 <script>(html5lib 可能把 script 内容塞进 body 文本)
        for div_tag in soup.find_all("div"):
            for script_tag in div_tag.find_all("script"):
                script_tag.decompose()
        # 删内联 style 属性
        for tag in soup.find_all(True):
            if 'style' in tag.attrs:
                del tag.attrs['style']
        # 删 HTML 注释
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        cls.read_text_recursively(soup.body, temp_sections, chunk_token_num=chunk_token_num)
        block_txt_list, table_list = cls.merge_block_text(temp_sections)
        sections = cls.chunk_block(block_txt_list, chunk_token_num=chunk_token_num)
        # 表格不做文本切块,整表(按行按 token 拆过的子表)原样作为一块追加
        for table in table_list:
            sections.append(table.get("content", ""))
        return sections

    @classmethod
    def split_table(cls, html_table, chunk_token_num=512):
        """把一张大表按"行累计 token 数"拆成多张子表,每张都仍是一个完整 <table>。

        注意:官方该方法是独立的工具方法(按行切表),parser_txt 主流程并未调用它,
        merge_block_text 阶段整表直接进 table_list。这里 1:1 保留。
        """
        soup = BeautifulSoup(html_table, "html.parser")
        rows = soup.find_all("tr")
        tables = []          # 每项是一组行的列表
        current_table = []
        current_count = 0
        table_str_list = []
        for row in rows:
            tks_str = rag_tokenizer.tokenize(str(row))
            token_count = len(tks_str.split(" ")) if tks_str else 0
            if current_count + token_count > chunk_token_num:
                tables.append(current_table)
                current_table = []
                current_count = 0
            current_table.append(row)
            current_count += token_count
        if current_table:
            tables.append(current_table)

        for table_rows in tables:
            new_table = soup.new_tag("table")
            for row in table_rows:
                new_table.append(row)
            table_str_list.append(str(new_table))

        return table_str_list

    @classmethod
    def read_text_recursively(cls, element, parser_result, chunk_token_num=512, parent_name=None, block_id=None):
        """递归遍历 DOM 节点,把可读文本压进 parser_result,把表格原样抽出。

        - NavigableString(纯文本):strip 后非空才要;若它本身又含 HTML(如属性里的
          碎片)就再包一层 BeautifulSoup 递归处理,否则记为一条 info;标签名继承父节点。
        - Tag:<table> 直接整体收走(带 uuid 的 table_id 和索引);BLOCK_TAGS 里的标签
          开新 block_id 传给子树;其他标签只是向下传递当前 block_id。
        """
        if isinstance(element, NavigableString):
            content = element.strip()

            def is_valid_html(content):
                try:
                    soup = BeautifulSoup(content, "html.parser")
                    return bool(soup.find())
                except Exception:
                    return False

            return_info = []
            if content:
                if is_valid_html(content):
                    # 文本节点里还嵌着 HTML,再包一层递归展开
                    soup = BeautifulSoup(content, "html.parser")
                    child_info = cls.read_text_recursively(soup, parser_result, chunk_token_num, element.name, block_id)
                    parser_result.extend(child_info)
                else:
                    info = {"content": element.strip(), "tag_name": "inner_text", "metadata": {"block_id": block_id}}
                    if parent_name:
                        info["tag_name"] = parent_name
                    return_info.append(info)
            return return_info
        elif isinstance(element, Tag):

            if str.lower(element.name) == "table":
                # 表格:整表 HTML 原文作为一块,带 table_id + 序号
                table_info_list = []
                table_id = str(uuid.uuid1())
                table_list = [html.unescape(str(element))]
                for t in table_list:
                    table_info_list.append({"content": t, "tag_name": "table",
                                            "metadata": {"table_id": table_id, "index": table_list.index(t)}})
                return table_info_list
            else:
                if str.lower(element.name) in BLOCK_TAGS:
                    block_id = str(uuid.uuid1())
                for child in element.children:
                    child_info = cls.read_text_recursively(child, parser_result, chunk_token_num, element.name,
                                                           block_id)
                    parser_result.extend(child_info)
        return []

    @classmethod
    def merge_block_text(cls, parser_result):
        """把 read_text_recursively 的散条目按 block_id 合并成段落文本。

        - 同一 block_id 的文本用空格拼接(标题加 Markdown 前缀);
        - 换 block_id 时把上一段收尾进 block_content;
        - 无 block_id 的杂散文本直接追加到当前段;表格条目单独收进 table_info_list。
        """
        block_content = []
        current_content = ""
        table_info_list = []
        last_block_id = None
        for item in parser_result:
            content = item.get("content")
            tag_name = item.get("tag_name")
            title_flag = tag_name in TITLE_TAGS
            block_id = item.get("metadata", {}).get("block_id")
            if block_id:
                if title_flag:
                    content = f"{TITLE_TAGS[tag_name]} {content}"
                if last_block_id != block_id:
                    if last_block_id is not None:
                        block_content.append(current_content)
                    current_content = content
                    last_block_id = block_id
                else:
                    current_content += (" " if current_content else "") + content
            else:
                if tag_name == "table":
                    table_info_list.append(item)
                else:
                    current_content += (" " if current_content else "") + content
        if current_content:
            block_content.append(current_content)
        return block_content, table_info_list

    @classmethod
    def chunk_block(cls, block_txt_list, chunk_token_num=512):
        """把合并好的块列表按 token 数聚成最终 chunk。

        单块超限:先落盘当前块,再把该块按 token 直接切成 chunk_token_num 的等份
        (用分词后的 token 串拼回,是官方原样语义);否则尽量把相邻小块并进同一 chunk。
        """
        chunks = []
        current_block = ""
        current_token_count = 0

        for block in block_txt_list:
            tks_str = rag_tokenizer.tokenize(block)
            block_token_count = len(tks_str.split(" ")) if tks_str else 0
            if block_token_count > chunk_token_num:
                if current_block:
                    chunks.append(current_block)
                start = 0
                tokens = tks_str.split(" ")
                while start < len(tokens):
                    end = start + chunk_token_num
                    split_tokens = tokens[start:end]
                    chunks.append(" ".join(split_tokens))
                    start = end
                current_block = ""
                current_token_count = 0
            else:
                if current_token_count + block_token_count <= chunk_token_num:
                    current_block += ("\n" if current_block else "") + block
                    current_token_count += block_token_count
                else:
                    chunks.append(current_block)
                    current_block = block
                    current_token_count = block_token_count

        if current_block:
            chunks.append(current_block)

        return chunks