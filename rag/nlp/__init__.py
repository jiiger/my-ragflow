"""rag/nlp/__init__.py — 学习版子集。

已移植(逻辑与官方 v0.24.0 逐行对齐):
- 文本判断: is_chinese / is_english
- 编码探测: find_codec / all_codecs(供 deepdoc get_text 猜编码)
- Merge/Tokenize 链: naive_merge 家族 + tokenize 家族, 把 parser 产出的
  sections 合并成块, 再转成带分词/位置字段的索引文档(ES/Infinity 的 chunk)
待移植: 结构切块族
(tree_merge/hierarchical_merge/Node)暂未搬; rag_tokenizer 已单独成文件。
"""

import copy
import logging
import random
import re
from collections import defaultdict

import chardet
import roman_numbers as r
from cn2an import cn2an
from word2number import w2n
from PIL import Image

from common.token_utils import num_tokens_from_string

# ---- 题型识别族(2026-08-27 补搬自官方 v0.24.0): QUESTION_PATTERN/BULLET_PATTERN/
# has_qbullet/index_int/qbullets_category/random_choices/not_bullet/bullets_category/
# docx_question_level。供 rag.app.qa 问答对解析(Excel 抽样判英文/PDF 题干识别/
# DOCX 题型层级)与 chunk rank_feature; 依赖 word2number/cn2an/roman_numbers 已入 pyproject

QUESTION_PATTERN = [
    r"第([零一二三四五六七八九十百0-9]+)问",
    r"第([零一二三四五六七八九十百0-9]+)条",
    r"[\(（]([零一二三四五六七八九十百]+)[\)）]",
    r"第([0-9]+)问",
    r"第([0-9]+)条",
    r"([0-9]{1,2})[\. 、]",
    r"([零一二三四五六七八九十百]+)[ 、]",
    r"[\(（]([0-9]{1,2})[\)）]",
    r"QUESTION (ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN)",
    r"QUESTION (I+V?|VI*|XI|IX|X)",
    r"QUESTION ([0-9]+)",
]


def has_qbullet(reg, box, last_box, last_index, last_bull, bull_x0_list):
    section, last_section = box['text'], last_box['text']
    q_reg = r'(\w|\W)*?(?:？|\?|\n|$)+'
    full_reg = reg + q_reg
    has_bull = re.match(full_reg, section)
    index_str = None
    if has_bull:
        if 'x0' not in last_box:
            last_box['x0'] = box['x0']
        if 'top' not in last_box:
            last_box['top'] = box['top']
        if last_bull and box['x0'] - last_box['x0'] > 10:
            return None, last_index
        if not last_bull and box['x0'] >= last_box['x0'] and box['top'] - last_box['top'] < 20:
            return None, last_index
        avg_bull_x0 = 0
        if bull_x0_list:
            avg_bull_x0 = sum(bull_x0_list) / len(bull_x0_list)
        else:
            avg_bull_x0 = box['x0']
        if box['x0'] - avg_bull_x0 > 10:
            return None, last_index
        index_str = has_bull.group(1)
        index = index_int(index_str)
        if last_section[-1] == ':' or last_section[-1] == '：':
            return None, last_index
        if not last_index or index >= last_index:
            bull_x0_list.append(box['x0'])
            return has_bull, index
        if section[-1] == '?' or section[-1] == '？':
            bull_x0_list.append(box['x0'])
            return has_bull, index
        if box['layout_type'] == 'title':
            bull_x0_list.append(box['x0'])
            return has_bull, index
        pure_section = section.lstrip(re.match(reg, section).group()).lower()
        ask_reg = r'(what|when|where|how|why|which|who|whose|为什么|为啥|哪)'
        if re.match(ask_reg, pure_section):
            bull_x0_list.append(box['x0'])
            return has_bull, index
    return None, last_index


def index_int(index_str):
    res = -1
    try:
        res = int(index_str)
    except ValueError:
        try:
            res = w2n.word_to_num(index_str)
        except ValueError:
            try:
                res = cn2an(index_str)
            except ValueError:
                try:
                    res = r.number(index_str)
                except ValueError:
                    return -1
    return res


def qbullets_category(sections):
    global QUESTION_PATTERN
    hits = [0] * len(QUESTION_PATTERN)
    for i, pro in enumerate(QUESTION_PATTERN):
        for sec in sections:
            if re.match(pro, sec) and not not_bullet(sec):
                hits[i] += 1
                break
    maximum = 0
    res = -1
    for i, h in enumerate(hits):
        if h <= maximum:
            continue
        res = i
        maximum = h
    return res, QUESTION_PATTERN[res]


BULLET_PATTERN = [[
    r"第[零一二三四五六七八九十百0-9]+(分?编|部分)",
    r"第[零一二三四五六七八九十百0-9]+章",
    r"第[零一二三四五六七八九十百0-9]+节",
    r"第[零一二三四五六七八九十百0-9]+条",
    r"[\(（][零一二三四五六七八九十百]+[\)）]",
], [
    r"第[0-9]+章",
    r"第[0-9]+节",
    r"[0-9]{,2}[\. 、]",
    r"[0-9]{,2}\.[0-9]{,2}[^a-zA-Z/%~-]",
    r"[0-9]{,2}\.[0-9]{,2}\.[0-9]{,2}",
    r"[0-9]{,2}\.[0-9]{,2}\.[0-9]{,2}\.[0-9]{,2}",
], [
    r"第[零一二三四五六七八九十百0-9]+章",
    r"第[零一二三四五六七八九十百0-9]+节",
    r"[零一二三四五六七八九十百]+[ 、]",
    r"[\(（][零一二三四五六七八九十百]+[\)）]",
    r"[\(（][0-9]{,2}[\)）]",
], [
    r"PART (ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN)",
    r"Chapter (I+V?|VI*|XI|IX|X)",
    r"Section [0-9]+",
    r"Article [0-9]+"
], [
    r"^#[^#]",
    r"^##[^#]",
    r"^###.*",
    r"^####.*",
    r"^#####.*",
    r"^######.*",
]
]


