import os
from datetime import date
import secrets

from common.config_utils import get_base_config, decrypt_database_config
from api.constants import RAG_FLOW_SERVICE_NAME

LLM = None
LLM_FACTORY = None
LLM_BASE_URL = None
CHAT_MDL = ""
EMBEDDING_MDL = ""
RERANK_MDL = ""
ASR_MDL = ""
IMAGE2TEXT_MDL = ""


CHAT_CFG = ""
EMBEDDING_CFG = ""
RERANK_CFG = ""
ASR_CFG = ""
IMAGE2TEXT_CFG = ""
API_KEY = None
PARSERS = None
HOST_IP = None
HOST_PORT = None
SECRET_KEY = None
FACTORY_LLM_INFOS = None
ALLOWED_LLM_FACTORIES = None

DATABASE_TYPE = os.getenv("DB_TYPE", "mysql")
DATABASE = decrypt_database_config(name=DATABASE_TYPE)

# authentication
AUTHENTICATION_CONF = None

# client
CLIENT_AUTHENTICATION = None
HTTP_APP_KEY = None
GITHUB_OAUTH = None
FEISHU_OAUTH = None
OAUTH_CONFIG = None
DOC_ENGINE = os.getenv("DOC_ENGINE", "elasticsearch")
DOC_ENGINE_INFINITY = DOC_ENGINE.lower() == "infinity"
DOC_ENGINE_OCEANBASE = DOC_ENGINE.lower() == "oceanbase"


docStoreConn = None
msgStoreConn = None

retriever = None
kg_retriever = None

# user registration switch
REGISTER_ENABLED = 1


# sandbox-executor-manager
SANDBOX_HOST = None
STRONG_TEST_COUNT = int(os.environ.get("STRONG_TEST_COUNT", "8"))

SMTP_CONF = None
MAIL_SERVER = ""
MAIL_PORT = 000
MAIL_USE_SSL = True
MAIL_USE_TLS = False
MAIL_USERNAME = ""
MAIL_PASSWORD = ""
MAIL_DEFAULT_SENDER = ()
MAIL_FRONTEND_URL = ""

# ===== 官方 kb_app 等路由引用的运行期字段(⚠️ 对应依赖未移植, 先占位) =====
DOC_ENGINE_INFINITY = False  # ⚠️ 文档引擎(ES/Infinity)未接入, 恒走 Elasticsearch 语义分支
docStoreConn = None  # ⚠️ 文档存储连接(common/doc_store 未移植, 官方为 DocStoreConnection 实例)
retriever = None  # ⚠️ 检索器(rag/utils/retriever 未移植), tags/知识图谱等接口暂不可用
STORAGE_IMPL = None  # ⚠️ 对象存储实现(官方为 FileStorage 实例), remove_bucket 用 hasattr 兜底
ALLOWED_LLM_FACTORIES = None  # 官方 init_settings 从 conf 读 user_default_llm.allowed_factories; 精简版恒 None = 不限制厂商

# move from rag.settings
ES = {}
INFINITY = {}
AZURE = {}
S3 = {}
MINIO = {}
OB = {}
OSS = {}
OS = {}
GCS = {}

DOC_MAXIMUM_SIZE: int = 128 * 1024 * 1024
DOC_BULK_SIZE: int = 4
EMBEDDING_BATCH_SIZE: int = 16

PARALLEL_DEVICES: int = 0

STORAGE_IMPL_TYPE = os.getenv("STORAGE_IMPL", "MINIO")
STORAGE_IMPL = None


def _get_or_create_secret_key():
    """从环境变量 RAGFLOW_SECRET_KEY / service_conf 的 secret_key 读取, 否则自动生成(官方 settings 原封)。"""
    secret_key = os.environ.get("RAGFLOW_SECRET_KEY")
    if secret_key and len(secret_key) >= 32:
        return secret_key

    # Check if there's a configured secret key
    configured_key = get_base_config(RAG_FLOW_SERVICE_NAME, {}).get("secret_key")
    if configured_key and configured_key != str(date.today()) and len(configured_key) >= 32:
        return configured_key

    # Generate a new secure key and warn about it
    import logging

    new_key = secrets.token_hex(32)
    logging.warning("SECURITY WARNING: Using auto-generated SECRET_KEY.")
    return new_key


def init_settings():
    """⚠️ 学习版精简版 init_settings(SECRET_KEY 部分对齐官方, 其余裁剪)。

    官方 init_settings 有 228 行(user_default_llm 各模型配置解析/llm_factories.json/
    doc_store/minio/s3 等), 学习版 settings 为精简版, 只在 HTTP 层需要时初始化:
    SECRET_KEY(签名 session)、oauth 配置(http_client._is_sensitive_url / user_app 第三方登录)。
    CHAT_CFG/EMBEDDING_CFG 等模型配置在 rag/llm 侧按需读取, 不在这里解析。
    """
    global SECRET_KEY, DATABASE, DATABASE_TYPE
    DATABASE_TYPE = os.getenv("DB_TYPE", "mysql")
    DATABASE = decrypt_database_config(name=DATABASE_TYPE)

    global GITHUB_OAUTH, FEISHU_OAUTH, OAUTH_CONFIG, CLIENT_AUTHENTICATION, HTTP_APP_KEY
    authentication_conf = get_base_config("authentication", {})
    CLIENT_AUTHENTICATION = authentication_conf.get("client", {}).get("switch", False)
    HTTP_APP_KEY = authentication_conf.get("client", {}).get("http_app_key")
    GITHUB_OAUTH = get_base_config("oauth", {}).get("github")
    FEISHU_OAUTH = get_base_config("oauth", {}).get("feishu")
    OAUTH_CONFIG = get_base_config("oauth", {})

    global SECRET_KEY
    SECRET_KEY = _get_or_create_secret_key()



