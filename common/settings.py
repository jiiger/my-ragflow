import json
import os
from datetime import date
import secrets

from common.config_utils import get_base_config, decrypt_database_config
from common.constants import SVR_QUEUE_NAME, Storage
from common.file_utils import get_project_base_directory
from rag.utils.minio_conn import RAGFlowMinio
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
retriever = None  # init_settings 装配为 rag.nlp.search.Dealer(检索器); kg_retriever 装配为 rag.graphrag.search.KGSearch(图谱检索器)
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


def get_svr_queue_name(priority: int) -> str:
    """任务队列名: 优先级 0 用基础队列名, 否则追加 _<priority>(官方同名方法 1:1)。

    官方在 common/settings.py:130; document_service 里曾有本地等价 _svr_queue_name,
    现在以本方法为准, 后续可清理重复实现。
    """
    if priority == 0:
        return SVR_QUEUE_NAME
    return f"{SVR_QUEUE_NAME}_{priority}"


def get_svr_queue_names():
    """task_executor 消费的全部队列名: 高优先级在前(官方 common/settings.py:135)。"""
    return [get_svr_queue_name(priority) for priority in [1, 0]]


class StorageFactory:
    """对象存储工厂:按 STORAGE_IMPL_TYPE(MINIO/AWS_S3/...) 实例化对应连接器。

    官方 common/settings.py:155 的 storage_mapping 有 7 个后端, 学习版只移植
    MINIO, 其余类型显式报错而不是假装支持。
    """
    storage_mapping = {
        Storage.MINIO: RAGFlowMinio,
    }

    @classmethod
    def create(cls, storage: Storage):
        if storage not in cls.storage_mapping:
            raise NotImplementedError(f"学习版仅支持 MINIO 对象存储, 收到: {storage}")
        return cls.storage_mapping[storage]()


def _parse_model_entry(entry):
    """把 conf 里单个模型条目规范成 dict(官方 common/settings.py:366 1:1)。

    支持两种写法: "模型名" 字符串, 或 {"name"/"model", "factory", "api_key", "base_url"} dict。
    """
    if isinstance(entry, str):
        return {"name": entry, "factory": None, "api_key": None, "base_url": None}
    if isinstance(entry, dict):
        name = entry.get("name") or entry.get("model") or ""
        return {
            "name": name,
            "factory": entry.get("factory"),
            "api_key": entry.get("api_key"),
            "base_url": entry.get("base_url"),
        }
    return {"name": "", "factory": None, "api_key": None, "base_url": None}


def _resolve_per_model_config(entry_dict, backup_factory, backup_api_key, backup_base_url):
    """模型条目解析: 补齐缺省的 factory/api_key/base_url, 模型名拼上 "@厂商" 后缀
    (官方 common/settings.py:380 1:1)。

    CHAT_CFG 等 *_CFG 的最终形态: {"model": "xxx@yyy", "factory": "yyy",
    "api_key": "...", "base_url": "..."}; get_init_tenant_llm / LLMBundle 都消费它。
    """
    name = (entry_dict.get("name") or "").strip()
    m_factory = entry_dict.get("factory") or backup_factory or ""
    m_api_key = entry_dict.get("api_key") or backup_api_key or ""
    m_base_url = entry_dict.get("base_url") or backup_base_url or ""

    if name and "@" not in name and m_factory:
        name = f"{name}@{m_factory}"

    return {
        "model": name,
        "factory": m_factory,
        "api_key": m_api_key,
        "base_url": m_base_url,
    }