def random_choices(arr, k):
    k = min(len(arr), k)
    return random.choices(arr, k=k)


def not_bullet(line):
    patt = [
        r"0", r"[0-9]+ +[0-9~个只-]", r"[0-9]+\.{2,}"
    ]
    return any([re.match(r, line) for r in patt])


def bullets_category(sections):
    global BULLET_PATTERN
    hits = [0] * len(BULLET_PATTERN)
    for i, pro in enumerate(BULLET_PATTERN):
        for sec in sections:
            sec = sec.strip()
            for p in pro:
                if re.match(p, sec) and not not_bullet(sec):
                    hits[i] += 1
                    break
    maximum = 0
    res = -1
    for i, h in enumerate(hits):
        if h <= maximum:
            continue
        res = i
        maximum = h
    return res




def docx_question_level(p, bull=-1):
    txt = re.sub(r"\u3000", " ", p.text).strip()
    if p.style.name.startswith('Heading'):
        return int(p.style.name.split(' ')[-1]), txt
    else:
        if bull < 0:
            return 0, txt
        for j, title in enumerate(BULLET_PATTERN[bull]):
            if re.match(title, txt):
                return j + 1, txt
    return len(BULLET_PATTERN[bull]) + 1, txt



all_codecs = [
    "utf-8",
    "gb2312",
    "gbk",
    "utf_16",
    "ascii",
    "big5",
    "big5hkscs",
    "cp037",
    "cp273",
    "cp424",
    "cp437",
    "cp500",
    "cp720",
    "cp737",
    "cp775",
    "cp850",
    "cp852",
    "cp855",
    "cp856",
    "cp857",
    "cp858",
    "cp860",
    "cp861",
    "cp862",
    "cp863",
    "cp864",
    "cp865",
    "cp866",
    "cp869",
    "cp874",
    "cp875",
    "cp932",
    "cp949",
    "cp950",
    "cp1006",
    "cp1026",
    "cp1125",
    "cp1140",
    "cp1250",
    "cp1251",
    "cp1252",
    "cp1253",
    "cp1254",
    "cp1255",
    "cp1256",
    "cp1257",
    "cp1258",
    "euc_jp",
    "euc_jis_2004",
    "euc_jisx0213",
    "euc_kr",
    "gb18030",
    "hz",
    "iso2022_jp",
    "iso2022_jp_1",
    "iso2022_jp_2",
    "iso2022_jp_2004",
    "iso2022_jp_3",
    "iso2022_jp_ext",
    "iso2022_kr",
    "latin_1",
    "iso8859_2",
    "iso8859_3",
    "iso8859_4",
    "iso8859_5",
    "iso8859_6",
    "iso8859_7",
    "iso8859_8",
    "iso8859_9",
    "iso8859_10",
    "iso8859_11",
    "iso8859_13",
    "iso8859_14",
    "iso8859_15",
    "iso8859_16",
    "johab",
    "koi8_r",
    "koi8_t",
    "koi8_u",
    "kz1048",
    "mac_cyrillic",
    "mac_greek",
    "mac_iceland",
    "mac_latin2",
    "mac_roman",
    "mac_turkish",
    "ptcp154",
    "shift_jis",
    "shift_jis_2004",
    "shift_jisx0213",
    "utf_32",
    "utf_32_be",
    "utf_32_le",
    "utf_16_be",
    "utf_16_le",
    "utf_7",
    "windows-1250",
    "windows-1251",
    "windows-1252",
    "windows-1253",
    "windows-1254",
    "windows-1255",
    "windows-1256",
    "windows-1257",
    "windows-1258",
    "latin-2",
]


def find_codec(blob):
    detected = chardet.detect(blob[:1024])
    if detected["confidence"] > 0.5:
        if detected["encoding"] == "ascii":
            return "utf-8"

    for c in all_codecs:
        try:
            blob[:1024].decode(c)
            return c
        except Exception:
            pass
        try:
            blob.decode(c)
            return c
        except Exception:
            pass

    return "utf-8"


def is_english(texts):
    """粗略判断文本是否英文(计"英文合法字符"占比 >80%)。官方原函数。"""
    if not texts:
        return False

    pattern = re.compile(r"[`a-zA-Z0-9\s.,':;/\"?<>!()\-]")

    if isinstance(texts, str):
        texts = list(texts)
    elif isinstance(texts, list):
        texts = [t for t in texts if isinstance(t, str) and t.strip()]
    else:
        return False

    if not texts:
        return False

    eng = sum(1 for t in texts if pattern.fullmatch(t.strip()))
    return (eng / len(texts)) > 0.8


