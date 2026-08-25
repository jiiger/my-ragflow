# ⚠️ 学习版裁剪说明: 官方 v0.24.0 本文件 1076 行, 按扩展名分发 docx/pdf/excel/
# txt/code/markdown/html/json/doc 九类。学习版已移植其中六路:
#   docx(基础版, 无图片/标题树增强) / csv+xlsx / txt+code / md / html / json
# 裁剪: pdf(依赖 pdf_parser 完整版+OCR 模型)、doc(依赖 tika 外部服务)、
#       TCADP(腾讯云 API)、vision 模型增强(依赖 LLMBundle)、
#       内嵌文件提取与 hyperlink 链接分析(依赖 rag/utils/file_utils.py)。

import logging
import re
from functools import reduce
from io import BytesIO
from timeit import default_timer as timer

from markdown import markdown
from PIL import Image

from common.float_utils import normalize_overlapped_percent
from common.token_utils import num_tokens_from_string
from deepdoc.parser import DocxParser, ExcelParser, HtmlParser, JsonParser, MarkdownElementExtractor, MarkdownParser, TxtParser
from rag.nlp import (
    concat_img,
    doc_tokenize_chunks_with_images,
    find_codec,
    naive_merge,
    naive_merge_docx,
    naive_merge_with_images,
    rag_tokenizer,
    tokenize_chunks,
    tokenize_chunks_with_images,
    tokenize_table,
)


class Markdown(MarkdownParser):
    """Markdown 解析子类:官方 naive.py 同名类(官方 L583-719)逻辑 1:1 移植。

    在父类(deepdoc.parser.markdown_parser)的工具方法之上补齐:
    - 渲染成 HTML 的辅助(md_to_html / get_hyperlink_urls);
    - 以"行号区间"为单位把 markdown/html 图片引用挂到每个元素块
      (extract_image_urls_with_lines),并按需下载成 PIL Image(load_images_from_urls);
    - __call__ 产出 (sections, tbls, section_images) 三元组:
      sections 是 (文本, "") 二元组列表;tbls 是 ((None, 表格HTML), "") 列表;
      section_images 与 sections 等长,元素要么是合并后的 PIL Image 要么是 None。
    """

    def md_to_html(self, sections):
        """把 markdown 文本渲染成 HTML,返回 BeautifulSoup 对象(供链接/图片提取)。"""
        if not sections:
            return []
        if isinstance(sections, type("")):
            text = sections
        elif isinstance(sections[0], type("")):
            text = sections[0]
        else:
            return []

        from bs4 import BeautifulSoup

        html_content = markdown(text)
        soup = BeautifulSoup(html_content, "html.parser")
        return soup

    def get_hyperlink_urls(self, soup):
        """收集 soup 里所有 <a href>。"""
        if soup:
            return set([a.get("href") for a in soup.find_all("a") if a.get("href")])
        return []

    def extract_image_urls_with_lines(self, text):
        """扫描文本里的图片引用,返回 [{"url": ..., "line": 行号}]。

        同时识别 markdown 语法 ![alt](url) 与 HTML <img src>;跨行场景
        (HTML 标签被换行拆开)用 BeautifulSoup 兜底并折算回行号。
        """
        md_img_re = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
        html_img_re = re.compile(r'src=["\\\']([^"\\\'>\s]+)', re.IGNORECASE)
        urls = []
        seen = set()
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            for url in md_img_re.findall(line):
                if (url, idx) not in seen:
                    urls.append({"url": url, "line": idx})
                    seen.add((url, idx))
            for url in html_img_re.findall(line):
                if (url, idx) not in seen:
                    urls.append({"url": url, "line": idx})
                    seen.add((url, idx))

        # cross-line
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(text, "html.parser")
            newline_offsets = [m.start() for m in re.finditer(r"\n", text)] + [len(text)]
            for img_tag in soup.find_all("img"):
                src = img_tag.get("src")
                if not src:
                    continue

                tag_str = str(img_tag)
                pos = text.find(tag_str)
                if pos == -1:
                    # fallback
                    pos = max(text.find(src), 0)
                line_no = 0
                for i, off in enumerate(newline_offsets):
                    if pos <= off:
                        line_no = i
                        break
                if (src, line_no) not in seen:
                    urls.append({"url": src, "line": line_no})
                    seen.add((src, line_no))
        except Exception as e:
            logging.error("Failed to extract image urls: {}".format(e))
            pass

        return urls

    def load_images_from_urls(self, urls, cache=None):
        """按 URL 列表下载/打开图片成 PIL Image;本地路径直接打开,http(s) 走 requests。

        cache 复用已加载对象;加载失败(404/非图片/坏文件)记日志并跳过。
        """
        import requests
        from pathlib import Path

        cache = cache or {}
        images = []
        for url in urls:
            if url in cache:
                if cache[url]:
                    images.append(cache[url])
                continue
            img_obj = None
            try:
                if url.startswith(("http://", "https://")):
                    response = requests.get(url, stream=True, timeout=30)
                    if response.status_code == 200 and response.headers.get("Content-Type", "").startswith("image/"):
                        img_obj = Image.open(BytesIO(response.content)).convert("RGB")
                else:
                    local_path = Path(url)
                    if local_path.exists():
                        img_obj = Image.open(url).convert("RGB")
                    else:
                        logging.warning(f"Local image file not found: {url}")
            except Exception as e:
                logging.error(f"Failed to download/open image from {url}: {e}")
            cache[url] = img_obj
            if img_obj:
                images.append(img_obj)
        return images, cache

    def __call__(self, filename, binary=None, separate_tables=True, delimiter=None, return_section_images=False) -> tuple:
        """入口:读 md 文本 → 抽表格 → 按行号把图片挂到元素块 → (sections, tbls, section_images)。

        与官方 1:1;visual 增强(视觉模型看图)不在此方法,而在 chunk() 的 md 分支(学习版裁剪)。
        """
        if binary:
            encoding = find_codec(binary)
            txt = binary.decode(encoding, errors="ignore")
        else:
            with open(filename, "r") as f:
                txt = f.read()

        remainder, tables = self.extract_tables_and_remainder(f"{txt}\n", separate_tables=separate_tables)
        # To eliminate duplicate tables in chunking result, uncomment code below and set separate_tables to True in line 410.
        # extractor = MarkdownElementExtractor(remainder)
        extractor = MarkdownElementExtractor(txt)
        image_refs = self.extract_image_urls_with_lines(txt)
        element_sections = extractor.extract_elements(delimiter, include_meta=True)

        sections = []
        section_images = []
        image_cache = {}
        for element in element_sections:
            content = element["content"]
            start_line = element["start_line"]
            end_line = element["end_line"]
            urls_in_section = [ref["url"] for ref in image_refs if start_line <= ref["line"] <= end_line]
            imgs = []
            if urls_in_section:
                imgs, image_cache = self.load_images_from_urls(urls_in_section, image_cache)
            combined_image = None
            if imgs:
                combined_image = reduce(concat_img, imgs) if len(imgs) > 1 else imgs[0]
            sections.append((content, ""))
            section_images.append(combined_image)

        tbls = []
        for table in tables:
            tbls.append(((None, markdown(table, extensions=["markdown.extensions.tables"])), ""))
        if return_section_images:
            return sections, tbls, section_images
        return sections, tbls


