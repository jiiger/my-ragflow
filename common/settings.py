import os

from common.config_utils import decrypt_database_config

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