def is_chinese(text):
    """粗略判断文本是否中文(CJK 统一表意文字占比 >20%)。官方原函数。"""
    if not text:
        return False
    chinese = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            chinese += 1
    if chinese / len(text) > 0.2:
        return True
    return False


# =========================================================================
# Merge/Tokenize 链 —— 从"parser 输出"到"索引文档"
# 移植自官方 v0.24.0 rag/nlp/__init__.py(逻辑 1:1, 注释为学习添加)
#
# 数据流:
#   parser 产出 sections(每项 [text, layout/位置串])或 chunks(文本列表)
#      ↓ naive_merge / naive_merge_with_images / naive_merge_docx  合并成块
#      ↓ tokenize_chunks 等  转成索引文档
#   最终每个块是一个 dict, 核心字段:
#     content_with_weight   原文(检索展示/加权)
#     content_ltks          粗粒度分词(整词), 关键词匹配用
#     content_sm_ltks       细粒度分词(子词), 向量/前缀匹配用
#     page_num_int/position_int/top_int  位置(PDF 真坐标, 非 PDF 伪位置)
#     mom_with_weight       父子块模式的母块全文
#     doc_type_kwd          块类型(text/table/image)
#     image                 块配图(PIL 图像)
# =========================================================================


def tokenize(d, txt, eng):
    """把一个块的文本写入文档 dict 的三个文本字段。

    参数:
        d: 目标文档 dict(通常是 doc 骨架的深拷贝), 就地写入字段
        txt: 块的原始文本
        eng: 是否英文; 官方签名保留, 当前实现未使用
    写入字段:
        content_with_weight = txt 原文
        content_ltks = 剥掉 html 表格标签后粗粒度分词
        content_sm_ltks = 对粗粒度结果再细粒度分词
    注意: rag_tokenizer 在函数内延迟 import(官方写法), 避免模块级循环依赖。
    """
    from . import rag_tokenizer
    d["content_with_weight"] = txt
    t = re.sub(r"</?(table|td|caption|tr|th)( [^<>]{0,12})?>", " ", txt)
    d["content_ltks"] = rag_tokenizer.tokenize(t)
    d["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(d["content_ltks"])


def split_with_pattern(d, pattern: str, content: str, eng) -> list:
    """按子分隔符(父子块模式)把 content 二次切分成多个子文档。

    参数:
        d: 文档骨架 dict(每次 deepcopy 后使用, 不改原对象)
        pattern: 子分隔符拼成的正则串(如 "。|。|\n")
        content: 母块全文(父子块的"父")
        eng: 是否英文(未使用, 保留官方签名)
    返回:
        docs: 子文档列表, 每个都带 tokenize 三字段
    关键细节:
        - 带捕获组的 split 输出 [文本, 分隔符, 文本, ...], 步长 2 取文本位,
          并把跟随的分隔符拼回文本尾部(与 txt_parser 丢弃分隔符相反——
          子块要独立进检索, 语义要完整)
        - 正则非法时回退为"整段一个块", 不抛异常
    """
    docs = []

    # Validate and compile regex pattern before use
    try:
        compiled_pattern = re.compile(r"(%s)" % pattern, flags=re.DOTALL)
    except re.error as e:
        logging.warning(f"Invalid delimiter regex pattern '{pattern}': {e}. Falling back to no split.")
        # Fallback: return content as single chunk
        dd = copy.deepcopy(d)
        tokenize(dd, content, eng)
        return [dd]

    txts = [txt for txt in compiled_pattern.split(content)]
    for j in range(0, len(txts), 2):
        txt = txts[j]
        if not txt:
            continue
        if j + 1 < len(txts):
            txt += txts[j + 1]
        dd = copy.deepcopy(d)
        tokenize(dd, txt, eng)
        docs.append(dd)
    return docs


def tokenize_chunks(chunks, doc, eng, pdf_parser=None, child_delimiters_pattern=None):
    """把合并好的文本块列表转成索引文档列表(纯文本链主入口)。

    参数:
        chunks: 文本块列表(naive_merge 的输出)
        doc: 文档骨架(文件名/标题分词), 每个块 deepcopy 一份
        eng: 是否英文
        pdf_parser: PDF 解析器实例(有则裁图+取真实坐标, 无则伪位置)
        child_delimiters_pattern: 子分隔符正则串; 非空则启用父子块模式
    返回:
        res: 索引文档 dict 列表, 每个元素消费方为 task_executor(embedding+入库)
    注意: 空块跳过; 父子块模式下块内设 mom_with_weight 再细切, 不再整体 tokenize。
    """
    res = []
    # wrap up as es documents
    for ii, ck in enumerate(chunks):
        if len(ck.strip()) == 0:
            continue
        logging.debug("-- {}".format(ck))
        d = copy.deepcopy(doc)
        if pdf_parser:
            try:
                d["image"], poss = pdf_parser.crop(ck, need_position=True)
                add_positions(d, poss)
                ck = pdf_parser.remove_tag(ck)
            except NotImplementedError:
                pass
        else:
            add_positions(d, [[ii] * 5])

        if child_delimiters_pattern:
            d["mom_with_weight"] = ck
            res.extend(split_with_pattern(d, child_delimiters_pattern, ck, eng))
            continue

        tokenize(d, ck, eng)
        res.append(d)
    return res


def doc_tokenize_chunks_with_images(chunks, doc, eng, child_delimiters_pattern=None, batch_size=10):
    """docx 链专用: 把 dict 块(text/image/ck_type/context_above/below)转成索引文档。

    参数:
        chunks: naive_merge_docx 的输出(块是 dict)
        doc: 文档骨架; eng: 是否英文
        child_delimiters_pattern: 子分隔符正则串(父子块)
        batch_size: 保留官方签名, 当前未使用
    返回: 索引文档列表
    关键细节:
        - 块的文本 = context_above + text + context_below(图表块已由 _add_context 补上下文)
        - ck_type 决定 doc_type_kwd: text/image/table
    """
    res = []
    for ii, ck in enumerate(chunks):
        text = ck.get("context_above", "") + ck.get("text") + ck.get("context_below", "")
        if len(text.strip()) == 0:
            continue
        logging.debug("-- {}".format(ck))
        d = copy.deepcopy(doc)
        if ck.get("image"):
            d["image"] = ck.get("image")
        add_positions(d, [[ii] * 5])

        if ck.get("ck_type") == "text":
            if child_delimiters_pattern:
                d["mom_with_weight"] = text
                res.extend(split_with_pattern(d, child_delimiters_pattern, text, eng))
                continue
        elif ck.get("ck_type") == "image":
            d["doc_type_kwd"] = "image"
        elif ck.get("ck_type") == "table":
            d["doc_type_kwd"] = "table"
        tokenize(d, text, eng)
        res.append(d)
    return res


def tokenize_chunks_with_images(chunks, doc, eng, images, child_delimiters_pattern=None):
    """纯文本链带图版本: 文本块与图片 zip 配对, 图写入 d["image"] 字段。

    参数:
        chunks: 文本块列表; images: 与 chunks 等长的图片列表(可含 None)
        doc: 文档骨架; eng: 是否英文
        child_delimiters_pattern: 子分隔符正则串(父子块)
    返回: 索引文档列表
    """
    res = []
    # wrap up as es documents
    for ii, (ck, image) in enumerate(zip(chunks, images)):
        if len(ck.strip()) == 0:
            continue
        logging.debug("-- {}".format(ck))
        d = copy.deepcopy(doc)
        d["image"] = image
        add_positions(d, [[ii] * 5])
        if child_delimiters_pattern:
            d["mom_with_weight"] = ck
            res.extend(split_with_pattern(d, child_delimiters_pattern, ck, eng))
            continue
        tokenize(d, ck, eng)
        res.append(d)
    return res


def tokenize_table(tbls, doc, eng, batch_size=10):
    """表格特化: 表格行按 batch 拼接成文本块, 标 doc_type_kwd。

    参数:
        tbls: [(图片, 行列表/字符串), 位置] 的列表(表格解析器产出)
        doc: 文档骨架; eng: 是否英文(决定行分隔符 "; " 中文 "； ")
        batch_size: 每块拼多少行
    返回: 索引文档列表
    关键细节: 行里没有 <tr> 标签却配了图 → 判定为图片块(doc_type_kwd=image)。
    """
    res = []
    # add tables
    for (img, rows), poss in tbls:
        if not rows:
            continue
        if isinstance(rows, str):
            d = copy.deepcopy(doc)
            tokenize(d, rows, eng)
            d["content_with_weight"] = rows
            d["doc_type_kwd"] = "table"
            if img:
                d["image"] = img
                if d["content_with_weight"].find("<tr>") < 0:
                    d["doc_type_kwd"] = "image"
            if poss:
                add_positions(d, poss)
            res.append(d)
            continue
        de = "; " if eng else "； "
        for i in range(0, len(rows), batch_size):
            d = copy.deepcopy(doc)
            r = de.join(rows[i:i + batch_size])
            tokenize(d, r, eng)
            d["doc_type_kwd"] = "table"
            if img:
                d["image"] = img
                if d["content_with_weight"].find("<tr>") < 0:
                    d["doc_type_kwd"] = "image"
            add_positions(d, poss)
            res.append(d)
    return res


def add_positions(d, poss):
    """把位置五元组写进文档 dict 的三个位置字段。

    参数:
        d: 目标文档 dict(就地写入)
        poss: [(页号, left, right, top, bottom), ...]; PDF 传真实坐标,
              非 PDF 传 [[块序号]*5] 伪位置
    写入字段:
        page_num_int   页号列表(PDF 库 0 基 → 展示 1 基, +1)
        top_int        顶部 y 坐标列表
        position_int   (页号, left, right, top, bottom) 完整元组列表
    """
    if not poss:
        return
    page_num_int = []
    position_int = []
    top_int = []
    for pn, left, right, top, bottom in poss:
        page_num_int.append(int(pn + 1))
        top_int.append(int(top))
        position_int.append((int(pn + 1), int(left), int(right), int(top), int(bottom)))
    d["page_num_int"] = page_num_int
    d["position_int"] = position_int
    d["top_int"] = top_int


def naive_merge(sections: str | list, chunk_token_num=128, delimiter="\n。；！？", overlapped_percent=0):
    """朴素合并: 把 sections 按分隔符切成小段, 再贪心合并成约 chunk_token_num 的块。

    参数:
        sections: str 或 [(text, pos), ...](parser 输出的 sections)
        chunk_token_num: 每块目标 token 数(实测会略超: 先合并后封口)
        delimiter: 分隔符, 支持反引号包裹的多字符自定义分隔符, 如 "`##`"
        overlapped_percent: 块间重叠率(0~100), 超限封口时把上一块尾部按比例
                            拼到新块开头, 减少跨块信息断层
    返回:
        cks: 文本块列表(纯字符串, 不带位置; 与 txt_parser 返回 [[c, ""]] 不同)
    关键细节(与 txt_parser 的 add_chunk 对比):
        - 开新块条件多乘了重叠率系数: block 提前封口给重叠留空间
        - <8 token 的残段不带位置
        - 位置串追加到文本尾, find(pos) < 0 防重复追加
        - 有自定义分隔符时整体重切: 每段独立成块, 不再按 token 合并
    ⚠️ 依赖: 官方在第 2 行函数内 import RAGFlowPdfParser(只用其 remove_tag 静态
    方法去除文本里的 PDF 位置标记 "@@页码,坐标##", 防止重叠拼接时位置串截半残留).
    deepdoc/parser/pdf_parser.py 已于 2026-08-25 移植最小版(仅 remove_tag), 本函数可用。
    """
    from deepdoc.parser.pdf_parser import RAGFlowPdfParser
    if not sections:
        return []
    if isinstance(sections, str):
        sections = [sections]
    if isinstance(sections[0], str):
        sections = [(s, "") for s in sections]
    cks = [""]
    tk_nums = [0]

    def add_chunk(t, pos):
        nonlocal cks, tk_nums, delimiter
        tnum = num_tokens_from_string(t)
        if not pos:
            pos = ""
        if tnum < 8:
            pos = ""
        # Ensure that the length of the merged chunk does not exceed chunk_token_num
        if cks[-1] == "" or tk_nums[-1] > chunk_token_num * (100 - overlapped_percent) / 100.:
            if cks:
                overlapped = RAGFlowPdfParser.remove_tag(cks[-1])
                t = overlapped[int(len(overlapped) * (100 - overlapped_percent) / 100.):] + t
            if t.find(pos) < 0:
                t += pos
            cks.append(t)
            tk_nums.append(tnum)
        else:
            if cks[-1].find(pos) < 0:
                cks[-1] += pos
            cks[-1] += t
            tk_nums[-1] += tnum

    custom_delimiters = [m.group(1) for m in re.finditer(r"`([^`]+)`", delimiter)]
    has_custom = bool(custom_delimiters)
    if has_custom:
        custom_pattern = "|".join(re.escape(t) for t in sorted(set(custom_delimiters), key=len, reverse=True))
        cks, tk_nums = [], []
        for sec, pos in sections:
            split_sec = re.split(r"(%s)" % custom_pattern, sec, flags=re.DOTALL)
            for sub_sec in split_sec:
                if re.fullmatch(custom_pattern, sub_sec or ""):
                    continue
                text = "\n" + sub_sec
                local_pos = pos
                if num_tokens_from_string(text) < 8:
                    local_pos = ""
                if local_pos and text.find(local_pos) < 0:
                    text += local_pos
                cks.append(text)
                tk_nums.append(num_tokens_from_string(text))
        return cks

    for sec, pos in sections:
        add_chunk("\n" + sec, pos)

    return cks


def naive_merge_with_images(texts, images, chunk_token_num=128, delimiter="\n。；！？", overlapped_percent=0):
    """naive_merge 的带图版本: 文本与图片同步合并, 同块多图纵向拼接成一张。

    参数:
        texts: 文本段列表(元素可为 str 或 (text, pos) 二元组)
        images: 与 texts 等长的图片列表(可含 None)
        chunk_token_num / delimiter / overlapped_percent: 同 naive_merge
    返回:
        (cks, result_images): 文本块列表 + 对应图片列表
    ⚠️ 依赖: 同 naive_merge, 函数内 import RAGFlowPdfParser(pdf_parser 未移植,
    调用时 ImportError 属预期)。
    """
    from deepdoc.parser.pdf_parser import RAGFlowPdfParser
    if not texts or len(texts) != len(images):
        return [], []
    cks = [""]
    result_images = [None]
    tk_nums = [0]

    def add_chunk(t, image, pos=""):
        nonlocal cks, result_images, tk_nums, delimiter
        tnum = num_tokens_from_string(t)
        if not pos:
            pos = ""
        if tnum < 8:
            pos = ""
        # Ensure that the length of the merged chunk does not exceed chunk_token_num
        if cks[-1] == "" or tk_nums[-1] > chunk_token_num * (100 - overlapped_percent) / 100.:
            if cks:
                overlapped = RAGFlowPdfParser.remove_tag(cks[-1])
                t = overlapped[int(len(overlapped) * (100 - overlapped_percent) / 100.):] + t
            if t.find(pos) < 0:
                t += pos
            cks.append(t)
            result_images.append(image)
            tk_nums.append(tnum)
        else:
            if cks[-1].find(pos) < 0:
                cks[-1] += pos
            cks[-1] += t
            if result_images[-1] is None:
                result_images[-1] = image
            else:
                result_images[-1] = concat_img(result_images[-1], image)
            tk_nums[-1] += tnum

    custom_delimiters = [m.group(1) for m in re.finditer(r"`([^`]+)`", delimiter)]
    has_custom = bool(custom_delimiters)
    if has_custom:
        custom_pattern = "|".join(re.escape(t) for t in sorted(set(custom_delimiters), key=len, reverse=True))
        cks, result_images, tk_nums = [], [], []
        for text, image in zip(texts, images):
            text_str = text[0] if isinstance(text, tuple) else text
            if text_str is None:
                text_str = ""
            text_pos = text[1] if isinstance(text, tuple) and len(text) > 1 else ""
            split_sec = re.split(r"(%s)" % custom_pattern, text_str)
            for sub_sec in split_sec:
                if re.fullmatch(custom_pattern, sub_sec or ""):
                    continue
                text_seg = "\n" + sub_sec
                local_pos = text_pos
                if num_tokens_from_string(text_seg) < 8:
                    local_pos = ""
                if local_pos and text_seg.find(local_pos) < 0:
                    text_seg += local_pos
                cks.append(text_seg)
                result_images.append(image)
                tk_nums.append(num_tokens_from_string(text_seg))
        return cks, result_images

    for text, image in zip(texts, images):
        # if text is tuple, unpack it
        if isinstance(text, tuple):
            text_str = text[0] if text[0] is not None else ""
            text_pos = text[1] if len(text) > 1 else ""
            add_chunk("\n" + text_str, image, text_pos)
        else:
            add_chunk("\n" + (text or ""), image)

    return cks, result_images


def concat_img(img1, img2):
    """纵向拼接两张图片为一张(同一块多图合并)。

    参数:
        img1 / img2: PIL Image 或 None
    返回:
        合并后的 PIL Image; 任一侧为 None 时返回另一侧, 像素相同(同一张图)
        时直接返回 img1(避免重复拼接)
    注意: 官方使用 PIL.Image.new("RGB") 作为画布, 透明图会被填充成黑底。
    """
    if img1 and not img2:
        return img1
    if not img1 and img2:
        return img2
    if not img1 and not img2:
        return None

    if img1 is img2:
        return img1

    if isinstance(img1, Image.Image) and isinstance(img2, Image.Image):
        pixel_data1 = img1.tobytes()
        pixel_data2 = img2.tobytes()
        if pixel_data1 == pixel_data2:
            return img1

    width1, height1 = img1.size
    width2, height2 = img2.size

    new_width = max(width1, width2)
    new_height = height1 + height2
    new_image = Image.new("RGB", (new_width, new_height))

    new_image.paste(img1, (0, 0))
    new_image.paste(img2, (0, height1))
    return new_image


def _build_cks(sections, delimiter):
    """docx 链第 1 步: 把 (text, image, table) 三元组 sections 建成 dict 块列表。

    参数:
        sections: 每个元素 (text, image, table)(docx 解析器的输出)
        delimiter: 分隔符(识别反引号自定义分隔符)
    返回:
        (cks, tables, images, has_custom):
          cks        dict 块列表, 每块 {text, image, ck_type, tk_nums}
          tables     ck_type=table 的块在 cks 中的下标
          images     ck_type=image 的块在 cks 中的下标
          has_custom 是否含反引号自定义分隔符(影响后续 _merge_cks)
    关键细节: 有自定义分隔符时文本段按分隔符切成多块(段间缓冲 seg 累积,
    遇分隔符/空段 flush); 无自定义分隔符时每段文本独立成块。
    """
    cks = []
    tables = []
    images = []

    # extract custom delimiters wrapped by backticks: `##`, `---`, etc.
    custom_delimiters = [m.group(1) for m in re.finditer(r"`([^`]+)`", delimiter)]
    has_custom = bool(custom_delimiters)

    if has_custom:
        # escape delimiters and build alternation pattern, longest first
        custom_pattern = "|".join(
            re.escape(t) for t in sorted(set(custom_delimiters), key=len, reverse=True)
        )
        # capture delimiters so they appear in re.split results
        pattern = r"(%s)" % custom_pattern

    seg = ""
    for text, image, table in sections:
        # normalize text: ensure string and prepend newline for continuity
        if not text:
            text = ""
        else:
            text = "\n" + str(text)

        if table:
            # table chunk
            ck_text = text + str(table)
            idx = len(cks)
            cks.append({
                "text": ck_text,
                "image": image,
                "ck_type": "table",
                "tk_nums": num_tokens_from_string(ck_text),
            })
            tables.append(idx)
            continue

        if image:
            # image chunk (text kept as-is for context)
            idx = len(cks)
            cks.append({
                "text": text,
                "image": image,
                "ck_type": "image",
                "tk_nums": num_tokens_from_string(text),
            })
            images.append(idx)
            continue

        # pure text chunk(s)
        if has_custom:
            split_sec = re.split(pattern, text)
            for sub_sec in split_sec:
                # ① empty or whitespace-only segment → flush current buffer
                if not sub_sec or not sub_sec.strip():
                    if seg and seg.strip():
                        s = seg.strip()
                        cks.append({
                            "text": s,
                            "image": None,
                            "ck_type": "text",
                            "tk_nums": num_tokens_from_string(s),
                        })
                    seg = ""
                    continue

                # ② matched custom delimiter (allow surrounding whitespace)
                if re.fullmatch(custom_pattern, sub_sec.strip()):
                    if seg and seg.strip():
                        s = seg.strip()
                        cks.append({
                            "text": s,
                            "image": None,
                            "ck_type": "text",
                            "tk_nums": num_tokens_from_string(s),
                        })
                    seg = ""
                    continue

                # ③ normal text content → accumulate
                seg += sub_sec
        else:
            # no custom delimiter: emit the text as a single chunk
            if text and text.strip():
                t = text.strip()
                cks.append({
                    "text": t,
                    "image": None,
                    "ck_type": "text",
                    "tk_nums": num_tokens_from_string(t),
                })

    # final flush after loop (only when custom delimiters are used)
    if has_custom and seg and seg.strip():
        s = seg.strip()
        cks.append({
            "text": s,
            "image": None,
            "ck_type": "text",
            "tk_nums": num_tokens_from_string(s),
        })

    return cks, tables, images, has_custom


def _add_context(cks, idx, context_size):
    """docx 链第 2 步: 给 idx 处的图表块补 context_above/context_below 上下文。

    参数:
        cks: _build_cks 的块列表(就地修改)
        idx: 图表块下标
        context_size: 上下文 token 预算(上下各取这么多)
    关键细节:
        - 从相邻的 text 块取句子, 按句切分(split_pat 含 。!?等), 不切半句
        - 上方取尾部句子(take_sentences_from_end), 下方取头部句子(from_start)
        - 邻居块本身超过预算时只截取需要的句子, 否则整块取走并继续向内扩
        - 非 image/table 块直接跳过
    """
    if cks[idx]["ck_type"] not in ("image", "table"):
        return

    prev = idx - 1
    after = idx + 1
    remain_above = context_size
    remain_below = context_size

    cks[idx]["context_above"] = ""
    cks[idx]["context_below"] = ""

    split_pat = r"([。!?？；！\n]|\. )"

    picked_above = []
    picked_below = []

    def take_sentences_from_end(cnt, need_tokens):
        txts = re.split(split_pat, cnt, flags=re.DOTALL)
        sents = []
        for j in range(0, len(txts), 2):
            sents.append(txts[j] + (txts[j + 1] if j + 1 < len(txts) else ""))
        acc = ""
        for s in reversed(sents):
            acc = s + acc
            if num_tokens_from_string(acc) >= need_tokens:
                break
        return acc

    def take_sentences_from_start(cnt, need_tokens):
        txts = re.split(split_pat, cnt, flags=re.DOTALL)
        acc = ""
        for j in range(0, len(txts), 2):
            acc += txts[j] + (txts[j + 1] if j + 1 < len(txts) else "")
            if num_tokens_from_string(acc) >= need_tokens:
                break
        return acc

    # above
    parts_above = []
    while prev >= 0 and remain_above > 0:
        if cks[prev]["ck_type"] == "text":
            tk = cks[prev]["tk_nums"]
            if tk >= remain_above:
                piece = take_sentences_from_end(cks[prev]["text"], remain_above)
                parts_above.insert(0, piece)
                picked_above.append((prev, "tail", remain_above, tk, piece[:80]))
                remain_above = 0
                break
            else:
                parts_above.insert(0, cks[prev]["text"])
                picked_above.append((prev, "full", remain_above, tk, (cks[prev]["text"] or "")[:80]))
                remain_above -= tk
        prev -= 1

    # below
    parts_below = []
    while after < len(cks) and remain_below > 0:
        if cks[after]["ck_type"] == "text":
            tk = cks[after]["tk_nums"]
            if tk >= remain_below:
                piece = take_sentences_from_start(cks[after]["text"], remain_below)
                parts_below.append(piece)
                picked_below.append((after, "head", remain_below, tk, piece[:80]))
                remain_below = 0
                break
            else:
                parts_below.append(cks[after]["text"])
                picked_below.append((after, "full", remain_below, tk, (cks[after]["text"] or "")[:80]))
                remain_below -= tk
        after += 1

    cks[idx]["context_above"] = "".join(parts_above) if parts_above else ""
    cks[idx]["context_below"] = "".join(parts_below) if parts_below else ""


def _merge_cks(cks, chunk_token_num, has_custom):
    """docx 链第 3 步: 文本块按 token 数贪心合并, 图表块永远独立成块。

    参数:
        cks: _build_cks 的块列表(dict 块)
        chunk_token_num: 每块目标 token 数
        has_custom: 有自定义分隔符时每段独立成块, 不做合并
    返回:
        (merged, image_idxs): 合并后的块列表; image_idxs 是 image 块在新列表中的下标
    """
    merged = []
    image_idxs = []
    prev_text_ck = -1

    for i in range(len(cks)):
        ck_type = cks[i]["ck_type"]

        if ck_type != "text":
            merged.append(cks[i])
            if ck_type == "image":
                image_idxs.append(len(merged) - 1)
            continue

        if prev_text_ck < 0 or merged[prev_text_ck]["tk_nums"] >= chunk_token_num or has_custom:
            merged.append(cks[i])
            prev_text_ck = len(merged) - 1
            continue

        merged[prev_text_ck]["text"] = (merged[prev_text_ck].get("text") or "") + (cks[i].get("text") or "")
        merged[prev_text_ck]["tk_nums"] = merged[prev_text_ck].get("tk_nums", 0) + cks[i].get("tk_nums", 0)

    return merged, image_idxs


def naive_merge_docx(
    sections,
    chunk_token_num=128,
    delimiter="\n。；！？",
    table_context_size=0,
    image_context_size=0,
):
    """docx 专用合并: (text, image, table) sections → (块列表, 图块下标)。

    三步流水线: _build_cks(建 dict 块) → _add_context(图表块补上下文) →
    _merge_cks(文本块合并)。图表块带 context_above/below 后由下游
    doc_tokenize_chunks_with_images 拼成完整文本。

    参数:
        sections: [(text, image, table), ...](docx 解析器输出)
        chunk_token_num: 文本块目标 token 数
        delimiter: 分隔符(支持反引号自定义)
        table_context_size / image_context_size: 图表块上下文 token 预算, 0 关闭
    返回:
        (merged_cks, merged_image_idx): 块列表 + image 块下标(供视觉模型看图)
    """
    if not sections:
        return [], []

    cks, tables, images, has_custom = _build_cks(sections, delimiter)

    if table_context_size > 0:
        for i in tables:
            _add_context(cks, i, table_context_size)

    if image_context_size > 0:
        for i in images:
            _add_context(cks, i, image_context_size)

    merged_cks, merged_image_idx = _merge_cks(cks, chunk_token_num, has_custom)

    return merged_cks, merged_image_idx


# ---- append_context2table_image4pdf(2026-08-27 补搬自官方 v0.24.0): ----
# naive chunk 的 pdf 分支用: 按页码(position_tag)把表格/图片附近的正文取上文
# 拼接, 强化表格上下文(table_context_size/image_context_size 配置)

def append_context2table_image4pdf(sections: list, tabls: list, table_context_size=0, return_context=False):
    from deepdoc.parser import PdfParser
    if table_context_size <=0:
        return [] if return_context else tabls

    page_bucket = defaultdict(list)
    for i, item in enumerate(sections):
        if isinstance(item, (tuple, list)):
            if len(item) > 2:
                txt, _sec_id, poss = item[0], item[1], item[2]
            else:
                txt = item[0] if item else ""
                poss = item[1] if len(item) > 1 else ""
        else:
            txt = item
            poss = ""
        # Normal: (text, "@@...##") from naive parser -> poss is a position tag string.
        # Manual: (text, sec_id, poss_list) -> poss is a list of (page, left, right, top, bottom).
        # Paper: (text_with_@@tag, layoutno) -> poss is layoutno; parse from txt when it contains @@ tags.
        if isinstance(poss, list):
            poss = poss
        elif isinstance(poss, str):
            if "@@" not in poss and isinstance(txt, str) and "@@" in txt:
                poss = txt
            poss = PdfParser.extract_positions(poss)
        else:
            if isinstance(txt, str) and "@@" in txt:
                poss = PdfParser.extract_positions(txt)
            else:
                poss = []
        if isinstance(txt, str) and "@@" in txt:
            txt = re.sub(r"@@[0-9-]+\t[0-9.\t]+##", "", txt).strip()
        for page, left, right, top, bottom in poss:
            if isinstance(page, list):
                page = page[0] if page else 0
            page_bucket[page].append(((left, right, top, bottom), txt))

    def upper_context(page, i):
        txt = ""
        if page not in page_bucket:
            i = -1
        while num_tokens_from_string(txt) < table_context_size:
            if i < 0:
                page -= 1
                if page < 0 or page not in page_bucket:
                    break
                i = len(page_bucket[page]) -1
            blks = page_bucket[page]
            (_, _, _, _), cnt = blks[i]
            txts = re.split(r"([。!?？；！\n]|\. )", cnt, flags=re.DOTALL)[::-1]
            for j in range(0, len(txts), 2):
                txt = (txts[j+1] if j+1<len(txts) else "") + txts[j] + txt
                if num_tokens_from_string(txt) > table_context_size:
                    break
            i -= 1
        return txt

    def lower_context(page, i):
        txt = ""
        if page not in page_bucket:
            return txt
        while num_tokens_from_string(txt) < table_context_size:
            if i >= len(page_bucket[page]):
                page += 1
                if page not in page_bucket:
                    break
                i = 0
            blks = page_bucket[page]
            (_, _, _, _), cnt = blks[i]
            txts = re.split(r"([。!?？；！\n]|\. )", cnt, flags=re.DOTALL)
            for j in range(0, len(txts), 2):
                txt += txts[j] + (txts[j+1] if j+1<len(txts) else "")
                if num_tokens_from_string(txt) > table_context_size:
                    break
            i += 1
        return txt

    res = []
    contexts = []
    for (img, tb), poss in tabls:
        page, left, right, top, bott = poss[0]
        _page, _left, _right, _top, _bott = poss[-1]
        if isinstance(tb, list):
            tb = "\n".join(tb)

        i = 0
        blks = page_bucket.get(page, [])
        _tb = tb
        while i < len(blks):
            if i + 1 >= len(blks):
                if _page > page:
                    page += 1
                    i = 0
                    blks = page_bucket.get(page, [])
                    continue
                upper = upper_context(page, i)
                lower = lower_context(page + 1, 0)
                tb = upper + tb + lower
                contexts.append((upper.strip(), lower.strip()))
                break
            (_, _, t, b), txt = blks[i]
            if b > top:
                break
            (_, _, _t, _b), _txt = blks[i+1]
            if _t < _bott:
                i += 1
                continue

            upper = upper_context(page, i)
            lower = lower_context(page, i)
            tb = upper + tb + lower
            contexts.append((upper.strip(), lower.strip()))
            break

        if _tb == tb:
            upper = upper_context(page, -1)
            lower = lower_context(page + 1, 0)
            tb = upper + tb + lower
            contexts.append((upper.strip(), lower.strip()))
        if len(contexts) < len(res) + 1:
            contexts.append(("", ""))
        res.append(((img, tb), poss))
    return contexts if return_context else res
