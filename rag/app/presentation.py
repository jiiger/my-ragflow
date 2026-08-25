# ⚠️ 学习版裁剪说明: 官方 v0.24.0 本文件 250 行, 是独立于 naive 的"演示文稿解析链",
# 支持 .ppt/.pptx/.pdf 三类, 语义是"每页一块 + 整页当图"(与 naive 的按 token 切不同)。
# 学习版仅移植 .ppt/.pptx 分支(依赖 RAGFlowPptParser, 已齐):
#   - pdf 分支依赖 PARSERS/by_plaintext/PlainParser 的 pdf 链, 未移植 → 显式报错
#   - tika 降级路径保留官方结构(未安装时自然 raise NotImplementedError)

import copy
import logging
import re

from deepdoc.parser.ppt_parser import RAGFlowPptParser
from rag.nlp import rag_tokenizer
from rag.nlp import tokenize


def chunk(filename, binary=None, from_page=0, to_page=100000, lang="Chinese", callback=None, parser_config=None, **kwargs):
    """
    The supported file formats are pdf, ppt, pptx.
    Every page will be treated as a chunk. And the thumbnail of every page will be stored.
    PPT file will be parsed by using this method automatically, setting-up for every PPT file is not necessary.
    """
    if parser_config is None:
        parser_config = {}
    eng = lang.lower() == "english"
    doc = {"docnm_kwd": filename, "title_tks": rag_tokenizer.tokenize(re.sub(r"\.[a-zA-Z]+$", "", filename))}
    doc["title_sm_tks"] = rag_tokenizer.fine_grained_tokenize(doc["title_tks"])
    res = []
    if re.search(r"\.pptx?$", filename, re.IGNORECASE):
        try:
            ppt_parser = RAGFlowPptParser()
            for pn, txt in enumerate(ppt_parser(filename if not binary else binary, from_page, 1000000, callback)):
                d = copy.deepcopy(doc)
                pn += from_page
                d["doc_type_kwd"] = "image"
                d["page_num_int"] = [pn + 1]
                d["top_int"] = [0]
                d["position_int"] = [(pn + 1, 0, 0, 0, 0)]
                tokenize(d, txt, eng)
                res.append(d)
            return res
        except Exception as e:
            logging.warning(f"python-pptx parsing failed for {filename}: {e}")
            # ⚠️ 官方此处: 降级到 tika 外部服务(需 Java)逐块解析, 学习版未引入,
            # 与 naive 的 .doc 分支同策略: 外部服务依赖显式报错而不是假装支持
            raise NotImplementedError(f"python-pptx 解析失败({e}), 学习版未引入 tika 降级服务: {filename}")
    elif re.search(r"\.pdf$", filename, re.IGNORECASE):
        # ⚠️ 官方此处: normalize_layout_recognizer + PARSERS 分发(DeepDOC/Docling/MinerU/
        # PaddleOCR/TCADP/Plain Text), 走 pdf 解析链, 学习版未移植 → 显式报错
        raise NotImplementedError("学习版 presentation 未移植 pdf 分支(依赖 pdf 解析链 PARSERS/by_plaintext): {}".format(filename))

    raise NotImplementedError("file type not supported yet(ppt, pptx, pdf supported)")


if __name__ == "__main__":
    import sys

    def dummy(a, b):
        pass

    chunk(sys.argv[1], from_page=0, to_page=10, callback=dummy)