def init_settings():
    """⚠️ 学习版 init_settings(模型配置解析已按官方补全, 其余按需裁剪)。

    与官方 228 行 init_settings 的差异:
    - ✅ 补全: user_default_llm 解析(LLM_FACTORY/BASE_URL/API_KEY/PARSERS/
      *_MDL/*_CFG)+ llm_factories.json 加载(FACTORY_LLM_INFOS)
    - ⚠️ 裁剪: HOST_IP/HOST_PORT(入口自行从 conf 读)、doc_store 多引擎
      (仅 ES)、对象存储多后端(仅 MINIO)、检索器 retriever/kg_retriever
      (None 占位)、SMTP 邮件、RAGFLOW_CRYPTO 加密存储、sandbox。
    SECRET_KEY、oauth/authentication、MINIO、ES 连接部分保留官方逻辑。
    """
    global SECRET_KEY, DATABASE, DATABASE_TYPE
    DATABASE_TYPE = os.getenv("DB_TYPE", "mysql")
    DATABASE = decrypt_database_config(name=DATABASE_TYPE)

    # ═══ 模型默认配置解析(2026-08-26 补全,对齐官方 L176-225)═══
    # 官方 init_settings 从 conf/service_conf.yaml 的 user_default_llm 段读取
    # 默认模型, 并加载 conf/llm_factories.json 的厂商目录。学习版此前裁剪
    # (CFG/MDL 全空串), 导致 get_init_tenant_llm 返回空、租户绑不上模型;
    # 现按官方逻辑补回, 未配置的模型类型保持空串(不会报错)。
    global ALLOWED_LLM_FACTORIES, LLM_FACTORY, LLM_BASE_URL
    llm_settings = get_base_config("user_default_llm", {}) or {}
    llm_default_models = llm_settings.get("default_models", {}) or {}
    LLM_FACTORY = llm_settings.get("factory", "") or ""
    LLM_BASE_URL = llm_settings.get("base_url", "") or ""
    ALLOWED_LLM_FACTORIES = llm_settings.get("allowed_factories", None)

    global REGISTER_ENABLED
    try:
        REGISTER_ENABLED = int(os.environ.get("REGISTER_ENABLED", "1"))
    except Exception:
        pass

    global FACTORY_LLM_INFOS
    try:
        with open(os.path.join(get_project_base_directory(), "conf", "llm_factories.json"), "r") as f:
            FACTORY_LLM_INFOS = json.load(f)["factory_llm_infos"]
    except Exception:
        FACTORY_LLM_INFOS = []

    global API_KEY
    API_KEY = llm_settings.get("api_key")

    global PARSERS
    PARSERS = llm_settings.get(
        "parsers", "naive:General,qa:Q&A,resume:Resume,manual:Manual,table:Table,paper:Paper,book:Book,laws:Laws,presentation:Presentation,picture:Picture,one:One,audio:Audio,email:Email,tag:Tag"
    )

    global CHAT_MDL, EMBEDDING_MDL, RERANK_MDL, ASR_MDL, IMAGE2TEXT_MDL
    chat_entry = _parse_model_entry(llm_default_models.get("chat_model", CHAT_MDL))
    embedding_entry = _parse_model_entry(llm_default_models.get("embedding_model", EMBEDDING_MDL))
    rerank_entry = _parse_model_entry(llm_default_models.get("rerank_model", RERANK_MDL))
    asr_entry = _parse_model_entry(llm_default_models.get("asr_model", ASR_MDL))
    image2text_entry = _parse_model_entry(llm_default_models.get("image2text_model", IMAGE2TEXT_MDL))

    global CHAT_CFG, EMBEDDING_CFG, RERANK_CFG, ASR_CFG, IMAGE2TEXT_CFG
    CHAT_CFG = _resolve_per_model_config(chat_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)
    EMBEDDING_CFG = _resolve_per_model_config(embedding_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)
    RERANK_CFG = _resolve_per_model_config(rerank_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)
    ASR_CFG = _resolve_per_model_config(asr_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)
    IMAGE2TEXT_CFG = _resolve_per_model_config(image2text_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)

    CHAT_MDL = CHAT_CFG.get("model", "") or ""
    EMBEDDING_MDL = EMBEDDING_CFG.get("model", "") or ""
    compose_profiles = os.getenv("COMPOSE_PROFILES", "")
    if "tei-" in compose_profiles:
        EMBEDDING_MDL = os.getenv("TEI_MODEL", EMBEDDING_MDL or "BAAI/bge-small-en-v1.5")
    RERANK_MDL = RERANK_CFG.get("model", "") or ""
    ASR_MDL = ASR_CFG.get("model", "") or ""
    IMAGE2TEXT_MDL = IMAGE2TEXT_CFG.get("model", "") or ""

    # ⚠️ 裁剪:官方此处(L227-229)还从 ragflow 段读 HOST_IP/HOST_PORT 填入
    # settings——学习版入口 ragflow_server.py 自行从 conf 读(见该文件适配说明),
    # 不在这里重复填充, 避免两处配置源不一致。

    global GITHUB_OAUTH, FEISHU_OAUTH, OAUTH_CONFIG, CLIENT_AUTHENTICATION, HTTP_APP_KEY
    authentication_conf = get_base_config("authentication", {})
    CLIENT_AUTHENTICATION = authentication_conf.get("client", {}).get("switch", False)
    HTTP_APP_KEY = authentication_conf.get("client", {}).get("http_app_key")
    GITHUB_OAUTH = get_base_config("oauth", {}).get("github")
    FEISHU_OAUTH = get_base_config("oauth", {}).get("feishu")
    OAUTH_CONFIG = get_base_config("oauth", {})

    global SECRET_KEY
    SECRET_KEY = _get_or_create_secret_key()

    # 对象存储装配(官方 settings L289-305):类型分支读 conf 连接配置, 工厂实例化单例。
    # ⚠️ 官方此处还有 AWS_S3/AZURE/OSS/GCS 分支与 RAGFLOW_CRYPTO 加密包装, 学习版裁剪。
    global MINIO, STORAGE_IMPL
    if STORAGE_IMPL_TYPE == 'MINIO':
        MINIO = decrypt_database_config(name="minio")
    else:
        raise NotImplementedError(f"学习版仅支持 MINIO 对象存储, STORAGE_IMPL={STORAGE_IMPL_TYPE}")
    STORAGE_IMPL = StorageFactory.create(Storage[STORAGE_IMPL_TYPE])

    # 文档引擎装配(官方 settings L246-270):只支持 elasticsearch。
    # ES_CONN 单例在 import 时会真连 ES(官方 es_conn_pool 设计), 故延迟 import,
    # 且必须先 set ES 配置再实例化; ES 容器未起时这里会明确报错(官方同语义)。
    global docStoreConn, ES
    doc_engine = os.getenv("DOC_ENGINE", "elasticsearch").lower()
    if doc_engine == "elasticsearch":
        ES = get_base_config("es", {})
        from rag.utils.es_conn import ESConnection

        docStoreConn = ESConnection()
    else:
        raise NotImplementedError(f"学习版仅支持 elasticsearch 文档引擎, DOC_ENGINE={doc_engine}")

    # 检索器装配(官方 settings L323-327):retriever 挂上检索核心 Dealer,
    # kb_app 的 tags/知识图谱/检索类接口由延迟 import 点亮。
    global retriever, kg_retriever
    from rag.nlp.search import Dealer

    retriever = Dealer(docStoreConn)
    from rag.graphrag import search as kg_search

    kg_retriever = kg_search.KGSearch(docStoreConn)



