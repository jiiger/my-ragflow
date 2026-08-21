"""rag/nlp/__init__.py — 学习版极简子集(只放 rag/llm 需要的两个小函数)。

官方的 rag/nlp 还包含 rag_tokenizer(分词)、search(检索)、query 等大模块,
依赖 jieba/中文处理等, 属于第 3 步内容, 到时候再补。
is_chinese/is_english 函数体与官方 v0.24.0 完全一致。
"""
import re


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