def chunk(filename, binary=None, from_page=0, to_page=100000, lang="Chinese", callback=None, **kwargs):
    """naive 解析器入口:按扩展名分发到对应解析器,统一走 merge → tokenize 收尾。

    与官方 v0.24.0 的 chunk() 结构 1:1,裁剪点见文件头说明。支持:
        .docx(基础版) / .csv .xlsx / .txt 及常见代码扩展名 / .md .markdown .mdx /
        .htm .html / .json .jsonl .ldjson
    不支持(显式 NotImplementedError 提示): .pdf / .doc / 其他扩展名。

    参数与返回值见原官方签名:返回索引文档 dict 列表,每块含
    content_with_weight / content_ltks / content_sm_ltks / 位置字段等,
    doc 骨架(docnm_kwd/title_tks)深拷贝进每个块。
    """
    # 改善: 官方 bare 调用 callback(None 时崩溃), 学习版缺省静默, 与官方 __main__ 的 dummy 等效
    callback = callback or (lambda prog=None, msg="": None)

    urls = set()
    # ⚠️ 官方此处还有 url_res/embed_res(analyze_hyperlink+内嵌文件消费), 学习版裁剪

    is_english = lang.lower() == "english"  # is_english(cks)
    parser_config = kwargs.get("parser_config", {"chunk_token_num": 512, "delimiter": "\n!?。；！？", "layout_recognize": "DeepDOC", "analyze_hyperlink": True})

    # 父子块: 子分隔符解析与 delimiter 同套路(unicode_escape 四连 + 反引号整体)
    child_deli = (parser_config.get("children_delimiter") or "").encode("utf-8").decode("unicode_escape").encode("latin1").decode("utf-8")
    cust_child_deli = re.findall(r"`([^`]+)`", child_deli)
    child_deli = "|".join(re.sub(r"`([^`]+)`", "", child_deli))
    if cust_child_deli:
        cust_child_deli = sorted(set(cust_child_deli), key=lambda x: -len(x))
        cust_child_deli = "|".join(re.escape(t) for t in cust_child_deli if t)
        child_deli += cust_child_deli

    is_markdown = False
    table_context_size = max(0, int(parser_config.get("table_context_size", 0) or 0))
    image_context_size = max(0, int(parser_config.get("image_context_size", 0) or 0))

    # 文档骨架: 每个块都会深拷贝它(文件名 + 文件名分词), 块之间互不污染
    doc = {"docnm_kwd": filename, "title_tks": rag_tokenizer.tokenize(re.sub(r"\.[a-zA-Z]+$", "", filename))}
    doc["title_sm_tks"] = rag_tokenizer.fine_grained_tokenize(doc["title_tks"])
    res = []
    pdf_parser = None  # ⚠️ 学习版恒为 None: tokenize_chunks 走伪位置分支, 不裁图
    section_images = None

    is_root = kwargs.get("is_root", True)
    # ⚠️ 官方此处: is_root 时 extract_embed_file 提取内嵌文件并递归 chunk, 学习版裁剪(依赖 file_utils)

    if re.search(r"\.docx$", filename, re.IGNORECASE):
        callback(0.1, "Start to parse.")
        # ⚠️ 官方此处: analyze_hyperlink 链接提取 + load_from_xml_v2 补丁 + Docx 豪华子类
        # (图片、标题树、表格样式、to_markdown), 学习版用基础 DocxParser 替代:
        # 它的 __call__ 只收一个参数(路径或 bytes), 返回 (段落[(text, style)], 表格[内容]),
        # 组装成 (text, image, table) 三元组喂 naive_merge_docx
        secs, tbls = DocxParser()(binary if binary is not None else filename)
        sections = [(t, None, None) for t, _ in (secs or []) if t] + [("", None, tb) for tb in (tbls or [])]
        chunks, _ = naive_merge_docx(sections, int(parser_config.get("chunk_token_num", 128)), parser_config.get("delimiter", "\n!?。；！？"), table_context_size, image_context_size)
        # ⚠️ 官方此处: vision_figure_parser_docx_wrapper_naive 视觉看图增强(依赖 LLM), 裁剪
        callback(0.8, "Finish parsing.")
        st = timer()

        res.extend(doc_tokenize_chunks_with_images(chunks, doc, is_english, child_delimiters_pattern=child_deli))
        logging.info("naive_merge({}): {}".format(filename, timer() - st))
        return res

    elif re.search(r"\.pdf$", filename, re.IGNORECASE):
        raise NotImplementedError("学习版 naive 未移植 pdf 解析链(官方 by_deepdoc/by_plaintext + pdf_parser 完整版/OCR 模型): {}".format(filename))

    elif re.search(r"\.(csv|xlsx?)$", filename, re.IGNORECASE):
        callback(0.1, "Start to parse.")
        # ⚠️ 官方此处: layout_recognize=="TCADP Parser" 分支(腾讯云 API), 裁剪
        # Default DeepDOC parser
        excel_parser = ExcelParser()
        if parser_config.get("html4excel"):
            sections = [(_, "") for _ in excel_parser.html(binary, 12) if _]
            parser_config["chunk_token_num"] = 0
        else:
            sections = [(_, "") for _ in excel_parser(binary) if _]

    elif re.search(r"\.(txt|py|js|java|c|cpp|h|php|go|ts|sh|cs|kt|sql)$", filename, re.IGNORECASE):
        callback(0.1, "Start to parse.")
        sections = TxtParser()(filename, binary, parser_config.get("chunk_token_num", 128), parser_config.get("delimiter", "\n!?;。；！？"))
        callback(0.8, "Finish parsing.")

    elif re.search(r"\.(md|markdown|mdx)$", filename, re.IGNORECASE):
        callback(0.1, "Start to parse.")
        markdown_parser = Markdown(int(parser_config.get("chunk_token_num", 128)))
        # 索引解包避开 Pyright 对 return_section_images 分支的元组长度推断
        md_result = markdown_parser(
            filename,
            binary,
            separate_tables=False,
            delimiter=parser_config.get("delimiter", "\n!?;。；！？"),
            return_section_images=True,
        )
        sections, tables, section_images = md_result[0], md_result[1], md_result[2]
        is_markdown = True
        # ⚠️ 官方此处: vision_model 检测与视觉增强(VisionFigureParser, 依赖 LLMBundle), 裁剪
        if parser_config.get("hyperlink_urls", False) and is_root:
            for section_text, _ in sections:
                soup = markdown_parser.md_to_html(section_text)
                hyperlink_urls = markdown_parser.get_hyperlink_urls(soup)
                urls.update(hyperlink_urls)
        res = tokenize_table(tables, doc, is_english)
        callback(0.8, "Finish parsing.")

    elif re.search(r"\.(htm|html)$", filename, re.IGNORECASE):
        callback(0.1, "Start to parse.")
        chunk_token_num = int(parser_config.get("chunk_token_num", 128))
        sections = HtmlParser()(filename, binary, chunk_token_num)
        sections = [(_, "") for _ in sections if _]
        callback(0.8, "Finish parsing.")

    elif re.search(r"\.(json|jsonl|ldjson)$", filename, re.IGNORECASE):
        callback(0.1, "Start to parse.")
        chunk_token_num = int(parser_config.get("chunk_token_num", 128))
        sections = JsonParser(chunk_token_num)(binary)
        sections = [(_, "") for _ in sections if _]
        callback(0.8, "Finish parsing.")

    elif re.search(r"\.doc$", filename, re.IGNORECASE):
        raise NotImplementedError("学习版 naive 未移植 .doc 解析(官方依赖 tika 外部服务): {}".format(filename))

    else:
        raise NotImplementedError("file type not supported yet(pdf, xlsx, doc, docx, txt supported)")

    st = timer()
    overlapped_percent = normalize_overlapped_percent(parser_config.get("overlapped_percent", 0))

    if is_markdown:
        # md 专用合并(官方 1:1): 按 token 上限累积文本块, 超限截重叠; 图片随块走
        merged_chunks = []
        merged_images = []
        chunk_limit = max(0, int(parser_config.get("chunk_token_num", 128)))
        current_text = ""
        current_tokens = 0
        current_image = None
        for idx, sec in enumerate(sections):
            text = sec[0] if isinstance(sec, tuple) else sec
            sec_tokens = num_tokens_from_string(text)
            sec_image = section_images[idx] if section_images and idx < len(section_images) else None
            if current_text and current_tokens + sec_tokens > chunk_limit:
                merged_chunks.append(current_text)
                merged_images.append(current_image)
                overlap_part = ""
                if overlapped_percent > 0:
                    overlap_len = int(len(current_text) * overlapped_percent / 100)
                    if overlap_len > 0:
                        overlap_part = current_text[-overlap_len:]
                current_text = overlap_part
                current_tokens = num_tokens_from_string(current_text)
                current_image = current_image if overlap_part else None
            if current_text:
                current_text += "\n" + text
            else:
                current_text = text
            current_tokens += sec_tokens
            if sec_image:
                current_image = concat_img(current_image, sec_image) if current_image else sec_image
        if current_text:
            merged_chunks.append(current_text)
            merged_images.append(current_image)

        chunks = merged_chunks
        has_images = merged_images and any(img is not None for img in merged_images)
        if has_images:
            res.extend(tokenize_chunks_with_images(chunks, doc, is_english, merged_images, child_delimiters_pattern=child_deli))
        else:
            res.extend(tokenize_chunks(chunks, doc, is_english, pdf_parser, child_delimiters_pattern=child_deli))
    else:
        if section_images:
            if all(image is None for image in section_images):
                section_images = None

        if section_images:
            chunks, images = naive_merge_with_images(sections, section_images, int(parser_config.get("chunk_token_num", 128)), parser_config.get("delimiter", "\n!?。；！？"), overlapped_percent)
            res.extend(tokenize_chunks_with_images(chunks, doc, is_english, images, child_delimiters_pattern=child_deli))
        else:
            chunks = naive_merge(sections, int(parser_config.get("chunk_token_num", 128)), parser_config.get("delimiter", "\n!?。；！？"), overlapped_percent)
            res.extend(tokenize_chunks(chunks, doc, is_english, pdf_parser, child_delimiters_pattern=child_deli))

    logging.info("naive_merge({}): {}".format(filename, timer() - st))

    # ⚠️ 官方尾部: urls 链接分析的 extract_html 消费 + embed_res 内嵌结果合并, 学习版裁剪
    return res