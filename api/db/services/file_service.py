"""api/db/services/file_service.py — 文件/文件夹服务, 对齐官方 v0.24.0。

移植说明(与官方差异):
- ⚠️ GptV4(rag.llm.cv_model)未移植 → parse 内延迟 import(官方在文件顶部)
- ⚠️ TaskService(task_service.py 未移植) → delete_docs 内延迟 import
- ⚠️ settings.STORAGE_IMPL 学习版精简 init_settings 未初始化(=None) → 涉及存储读写的
  方法(upload_document/get_blob/put_blob/delete_docs/upload_info)运行时需先建存储链
- 其余 34 个方法与官方 1:1; 用户手写的前 3 个方法(get_by_pf_id/get_kb_id_by_file_id/get_by_pf_id_name)保留
"""

import asyncio
import base64
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Union

from peewee import fn

from api.db import KNOWLEDGEBASE_FOLDER_NAME, FileType
from api.db.db_models import DB, Document, File, File2Document, Knowledgebase, Task
from api.db.services import duplicate_name
from api.db.services.common_service import CommonService
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from common.misc_utils import get_uuid
from common.constants import TaskStatus, FileSource, ParserType
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.file_utils import filename_type, read_potential_broken_pdf, thumbnail_img, sanitize_path
from common import settings


