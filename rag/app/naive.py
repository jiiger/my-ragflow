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
# ⚠️ 学习版裁剪说明: 官方 v0.24.0 本文件 1076 行, 按扩展名分发 docx/pdf/excel/
# ppt/markdown/html/json/txt/doc 九类; 本文件仅保留 txt/code 分支(任务链第一环),
# 其余分支的解析器导入全部含在官方顶部 import 中, 学习版不引入。

import logging
import re
from timeit import default_timer as timer

from common.float_utils import normalize_overlapped_percent
from deepdoc.parser import TxtParser
from rag.nlp import naive_merge, rag_tokenizer, tokenize_chunks


def chunk(filename, binary=None, from_page=0, to_page=100000, lang="Chinese", callback=None, **kwargs):
    """naive 解析器入口 — 学习版 txt 子集。

    整条 txt 解析链的"指挥者"(官方 naive.py 的 chunk 函数):
    parser(分隔符切段+贪心合并) → naive_merge(重叠/位置/自定义分隔符) →
    tokenize_chunks(分词三字段+位置) → 返回索引文档列表, 供 task_executor 入库。

    参数:
        filename: 文件名(含扩展名, 用于扩展名分发和文档骨架 docnm_kwd)
        binary: 文件二进制内容(HTTP 上传场景), 与 filename 二选一
        from_page / to_page: PDF 分页参数, 官方签名保留(txt 分支未使用)
        lang: "Chinese"/"English", 决定 is_english(影响表格行分隔符等风格)
        callback: 进度回调 callback(progress, msg), 官方逐阶段上报
        **kwargs: parser_config 知识库切块配置:
            chunk_token_num    每块目标 token 数(默认 512)
            delimiter          分隔符(默认 "\\n!?。；！？")
            overlapped_percent 块间重叠率(0~90)
            children_delimiter 父子块子分隔符(留空则普通切块)
    返回:
        res: 索引文档 dict 列表(每个块含 content_with_weight / content_ltks /
             content_sm_ltks / 位置字段 / mom_with_weight 等)
    ⚠️ 与官方的裁剪差异: 官方在扩展名分发前有 is_root 分支(extract_embed_file
    提取内嵌文件)并处理 analyze_hyperlink, 学习版均未移植——txt 解析路径用不到。
    """
    # 知识库表单配置: 调用方(未来 task_executor)通过 kwargs["parser_config"] 传入
    parser_config = kwargs.get("parser_config", {"chunk_token_num": 512, "delimiter": "\n!?。；！？"})
    is_english = lang.lower() == "english"

    # 父子块: 子分隔符解析与 delimiter 同套路(unicode_escape 四连 + 反引号整体)
    child_deli = (parser_config.get("children_delimiter") or "").encode("utf-8").decode("unicode_escape").encode("latin1").decode("utf-8")
    cust_child_deli = re.findall(r"`([^`]+)`", child_deli)
    child_deli = "|".join(re.sub(r"`([^`]+)`", "", child_deli))
    if cust_child_deli:
        cust_child_deli = sorted(set(cust_child_deli), key=lambda x: -len(x))
        cust_child_deli = "|".join(re.escape(t) for t in cust_child_deli if t)
        child_deli += cust_child_deli

    # 文档骨架: 每个块都会深拷贝它(文件名 + 文件名分词), 块之间互不污染
    doc = {"docnm_kwd": filename, "title_tks": rag_tokenizer.tokenize(re.sub(r"\.[a-zA-Z]+$", "", filename))}
    doc["title_sm_tks"] = rag_tokenizer.fine_grained_tokenize(doc["title_tks"])
    res = []
    pdf_parser = None  # ⚠️ 学习版恒为 None: tokenize_chunks 走伪位置分支, 不裁图
    # ⚠️ 官方此处还有 section_images 变量与图片分支(naive_merge_with_images), 学习版未移植。

    # ⚠️ 官方此处为九路扩展名分发(docx/pdf/excel/ppt/markdown/html/json/txt/doc),
    # 学习版只保留 txt/code 分支, 其余分支遇到直接抛错。
    if re.search(r"\.(txt|py|js|java|c|cpp|h|php|go|ts|sh|cs|kt|sql)$", filename, re.IGNORECASE):
        callback(0.1, "Start to parse.")
        sections = TxtParser()(filename, binary, parser_config.get("chunk_token_num", 128), parser_config.get("delimiter", "\n!?;。；！？"))
        callback(0.8, "Finish parsing.")
    else:
        raise NotImplementedError(f"学习版 naive 仅支持 txt/code 文件类型, 收到: {filename}")

    st = timer()
    overlapped_percent = normalize_overlapped_percent(parser_config.get("overlapped_percent", 0))
    # 二次合并: 补重叠率/位置串/自定义分隔符(官方注释: 先按 delimiter 切段, 再合并到 Max token)
    chunks = naive_merge(sections, int(parser_config.get("chunk_token_num", 128)), parser_config.get("delimiter", "\n!?。；！？"), overlapped_percent)
    # 块 → 索引文档: 空块跳过、伪位置、分词三字段、父子块(mom_with_weight)
    res.extend(tokenize_chunks(chunks, doc, is_english, pdf_parser, child_delimiters_pattern=child_deli))
    logging.info("naive_merge({}): {}".format(filename, timer() - st))

    # ⚠️ 官方尾部还有 urls/analyze_hyperlink 处理(docx/pdf 链接提取), 学习版未移植。
    return res