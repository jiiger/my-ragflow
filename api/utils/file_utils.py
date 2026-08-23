"""api/utils/file_utils.py — 文件类型识别/缩略图/PDF 修复/路径清洗工具, 对齐官方 v0.24.0 全量。"""

# Standard library imports
import base64
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from io import BytesIO

import pdfplumber
from PIL import Image

# Local imports
from api.constants import IMG_BASE64_PREFIX
from api.db import FileType

# pdfplumber 全局锁(pdfplumber 非线程安全, 用模块级单例锁串行化)
LOCK_KEY_pdfplumber = "global_shared_lock_pdfplumber"
if LOCK_KEY_pdfplumber not in sys.modules:
    sys.modules[LOCK_KEY_pdfplumber] = threading.Lock()


def filename_type(filename):
    """按扩展名判断文件类型(PDF/DOC 文档/AURAL 音频/VISUAL 图像视频/OTHER)。

    映射到 api.db.FileType, 是上传与解析链路的第一个分叉点。
    """
    filename = filename.lower()
    if re.match(r".*\.pdf$", filename):
        return FileType.PDF.value

    if re.match(r".*\.(msg|eml|doc|docx|ppt|pptx|yml|xml|htm|json|jsonl|ldjson|csv|txt|ini|xls|xlsx|wps|rtf|hlp|pages|numbers|key|md|mdx|py|js|java|c|cpp|h|php|go|ts|sh|cs|kt|html|sql)$", filename):
        return FileType.DOC.value

    if re.match(r".*\.(wav|flac|ape|alac|wavpack|wv|mp3|aac|ogg|vorbis|opus)$", filename):
        return FileType.AURAL.value

    if re.match(r".*\.(jpg|jpeg|png|tif|gif|pcx|tga|exif|fpx|svg|psd|cdr|pcd|dxf|ufo|eps|ai|raw|WMF|webp|avif|apng|icon|ico|mpg|mpeg|avi|rm|rmvb|mov|wmv|asf|dat|asx|wvx|mpe|mpa|mp4|avi|mkv)$", filename):
        return FileType.VISUAL.value

    return FileType.OTHER.value


def thumbnail_img(filename, blob):
    """生成文件缩略图字节(PDF 首页渲染或图片缩小), 超 MySQL LongText 上限时降分辨率。

    返回 None 表示不支持生成; 缩略图以 PNG 存进对象存储, 前端列表页展示。
    """
    filename = filename.lower()
    if re.match(r".*\.pdf$", filename):
        with sys.modules[LOCK_KEY_pdfplumber]:
            pdf = pdfplumber.open(BytesIO(blob))

            buffered = BytesIO()
            resolution = 32
            img = None
            for _ in range(10):
                # https://github.com/jsvine/pdfplumber?tab=readme-ov-file#creating-a-pageimage-with-to_image
                pdf.pages[0].to_image(resolution=resolution).annotated.save(buffered, format="png")
                img = buffered.getvalue()
                if len(img) >= 64000 and resolution >= 2:
                    resolution = resolution / 2
                    buffered = BytesIO()
                else:
                    break
        pdf.close()
        return img

    elif re.match(r".*\.(jpg|jpeg|png|tif|gif|icon|ico|webp)$", filename):
        image = Image.open(BytesIO(blob))
        image.thumbnail((30, 30))
        buffered = BytesIO()
        image.save(buffered, format="png")
        return buffered.getvalue()
    return None


def thumbnail(filename, blob):
    """thumbnail_img 的 base64 data-URL 包装(前端可直接当 img src 用)。"""
    img = thumbnail_img(filename, blob)
    if img is not None:
        return IMG_BASE64_PREFIX + base64.b64encode(img).decode("utf-8")
    else:
        return ""


def repair_pdf_with_ghostscript(input_bytes):
    """用系统 ghostscript(gs) 修复损坏 PDF; 无 gs 或修复失败时原样返回。"""
    if shutil.which("gs") is None:
        return input_bytes

    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_in, tempfile.NamedTemporaryFile(suffix=".pdf") as temp_out:
        temp_in.write(input_bytes)
        temp_in.flush()

        cmd = [
            "gs",
            "-o",
            temp_out.name,
            "-sDEVICE=pdfwrite",
            "-dPDFSETTINGS=/prepress",
            temp_in.name,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                return input_bytes
        except Exception:
            return input_bytes

        temp_out.seek(0)
        repaired_bytes = temp_out.read()

    return repaired_bytes


def read_potential_broken_pdf(blob):
    """读取可能有损坏的 PDF: 打不开时先用 ghostscript 修复再试, 都不行就原样返回。"""
    def try_open(blob):
        try:
            with pdfplumber.open(BytesIO(blob)) as pdf:
                if pdf.pages:
                    return True
        except Exception:
            return False
        return False

    if try_open(blob):
        return blob

    repaired = repair_pdf_with_ghostscript(blob)
    if try_open(repaired):
        return repaired

    return blob


def sanitize_path(raw_path: str | None) -> str:
    """Normalize and sanitize a user-provided path segment.

    - Converts backslashes to forward slashes
    - Strips leading/trailing slashes
    - Removes '.' and '..' segments
    - Restricts characters to A-Za-z0-9, underscore, dash, and '/'
    """
    if not raw_path:
        return ""
    backslash_re = re.compile(r"[\\]+")
    unsafe_re = re.compile(r"[^A-Za-z0-9_\-/]")
    normalized = backslash_re.sub("/", raw_path)
    normalized = normalized.strip("/")
    parts = [seg for seg in normalized.split("/") if seg and seg not in (".", "..")]
    sanitized = "/".join(parts)
    sanitized = unsafe_re.sub("", sanitized)
    return sanitized