class FileService(CommonService):
    # Service class for managing file operations and storage
    model = File

    @classmethod
    @DB.connection_context()
    def get_by_pf_id(cls, tenant_id, pf_id, page_number, items_per_page, orderby, desc, keywords):
        """分页查询某父目录下的文件列表(文件夹算大小/子目录, 文件算关联知识库), 返回 (列表, 总数)。"""
        if keywords:
            files = cls.model.select().where((cls.model.tenant_id == tenant_id), (cls.model.parent_id == pf_id), (fn.LOWER(cls.model.name).contains(keywords.lower())), ~(cls.model.id == pf_id))
        else:
            files = cls.model.select().where((cls.model.tenant_id == tenant_id), (cls.model.parent_id == pf_id), ~(cls.model.id == pf_id))
        count = files.count()

        if desc:
            files = files.order_by(cls.model.getter_by(orderby).desc())
        else:
            files = files.order_by(cls.model.getter_by(orderby).asc())

        files = files.paginate(page_number, items_per_page)
        res_files = list(files.dicts())

        for file in res_files:
            if file["type"] == FileType.FOLDER.value:
                file["size"] = cls.get_folder_size(file["id"])
                file["kbs_info"] = []
                children = list(
                    cls.model.select()
                    .where(
                        (cls.model.tenant_id == tenant_id),
                        (cls.model.parent_id == file["id"]),
                        ~(cls.model.id == file["id"]),
                    )
                    .dicts()
                )
                file["has_child_folder"] = any(value["type"] == FileType.FOLDER.value for value in children)
                continue
            kbs_info = cls.get_kb_id_by_file_id(file["id"])
            file["kbs_info"] = kbs_info

        return res_files, count

    @classmethod
    @DB.connection_context()
    def get_kb_id_by_file_id(cls, file_id):
        """顺着 File→File2Document→Document→Knowledgebase 的 join 找出文件关联的知识库列表。"""
        kbs = (
            cls.model.select(*[Knowledgebase.id, Knowledgebase.name, File2Document.document_id])
            .join(File2Document, on=(File2Document.file_id == file_id))
            .join(Document, on=(File2Document.document_id == Document.id))
            .join(Knowledgebase, on=(Knowledgebase.id == Document.kb_id))
            .where(cls.model.id == file_id)
        )
        if not kbs:
            return []
        kbs_info_list = []
        for kb in list(kbs.dicts()):
            kbs_info_list.append({"kb_id": kb["id"], "kb_name": kb["name"], "document_id": kb["document_id"]})
        return kbs_info_list

    @classmethod
    @DB.connection_context()
    def get_by_pf_id_name(cls, id, name):
        """按父目录 id + 名字精确查文件, 找不到返回 None。"""
        file = cls.model.select().where((cls.model.parent_id == id) & (cls.model.name == name))
        if file.count():
            e, file = cls.get_by_id(file[0].id)
            if not e:
                raise RuntimeError("Database error (File retrieval)!")
            return file
        return None

    @classmethod
    @DB.connection_context()
    def get_id_list_by_id(cls, id, name, count, res):
        """按路径名字列表逐层下钻, 把每一层的文件夹 id 收进 res(路径定位用)。"""
        if count < len(name):
            file = cls.get_by_pf_id_name(id, name[count])
            if file:
                res.append(file.id)
                return cls.get_id_list_by_id(file.id, name, count + 1, res)
            else:
                return res
        else:
            return res

    @classmethod
    @DB.connection_context()
    def get_all_innermost_file_ids(cls, folder_id, result_ids):
        """递归收集"最里层"的文件夹/文件 id(没有子目录的节点), 删除整棵子树时用。"""
        subfolders = cls.model.select().where(cls.model.parent_id == folder_id)
        if subfolders.exists():
            for subfolder in subfolders:
                cls.get_all_innermost_file_ids(subfolder.id, result_ids)
        else:
            result_ids.append(folder_id)
        return result_ids

    @classmethod
    @DB.connection_context()
    def get_all_file_ids_by_tenant_id(cls, tenant_id):
        """分页取租户下全部文件 id(create_time 升序, 每批 100 条)。"""
        fields = [cls.model.id]
        files = cls.model.select(*fields).where(cls.model.tenant_id == tenant_id)
        files.order_by(cls.model.create_time.asc())
        offset, limit = 0, 100
        res = []
        while True:
            file_batch = files.offset(offset).limit(limit)
            _temp = list(file_batch.dicts())
            if not _temp:
                break
            res.extend(_temp)
            offset += limit
        return res

    @classmethod
    @DB.connection_context()
    def create_folder(cls, file, parent_id, name, count):
        from api.apps import current_user
        """按名字列表递归建目录(建到倒数第二层, 最后一层由调用方处理)。"""
        if count > len(name) - 2:
            return file
        else:
            file = cls.insert(
                {"id": get_uuid(), "parent_id": parent_id, "tenant_id": current_user.id, "created_by": current_user.id, "name": name[count], "location": "", "size": 0, "type": FileType.FOLDER.value}
            )
            return cls.create_folder(file, file.id, name, count + 1)

    @classmethod
    @DB.connection_context()
    def is_parent_folder_exist(cls, parent_id):
        """父目录是否存在; 不存在时顺手清掉它名下的孤儿文件并返回 False。"""
        parent_files = cls.model.select().where(cls.model.id == parent_id)
        if parent_files.count():
            return True
        # ⚠️ 官方疑似 bug: delete_folder_by_pf_id 签名是 (user_id, folder_id) 两个参数,
        #    这里只传了 parent_id; 保持原样以便 diff(该分支基本不会触发)
        cls.delete_folder_by_pf_id(parent_id)
        return False

    @classmethod
    @DB.connection_context()
    def get_root_folder(cls, tenant_id):
        """取租户根目录(自引用 parent_id==id 的 "/"), 不存在则创建。"""
        for file in cls.model.select().where((cls.model.tenant_id == tenant_id), (cls.model.parent_id == cls.model.id)):
            return file.to_dict()

        file_id = get_uuid()
        file = {
            "id": file_id,
            "parent_id": file_id,
            "tenant_id": tenant_id,
            "created_by": tenant_id,
            "name": "/",
            "type": FileType.FOLDER.value,
            "size": 0,
            "location": "",
        }
        cls.save(**file)
        return file

    @classmethod
    @DB.connection_context()
    def get_kb_folder(cls, tenant_id):
        """取租户的"知识库"总目录(.knowledgebase, 挂在根目录下), 不存在则创建。"""
        root_folder = cls.get_root_folder(tenant_id)
        root_id = root_folder["id"]
        kb_folder = cls.model.select().where((cls.model.tenant_id == tenant_id), (cls.model.parent_id == root_id), (cls.model.name == KNOWLEDGEBASE_FOLDER_NAME)).first()
        if not kb_folder:
            kb_folder = cls.new_a_file_from_kb(tenant_id, KNOWLEDGEBASE_FOLDER_NAME, root_id)
            return kb_folder
        return kb_folder.to_dict()

    @classmethod
    @DB.connection_context()
    def new_a_file_from_kb(cls, tenant_id, name, parent_id, ty=FileType.FOLDER.value, size=0, location=""):
        """在知识库目录树下新建一个文件/目录记录(同名字存在则直接返回已有的)。"""
        for file in cls.query(tenant_id=tenant_id, parent_id=parent_id, name=name):
            return file.to_dict()
        file = {
            "id": get_uuid(),
            "parent_id": parent_id,
            "tenant_id": tenant_id,
            "created_by": tenant_id,
            "name": name,
            "type": ty,
            "size": size,
            "location": location,
            "source_type": FileSource.KNOWLEDGEBASE,
        }
        cls.save(**file)
        return file

    @classmethod
    @DB.connection_context()
    def init_knowledgebase_docs(cls, root_id, tenant_id):
        """把知识库目录树初始化为文件树: .knowledgebase → 每个 kb 一个文件夹 → 每个文档一个文件。"""
        for _ in cls.model.select().where((cls.model.name == KNOWLEDGEBASE_FOLDER_NAME) & (cls.model.parent_id == root_id)):
            return
        folder = cls.new_a_file_from_kb(tenant_id, KNOWLEDGEBASE_FOLDER_NAME, root_id)

        for kb in Knowledgebase.select(*[Knowledgebase.id, Knowledgebase.name]).where(Knowledgebase.tenant_id == tenant_id):
            kb_folder = cls.new_a_file_from_kb(tenant_id, kb.name, folder["id"])
            for doc in DocumentService.query(kb_id=kb.id):
                FileService.add_file_from_kb(doc.to_dict(), kb_folder["id"], tenant_id)

    @classmethod
    @DB.connection_context()
    def get_parent_folder(cls, file_id):
        """取文件的直属父目录, 文件或父目录不存在都抛异常。"""
        file = cls.model.select().where(cls.model.id == file_id)
        if file.count():
            e, file = cls.get_by_id(file[0].parent_id)
            if not e:
                raise RuntimeError("Database error (File retrieval)!")
        else:
            raise RuntimeError("Database error (File doesn't exist)!")
        return file

    @classmethod
    @DB.connection_context()
    def get_all_parent_folders(cls, start_id):
        """从 start_id 一路向上收集全部祖先目录(到根为止, 含自身)。"""
        parent_folders = []
        current_id = start_id
        while current_id:
            e, file = cls.get_by_id(current_id)
            if e and file.parent_id != file.id:
                parent_folders.append(file)
                current_id = file.parent_id
            else:
                parent_folders.append(file)
                break
        return parent_folders

    @classmethod
    @DB.connection_context()
    def insert(cls, file):
        """插入一条文件记录并返回模型实例(user_register 建根目录走的入口)。"""
        if not cls.save(**file):
            raise RuntimeError("Database error (File)!")
        return File(**file)

    @classmethod
    @DB.connection_context()
    def delete(cls, file):
        """按模型实例删除(即 delete_by_id)。"""
        return cls.delete_by_id(file.id)

    @classmethod
    @DB.connection_context()
    def delete_by_pf_id(cls, folder_id):
        """删除某父目录下的全部直接子文件记录(不递归)。"""
        return cls.model.delete().where(cls.model.parent_id == folder_id).execute()

    @classmethod
    @DB.connection_context()
    def delete_folder_by_pf_id(cls, user_id, folder_id):
        """递归删除用户某目录整棵子树(先子后父), 返回删除结果元组。"""
        try:
            files = cls.model.select().where((cls.model.tenant_id == user_id) & (cls.model.parent_id == folder_id))
            for file in files:
                cls.delete_folder_by_pf_id(user_id, file.id)
            return (cls.model.delete().where((cls.model.tenant_id == user_id) & (cls.model.id == folder_id)).execute(),)
        except Exception:
            logging.exception("delete_folder_by_pf_id")
            raise RuntimeError("Database error (File retrieval)!")

    @classmethod
    @DB.connection_context()
    def get_file_count(cls, tenant_id):
        """租户文件总数。"""
        files = cls.model.select(cls.model.id).where(cls.model.tenant_id == tenant_id)
        return len(files)

    @classmethod
    @DB.connection_context()
    def get_folder_size(cls, folder_id):
        """递归累加文件夹下所有子孙文件的 size(DFS)。"""
        size = 0

        def dfs(parent_id):
            nonlocal size
            for f in cls.model.select(*[cls.model.id, cls.model.size, cls.model.type]).where(cls.model.parent_id == parent_id, cls.model.id != parent_id):
                size += f.size
                if f.type == FileType.FOLDER.value:
                    dfs(f.id)

        dfs(folder_id)
        return size

    @classmethod
    @DB.connection_context()
    def add_file_from_kb(cls, doc, kb_folder_id, tenant_id):
        """把一个文档映射成知识库目录下的文件记录(一个文档只映射一次, File2Document 同时落库)。"""
        for _ in File2DocumentService.get_by_document_id(doc["id"]):
            return
        file = {
            "id": get_uuid(),
            "parent_id": kb_folder_id,
            "tenant_id": tenant_id,
            "created_by": tenant_id,
            "name": doc["name"],
            "type": doc["type"],
            "size": doc["size"],
            "location": doc["location"],
            "source_type": FileSource.KNOWLEDGEBASE,
        }
        cls.save(**file)
        File2DocumentService.save(**{"id": get_uuid(), "file_id": file["id"], "document_id": doc["id"]})

    @classmethod
    @DB.connection_context()
    def move_file(cls, file_ids, folder_id):
        """批量把文件移动到目标目录(改 parent_id)。"""
        try:
            cls.filter_update((cls.model.id << file_ids,), {"parent_id": folder_id})
        except Exception:
            logging.exception("move_file")
            raise RuntimeError("Database error (File move)!")

    @classmethod
    @DB.connection_context()
    def upload_document(self, kb, file_objs, user_id, src="local", parent_path: str | None = None):
        """上传文档到知识库: 落对象存储 + 建 Document 记录 + 映射文件树。

        ⚠️ 依赖 settings.STORAGE_IMPL(学习版精简 init_settings 未初始化, =None), 存储链建成后才能跑。
        """
        root_folder = self.get_root_folder(user_id)
        pf_id = root_folder["id"]
        self.init_knowledgebase_docs(pf_id, user_id)
        kb_root_folder = self.get_kb_folder(user_id)
        kb_folder = self.new_a_file_from_kb(kb.tenant_id, kb.name, kb_root_folder["id"])

        safe_parent_path = sanitize_path(parent_path)

        err, files = [], []
        for file in file_objs:
            doc_id = file.id if hasattr(file, "id") else get_uuid()
            e, doc = DocumentService.get_by_id(doc_id)
            if e:
                blob = file.read()
                settings.STORAGE_IMPL.put(kb.id, doc.location, blob, kb.tenant_id)
                doc.size = len(blob)
                doc = doc.to_dict()
                DocumentService.update_by_id(doc["id"], doc)
                continue
            try:
                DocumentService.check_doc_health(kb.tenant_id, file.filename)
                filename = duplicate_name(DocumentService.query, name=file.filename, kb_id=kb.id)
                filetype = filename_type(filename)
                if filetype == FileType.OTHER.value:
                    raise RuntimeError("This type of file has not been supported yet!")

                location = filename if not safe_parent_path else f"{safe_parent_path}/{filename}"
                while settings.STORAGE_IMPL.obj_exist(kb.id, location):
                    location += "_"

                blob = file.read()
                if filetype == FileType.PDF.value:
                    blob = read_potential_broken_pdf(blob)
                settings.STORAGE_IMPL.put(kb.id, location, blob)

                img = thumbnail_img(filename, blob)
                thumbnail_location = ""
                if img is not None:
                    thumbnail_location = f"thumbnail_{doc_id}.png"
                    settings.STORAGE_IMPL.put(kb.id, thumbnail_location, img)

                doc = {
                    "id": doc_id,
                    "kb_id": kb.id,
                    "parser_id": self.get_parser(filetype, filename, kb.parser_id),
                    "pipeline_id": kb.pipeline_id,
                    "parser_config": kb.parser_config,
                    "created_by": user_id,
                    "type": filetype,
                    "name": filename,
                    "source_type": src,
                    "suffix": Path(filename).suffix.lstrip("."),
                    "location": location,
                    "size": len(blob),
                    "thumbnail": thumbnail_location,
                }
                DocumentService.insert(doc)

                FileService.add_file_from_kb(doc, kb_folder["id"], kb.tenant_id)
                files.append((doc, blob))
            except Exception as e:
                err.append(file.filename + ": " + str(e))

        return err, files

    @classmethod
    @DB.connection_context()
    def list_all_files_by_parent_id(cls, parent_id):
        """父目录下的全部直接子文件(不含目录自身)。"""
        try:
            files = cls.model.select().where((cls.model.parent_id == parent_id) & (cls.model.id != parent_id))
            return list(files)
        except Exception:
            logging.exception("list_by_parent_id failed")
            raise RuntimeError("Database error (list_by_parent_id)!")

    @staticmethod
    def parse_docs(file_objs, user_id):
        """并发(12 线程)解析多个文件, 返回拼接后的纯文本。"""
        exe = ThreadPoolExecutor(max_workers=12)
        threads = []
        for file in file_objs:
            threads.append(exe.submit(FileService.parse, file.filename, file.read(), False))

        res = []
        for th in threads:
            res.append(th.result())

        return "\n\n".join(res)

    @staticmethod
    def parse(filename, blob, img_base64=True, tenant_id=None):
        """按解析器工厂对文件内容做切块解析, 返回带文件头的文本块拼接。

        ⚠️ 依赖 rag.app(解析器注册表, 学习版未移植)与 GptV4(rag.llm.cv_model 裁剪), 延迟 import;
          解析链(deepdoc/rag.app)建成后才能跑。
        """
        from rag.app import audio, email, naive, picture, presentation
        # ⚠️ 官方在文件顶部 import GptV4; rag.llm.cv_model 学习版裁剪(用户只要 Qwen), 改到这里延迟
        from rag.llm.cv_model import GptV4
        from api.apps import current_user

        def dummy(prog=None, msg=""):
            pass

        FACTORY = {ParserType.PRESENTATION.value: presentation, ParserType.PICTURE.value: picture, ParserType.AUDIO.value: audio, ParserType.EMAIL.value: email}
        parser_config = {"chunk_token_num": 16096, "delimiter": "\n!?;。；！？", "layout_recognize": "Plain Text"}
        kwargs = {"lang": "English", "callback": dummy, "parser_config": parser_config, "from_page": 0, "to_page": 100000, "tenant_id": current_user.id if current_user else tenant_id}
        file_type = filename_type(filename)
        if img_base64 and file_type == FileType.VISUAL.value:
            return GptV4.image2base64(blob)
        cks = FACTORY.get(FileService.get_parser(filename_type(filename), filename, ""), naive).chunk(filename, blob, **kwargs)
        return f"\n -----------------\nFile: {filename}\nContent as following: \n" + "\n".join([ck["content_with_weight"] for ck in cks])

    @staticmethod
    def get_parser(doc_type, filename, default):
        """文件类型 → 解析器 id: 图像→picture / 音频→audio / ppt/邮件按扩展名特判, 其余用默认。"""
        if doc_type == FileType.VISUAL:
            return ParserType.PICTURE.value
        if doc_type == FileType.AURAL:
            return ParserType.AUDIO.value
        if re.search(r"\.(ppt|pptx|pages)$", filename):
            return ParserType.PRESENTATION.value
        if re.search(r"\.(msg|eml)$", filename):
            return ParserType.EMAIL.value
        return default

    @staticmethod
    def get_blob(user_id, location):
        """从对象存储读取用户下载区({user_id}-downloads)对象。

        ⚠️ 依赖 settings.STORAGE_IMPL(未初始化), 存储链建成后可用。
        """
        bname = f"{user_id}-downloads"
        return settings.STORAGE_IMPL.get(bname, location)

    @staticmethod
    def put_blob(user_id, location, blob):
        """写入对象存储用户下载区。

        ⚠️ 依赖 settings.STORAGE_IMPL(未初始化), 存储链建成后可用。
        """
        bname = f"{user_id}-downloads"
        return settings.STORAGE_IMPL.put(bname, location, blob)

    @classmethod
    @DB.connection_context()
    def delete_docs(cls, doc_ids, tenant_id):
        """删除文档: 停任务 → 删 Document → 删 File2Document 映射 → 删文件记录 → 删对象存储数据。

        ⚠️ TaskService(task_service.py 未移植)延迟 import; ⚠️ settings.STORAGE_IMPL 未初始化。
        """
        # ⚠️ 延迟 import: task_service.py 未移植, 移植后移到文件顶部
        from api.db.services.task_service import TaskService

        root_folder = FileService.get_root_folder(tenant_id)
        pf_id = root_folder["id"]
        FileService.init_knowledgebase_docs(pf_id, tenant_id)
        errors = ""
        kb_table_num_map = {}
        for doc_id in doc_ids:
            try:
                e, doc = DocumentService.get_by_id(doc_id)
                if not e:
                    raise Exception("Document not found!")
                tenant_id = DocumentService.get_tenant_id(doc_id)
                if not tenant_id:
                    raise Exception("Tenant not found!")

                b, n = File2DocumentService.get_storage_address(doc_id=doc_id)

                TaskService.filter_delete([Task.doc_id == doc_id])
                if not DocumentService.remove_document(doc, tenant_id):
                    raise Exception("Database error (Document removal)!")

                f2d = File2DocumentService.get_by_document_id(doc_id)
                deleted_file_count = 0
                if f2d:
                    deleted_file_count = FileService.filter_delete([File.source_type == FileSource.KNOWLEDGEBASE, File.id == f2d[0].file_id])
                File2DocumentService.delete_by_document_id(doc_id)
                if deleted_file_count > 0:
                    settings.STORAGE_IMPL.rm(b, n)

                doc_parser = doc.parser_id
                if doc_parser == ParserType.TABLE:
                    kb_id = doc.kb_id
                    if kb_id not in kb_table_num_map:
                        counts = DocumentService.count_by_kb_id(kb_id=kb_id, keywords="", run_status=[TaskStatus.DONE], types=[])
                        kb_table_num_map[kb_id] = counts
                    kb_table_num_map[kb_id] -= 1
                    if kb_table_num_map[kb_id] <= 0:
                        KnowledgebaseService.delete_field_map(kb_id)
            except Exception as e:
                errors += str(e)

        return errors

    @staticmethod
    def upload_info(user_id, file, url: str|None=None):
        """把单个上传文件(或 URL 抓取结果)落到存储, 返回给前端的文件元信息 dict。

        url 分支用 crawl4ai 抓网页(官方延迟 import, 学习版未装 crawl4ai), 本地文件分支直接落存储。
        ⚠️ 依赖 settings.STORAGE_IMPL(未初始化)。
        """
        def structured(filename, filetype, blob, content_type):
            nonlocal user_id
            if filetype == FileType.PDF.value:
                blob = read_potential_broken_pdf(blob)

            location = get_uuid()
            FileService.put_blob(user_id, location, blob)

            return {
                "id": location,
                "name": filename,
                "size": sys.getsizeof(blob),
                "extension": filename.split(".")[-1].lower(),
                "mime_type": content_type,
                "created_by": user_id,
                "created_at": time.time(),
                "preview_url": None
            }

        if url:
            from crawl4ai import (
                AsyncWebCrawler,
                BrowserConfig,
                CrawlerRunConfig,
                DefaultMarkdownGenerator,
                PruningContentFilter,
                CrawlResult
            )
            filename = re.sub(r"\?.*", "", url.split("/")[-1])
            async def adownload():
                browser_config = BrowserConfig(
                    headless=True,
                    verbose=False,
                )
                async with AsyncWebCrawler(config=browser_config) as crawler:
                    crawler_config = CrawlerRunConfig(
                        markdown_generator=DefaultMarkdownGenerator(
                            content_filter=PruningContentFilter()
                        ),
                        pdf=True,
                        screenshot=False
                    )
                    result: CrawlResult = await crawler.arun(
                        url=url,
                        config=crawler_config
                    )
                    return result
            page = asyncio.run(adownload())
            if page.pdf:
                if filename.split(".")[-1].lower() != "pdf":
                    filename += ".pdf"
                return structured(filename, "pdf", page.pdf, page.response_headers["content-type"])

            return structured(filename, "html", str(page.markdown).encode("utf-8"), page.response_headers["content-type"], user_id)

        DocumentService.check_doc_health(user_id, file.filename)
        return structured(file.filename, filename_type(file.filename), file.read(), file.content_type)

    @staticmethod
    def get_files(files: Union[None, list[dict]]) -> list[str]:
        """把文件元信息列表转成内容列表: 图片转 base64 data-URL, 其余走 parse 解析成文本。"""
        if not files:
            return  []
        def image_to_base64(file):
            return "data:{};base64,{}".format(file["mime_type"],
                                        base64.b64encode(FileService.get_blob(file["created_by"], file["id"])).decode("utf-8"))
        exe = ThreadPoolExecutor(max_workers=5)
        threads = []
        for file in files:
            if file["mime_type"].find("image") >=0:
                threads.append(exe.submit(image_to_base64, file))
                continue
            threads.append(exe.submit(FileService.parse, file["name"], FileService.get_blob(file["created_by"], file["id"]), True, file["created_by"]))
        return [th.result() for th in threads]