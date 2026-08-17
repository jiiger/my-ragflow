
# ============================================================
# constants.py — 全库常量与枚举的"词汇表"(仿写自 RAGFlow v0.24)
# 上层代码(settings/services/接口层/task_executor)都从这里取枚举和常量。
# 新代码需要枚举或魔法字符串时,优先在此登记,不要到处硬编码。
# 注意:枚举值(如 TaskStatus.RUNNING == "1")是字符串,存进 MySQL 的就是这些字符串。
# ============================================================

from enum import Enum, IntEnum

from strenum import StrEnum

# 项目后端的全局配置文件(conf/service_conf.yaml),由 common/config_utils 读取
SERVICE_CONF = "service_conf.yaml"
# 服务名(日志与注册标识)
RAG_FLOW_SERVICE_NAME = "ragflow"


class CustomEnum(Enum):
    """枚举基类,提供工具方法:valid(value) 判断值是否合法;values()/names() 取成员值/名列表。"""

    @classmethod
    def valid(cls, value):
        try:
            cls(value)
            return True
        except BaseException:  # noqa: BLE001 —— 原版写法:valid() 需拦截任何非法值,保持与 v0.24 一致
            return False

    @classmethod
    def values(cls):
        return [member.value for member in cls.__members__.values()]

    @classmethod
    def names(cls):
        return [member.name for member in cls.__members__.values()]


class RetCode(IntEnum, CustomEnum):
    """接口统一返回码:0=成功;10x=业务错误(参数/数据/权限);4xx=HTTP 语义;5xx=服务端错误。所有接口 get_json_result(code=...) 用它。"""

    SUCCESS = 0
    NOT_EFFECTIVE = 10
    EXCEPTION_ERROR = 100
    ARGUMENT_ERROR = 101
    DATA_ERROR = 102
    OPERATING_ERROR = 103
    CONNECTION_ERROR = 105
    RUNNING = 106
    PERMISSION_ERROR = 108
    AUTHENTICATION_ERROR = 109
    BAD_REQUEST = 400
    UNAUTHORIZED = 401
    SERVER_ERROR = 500
    FORBIDDEN = 403
    NOT_FOUND = 404
    CONFLICT = 409


class StatusEnum(Enum):
    """通用启停状态:"1"=有效/启用,"0"=无效。知识库、文档的 status 字段用这套值。"""

    VALID = "1"
    INVALID = "0"


class ActiveEnum(Enum):
    """通用激活状态:"1"=激活,"0"=未激活。"""

    ACTIVE = "1"
    INACTIVE = "0"


class LLMType(StrEnum):
    """模型类型分类:对应 rag/llm 的模型接入抽象,LLMBundle(tenant, LLMType.X, name) 按此类型取模型实例。"""

    CHAT = "chat"
    EMBEDDING = "embedding"
    SPEECH2TEXT = "speech2text"
    IMAGE2TEXT = "image2text"
    RERANK = "rerank"
    TTS = "tts"
    OCR = "ocr"


class TaskStatus(StrEnum):
    """文档任务状态机:UNSTART → RUNNING → DONE/FAIL/CANCEL;SCHEDULE 是连接器定时调度的常驻状态。documents.run 字段存这些字符串值。"""

    UNSTART = "0"
    RUNNING = "1"
    CANCEL = "2"
    DONE = "3"
    FAIL = "4"
    SCHEDULE = "5"


# 合法任务状态集合:接口过滤与日志校验用(如 list_pipeline_logs 的 operation_status 白名单)
VALID_TASK_STATUS = {
    TaskStatus.UNSTART,
    TaskStatus.RUNNING,
    TaskStatus.CANCEL,
    TaskStatus.DONE,
    TaskStatus.FAIL,
    TaskStatus.SCHEDULE,
}


class ParserType(StrEnum):
    """切块模板类型:task_executor 的 FACTORY 映射按 parser_id 的值选 rag/app 下的解析模板(naive→naive 模板等)。"""

    PRESENTATION = "presentation"
    LAWS = "laws"
    MANUAL = "manual"
    PAPER = "paper"
    RESUME = "resume"
    BOOK = "book"
    QA = "qa"
    TABLE = "table"
    NAIVE = "naive"
    PICTURE = "picture"
    ONE = "one"
    AUDIO = "audio"
    EMAIL = "email"
    KG = "knowledge_graph"
    TAG = "tag"


