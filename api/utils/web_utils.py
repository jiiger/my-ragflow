"""api/utils/web_utils.py — 学习版精简子集。

官方 245 行, 顶层 import 了 selenium/webdriver_manager 等重依赖; 学习版只拆
HTTP 层需要的部分(官方 api_utils.py 先例同款做法):
- OTP/验证码常量与工具(忘记密码流程用): OTP_LENGTH / OTP_TTL_SECONDS /
  ATTEMPT_LIMIT / ATTEMPT_LOCK_SECONDS / RESEND_COOLDOWN_SECONDS / otp_keys /
  hash_code / captcha_key
- send_email_html / send_invite_email: 邮件发送(aiosmtplib 在 venv)
- CONTENT_TYPE_MAP / is_private_ip / is_valid_url: document_app 下载/爬取校验用,
  纯标准库实现, 2026-08-24 document_app 移植时补回
砍掉: html2pdf(selenium+chromedriver 渲染, 保留 ⚠️ 桩, web_crawl 暂不可用)。
"""

import aiosmtplib
import ipaddress
import re
import socket
from email.mime.text import MIMEText
from email.header import Header
from urllib.parse import urlparse

from common import settings
from quart import render_template_string
from api.utils.email_templates import EMAIL_TEMPLATES

OTP_LENGTH = 4
OTP_TTL_SECONDS = 5 * 60 # valid for 5 minutes
ATTEMPT_LIMIT = 5 # maximum attempts
ATTEMPT_LOCK_SECONDS = 30 * 60 # lock for 30 minutes
RESEND_COOLDOWN_SECONDS = 60 # cooldown for 1 minute


CONTENT_TYPE_MAP = {
    # Office
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    # Text/code
    "txt": "text/plain",
    "py": "text/plain",
    "js": "text/plain",
    "java": "text/plain",
    "c": "text/plain",
    "cpp": "text/plain",
    "h": "text/plain",
    "php": "text/plain",
    "go": "text/plain",
    "ts": "text/plain",
    "sh": "text/plain",
    "cs": "text/plain",
    "kt": "text/plain",
    "sql": "text/plain",
    # Web
    "md": "text/markdown",
    "markdown": "text/markdown",
    "mdx": "text/markdown",
    "htm": "text/html",
    "html": "text/html",
    "json": "application/json",
    # Image formats
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "tif": "image/tiff",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "ico": "image/x-icon",
    "avif": "image/avif",
    "heic": "image/heic",
    # PPTX
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def is_private_ip(ip: str) -> bool:
    """判断 IP 是否为内网地址(防止 SSRF 打到内网)。"""
    try:
        ip_obj = ipaddress.ip_address(ip)
        return ip_obj.is_private
    except ValueError:
        return False


def is_valid_url(url: str) -> bool:
    """校验 url 格式并拒绝解析到内网 IP(web_crawl 用)。"""
    if not re.match(r"(https?)://[-A-Za-z0-9+&@#/%?=~_|!:,.;]+[-A-Za-z0-9+&@#/%=~_|]", url):
        return False
    parsed_url = urlparse(url)
    hostname = parsed_url.hostname

    if not hostname:
        return False
    try:
        ip = socket.gethostbyname(hostname)
        if is_private_ip(ip):
            return False
    except socket.gaierror:
        return False
    return True


def html2pdf(source, timeout=2, install_driver=True, print_options={}):
    """⚠️ 学习版裁剪: 官方实现依赖 selenium+ChromeDriverManager 渲染网页为 PDF, 未移植。

    document_app 的 /web_crawl 调用点会显式触发 NotImplementedError。
    来源: 官方 api/utils/web_utils.py @ v0.24.0
    """
    raise NotImplementedError("html2pdf 依赖 selenium+chromedriver 渲染, 学习版未移植; web_crawl 端点暂不可用")


async def send_email_html(to_email: str, subject: str, template_key: str, **context):
    """按模板渲染邮件正文并通过 SMTP 发送(settings.MAIL_* 配置).

    模板在 email_templates.EMAIL_TEMPLATES 里按 template_key 取(如 reset_code)。
    """
    body = await render_template_string(EMAIL_TEMPLATES.get(template_key), **context)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = f"{settings.MAIL_DEFAULT_SENDER[0]} <{settings.MAIL_DEFAULT_SENDER[1]}>"
    msg["To"] = to_email

    smtp = aiosmtplib.SMTP(
        hostname=settings.MAIL_SERVER,
        port=settings.MAIL_PORT,
        use_tls=True,
        timeout=10,
    )

    await smtp.connect()
    await smtp.login(settings.MAIL_USERNAME, settings.MAIL_PASSWORD)
    await smtp.send_message(msg)
    await smtp.quit()


async def send_invite_email(to_email, invite_url, tenant_id, inviter):
    # Reuse the generic HTML sender with 'invite' template
    await send_email_html(
        to_email=to_email,
        subject="RAGFlow Invitation",
        template_key="invite",
        email=to_email,
        invite_url=invite_url,
        tenant_id=tenant_id,
        inviter=inviter,
    )


def otp_keys(email: str):
    """生成该邮箱在 Redis 里的 4 个 OTP 相关 key(代码/尝试次数/上次发送/锁定)。"""
    email = (email or "").strip().lower()
    return (
        f"otp:{email}",
        f"otp_attempts:{email}",
        f"otp_last_sent:{email}",
        f"otp_lock:{email}",
    )


def hash_code(code: str, salt: bytes) -> str:
    """OTP 加盐哈希(hmac-sha256, 落 Redis 的是 hexdigest 不是明文)。"""
    import hashlib
    import hmac
    return hmac.new(salt, (code or "").encode("utf-8"), hashlib.sha256).hexdigest()


def captcha_key(email: str) -> str:
    """图形验证码在 Redis 里的 key。"""
    return f"captcha:{email}"