class FileSource(StrEnum):
    """文件来源(file 表 source_type 字段):LOCAL=上传区文件;KNOWLEDGEBASE=知识库文件树;其余是 connector 外部数据源。"""

    LOCAL = ""
    KNOWLEDGEBASE = "knowledgebase"
    S3 = "s3"
    NOTION = "notion"
    DISCORD = "discord"
    CONFLUENCE = "confluence"
    GMAIL = "gmail"
    GOOGLE_DRIVE = "google_drive"
    JIRA = "jira"
    SHAREPOINT = "sharepoint"
    SLACK = "slack"
    TEAMS = "teams"
    WEBDAV = "webdav"
    MOODLE = "moodle"
    DROPBOX = "dropbox"
    BOX = "box"
    R2 = "r2"
    OCI_STORAGE = "oci_storage"
    GOOGLE_CLOUD_STORAGE = "google_cloud_storage"
    AIRTABLE = "airtable"
    ASANA = "asana"
    GITHUB = "github"
    GITLAB = "gitlab"
    IMAP = "imap"
    BITBUCKET = "bitbucket"
    ZENDESK = "zendesk"
    SEAFILE = "seafile"
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"


class PipelineTaskType(StrEnum):
    """流水线任务类型:task 记录的 task_type 字段;PARSE=文档解析,DOWNLOAD=下载,后三者是知识增强(图谱三件套)。"""

    PARSE = "Parse"
    DOWNLOAD = "Download"
    RAPTOR = "RAPTOR"
    GRAPH_RAG = "GraphRAG"
    MINDMAP = "Mindmap"
    MEMORY = "Memory"


# 合法流水线任务类型集合(日志与过滤校验用)
VALID_PIPELINE_TASK_TYPES = {
    PipelineTaskType.PARSE,
    PipelineTaskType.DOWNLOAD,
    PipelineTaskType.RAPTOR,
    PipelineTaskType.GRAPH_RAG,
    PipelineTaskType.MINDMAP,
}


class MCPServerType(StrEnum):
    """MCP 服务器连接方式(迷你版暂不用,保留枚举以便对照原版)。"""

    SSE = "sse"
    STREAMABLE_HTTP = "streamable-http"


# 合法 MCP 服务器类型集合
VALID_MCP_SERVER_TYPES = {MCPServerType.SSE, MCPServerType.STREAMABLE_HTTP}


class Storage(Enum):
    """对象存储后端类型:STORAGE_IMPL 工厂按 settings 的 STORAGE_IMPL_TYPE 选择实现(迷你版只用 MINIO)。"""

    MINIO = 1
    AZURE_SPN = 2
    AZURE_SAS = 3
    AWS_S3 = 4
    OSS = 5
    OPENDAL = 6
    GCS = 7


class MemoryType(Enum):
    """记忆类型位标志(可按位或组合):RAW=原始 / SEMANTIC=语义 / EPISODIC=情节 / PROCEDURAL=程序(迷你版暂不用)。"""

    RAW = 0b0001  # 1 << 0 = 1 (0b00000001)
    SEMANTIC = 0b0010  # 1 << 1 = 2 (0b00000010)
    EPISODIC = 0b0100  # 1 << 2 = 4 (0b00000100)
    PROCEDURAL = 0b1000  # 1 << 3 = 8 (0b00001000)


class MemoryStorageType(StrEnum):
    """记忆存储形态:TABLE=存表 / GRAPH=存图(迷你版暂不用)。"""

    TABLE = "table"
    GRAPH = "graph"


class ForgettingPolicy(StrEnum):
    """记忆遗忘策略:FIFO=先入先出(迷你版暂不用)。"""

    FIFO = "FIFO"


# environment(环境变量名清单,原版注释保留,需要时取消注释启用)
# ENV_STRONG_TEST_COUNT = "STRONG_TEST_COUNT"
# ENV_RAGFLOW_SECRET_KEY = "RAGFLOW_SECRET_KEY"
# ENV_REGISTER_ENABLED = "REGISTER_ENABLED"
# ENV_DOC_ENGINE = "DOC_ENGINE"
# ENV_SANDBOX_ENABLED = "SANDBOX_ENABLED"
# ENV_SANDBOX_HOST = "SANDBOX_HOST"
# ENV_MAX_CONTENT_LENGTH = "MAX_CONTENT_LENGTH"
# ENV_COMPONENT_EXEC_TIMEOUT = "COMPONENT_EXEC_TIMEOUT"
# ENV_TRINO_USE_TLS = "TRINO_USE_TLS"
# ENV_MAX_FILE_NUM_PER_USER = "MAX_FILE_NUM_PER_USER"
# ENV_MACOS = "MACOS"
# ENV_RAGFLOW_DEBUGPY_LISTEN = "RAGFLOW_DEBUGPY_LISTEN"
# ENV_WERKZEUG_RUN_MAIN = "WERKZEUG_RUN_MAIN"
# ENV_DISABLE_SDK = "DISABLE_SDK"
# ENV_ENABLE_TIMEOUT_ASSERTION = "ENABLE_TIMEOUT_ASSERTION"
# ENV_LOG_LEVELS = "LOG_LEVELS"
# ENV_TENSORRT_DLA_SVR = "TENSORRT_DLA_SVR"
# ENV_OCR_GPU_MEM_LIMIT_MB = "OCR_GPU_MEM_LIMIT_MB"
# ENV_OCR_ARENA_EXTEND_STRATEGY = "OCR_ARENA_EXTEND_STRATEGY"
# ENV_MAX_CONCURRENT_PROCESS_AND_EXTRACT_CHUNK = "MAX_CONCURRENT_PROCESS_AND_EXTRACT_CHUNK"
# ENV_MAX_MAX_CONCURRENT_CHATS = "MAX_CONCURRENT_CHATS"
# ENV_RAGFLOW_MCP_BASE_URL = "RAGFLOW_MCP_BASE_URL"
# ENV_RAGFLOW_MCP_HOST = "RAGFLOW_MCP_HOST"
# ENV_RAGFLOW_MCP_PORT = "RAGFLOW_MCP_PORT"
# ENV_RAGFLOW_MCP_LAUNCH_MODE = "RAGFLOW_MCP_LAUNCH_MODE"
# ENV_RAGFLOW_MCP_HOST_API_KEY = "RAGFLOW_MCP_HOST_API_KEY"
# ENV_MINERU_EXECUTABLE = "MINERU_EXECUTABLE"
# ENV_MINERU_APISERVER = "MINERU_APISERVER"
# ENV_MINERU_OUTPUT_DIR = "MINERU_OUTPUT_DIR"
# ENV_MINERU_BACKEND = "MINERU_BACKEND"
# ENV_MINERU_DELETE_OUTPUT = "MINERU_DELETE_OUTPUT"
# ENV_TCADP_OUTPUT_DIR = "TCADP_OUTPUT_DIR"
# ENV_LM_TIMEOUT_SECONDS = "LM_TIMEOUT_SECONDS"
# ENV_LLM_MAX_RETRIES = "LLM_MAX_RETRIES"
# ENV_LLM_BASE_DELAY = "LLM_BASE_DELAY"
# ENV_OLLAMA_KEEP_ALIVE = "OLLAMA_KEEP_ALIVE"
# ENV_DOC_BULK_SIZE = "DOC_BULK_SIZE"
# ENV_EMBEDDING_BATCH_SIZE = "EMBEDDING_BATCH_SIZE"
# ENV_MAX_CONCURRENT_TASKS = "MAX_CONCURRENT_TASKS"
# ENV_MAX_CONCURRENT_CHUNK_BUILDERS = "MAX_CONCURRENT_CHUNK_BUILDERS"
# ENV_MAX_CONCURRENT_MINIO = "MAX_CONCURRENT_MINIO"
# ENV_WORKER_HEARTBEAT_TIMEOUT = "WORKER_HEARTBEAT_TIMEOUT"
# ENV_TRACE_MALLOC_ENABLED = "TRACE_MALLOC_ENABLED"


# ---- 检索层(docStore)专用字段名 ----
# 图谱节点重要度字段(仅 Elasticsearch 支持,Infinity 引擎下不允许设置)
PAGERANK_FLD = "pagerank_fea"
# 任务队列基名:get_svr_queue_name(priority) 按优先级拼后缀(0→原名,1→原名_1)
SVR_QUEUE_NAME = "rag_flow_svr_queue"
# 任务队列消费组名:task_executor 以消费组成员身份读消息(get_unacked_iterator/queue_consumer 都用它)
SVR_CONSUMER_GROUP_NAME = "rag_flow_svr_task_broker"
# 切块标签字段(标签存在切块的 tag_feas 上,检索层聚合)
TAG_FLD = "tag_feas"


# ---- 视觉解析引擎配置(deepdoc 阶段才用,先保留原版默认值) ----
# MinerU 文档解析器的环境变量键与默认配置
MINERU_ENV_KEYS = [
    "MINERU_APISERVER",
    "MINERU_OUTPUT_DIR",
    "MINERU_BACKEND",
    "MINERU_SERVER_URL",
    "MINERU_DELETE_OUTPUT",
]
MINERU_DEFAULT_CONFIG = {
    "MINERU_APISERVER": "",
    "MINERU_OUTPUT_DIR": "",
    "MINERU_BACKEND": "pipeline",
    "MINERU_SERVER_URL": "",
    "MINERU_DELETE_OUTPUT": 1,
}

# PaddleOCR 的环境变量键与默认配置
PADDLEOCR_ENV_KEYS = [
    "PADDLEOCR_API_URL",
    "PADDLEOCR_ACCESS_TOKEN",
    "PADDLEOCR_ALGORITHM",
]
PADDLEOCR_DEFAULT_CONFIG = {
    "PADDLEOCR_API_URL": "",
    "PADDLEOCR_ACCESS_TOKEN": None,
    "PADDLEOCR_ALGORITHM": "PaddleOCR-VL",
}