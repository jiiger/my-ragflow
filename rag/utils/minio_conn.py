# -*- coding: utf-8 -*-
#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""MinIO 对象存储连接器:settings.STORAGE_IMPL 的默认实现。

移植自官方 v0.24.0 rag/utils/minio_conn.py(278 行, 逻辑 1:1)。
上传链路里 FileService.upload_document 的 settings.STORAGE_IMPL.put / obj_exist /
get 全部由它兑现;init_settings 里 StorageFactory 按 STORAGE_IMPL_TYPE 装配。

关键设计:
- 单例(@singleton), 连接在 __init__ 里惰性建立, 失败只记日志不抛(每次操作
  前也有 try/except + 重连兜底), 所以容器没起时 import/装配都不崩。
- bucket 双模式: conf 里 minio.bucket 为空 → 每个知识库一个真实桶(put 时
  make_bucket);配置了默认桶 → 单一物理桶, 知识库 id 作为对象路径前缀
  (use_default_bucket/use_prefix_path 两个装饰器 + _resolve_bucket_and_path 实现)。
- 与官方 1:1, 唯一的裁剪: 无(全部方法保留, 含 copy/move/remove_bucket)。
"""

import logging
import time
from io import BytesIO

from minio import Minio
from minio.commonconfig import CopySource
from minio.error import InvalidResponseError, S3Error, ServerError

from common import settings
from common.decorator import singleton


@singleton
class RAGFlowMinio:
    def __init__(self):
        self.conn = None
        # Use `or None` to convert empty strings to None, ensuring single-bucket
        # mode is truly disabled when not configured
        self.bucket = settings.MINIO.get('bucket', None) or None
        self.prefix_path = settings.MINIO.get('prefix_path', None) or None
        self.__open__()

    @staticmethod
    def use_default_bucket(method):
        """装饰器: 配置了默认桶时把入参 bucket 换成物理桶, 原标识经 _orig_bucket 传递。"""
        def wrapper(self, bucket, *args, **kwargs):
            # If there is a default bucket, use the default bucket
            # but preserve the original bucket identifier so it can be
            # used as a path prefix inside the physical/default bucket.
            original_bucket = bucket
            actual_bucket = self.bucket if self.bucket else bucket
            if self.bucket:
                # pass original identifier forward for use by other decorators
                kwargs['_orig_bucket'] = original_bucket
            return method(self, actual_bucket, *args, **kwargs)

        return wrapper

    @staticmethod
    def use_prefix_path(method):
        """装饰器: 把知识库 id(原 bucket)拼进对象路径, 形成 物理桶/<kb_id>/<path> 布局。"""
        def wrapper(self, bucket, fnm, *args, **kwargs):
            # If a default MINIO bucket is configured, the use_default_bucket
            # decorator will have replaced the `bucket` arg with the physical
            # bucket name and forwarded the original identifier as `_orig_bucket`.
            # Prefer that original identifier when constructing the key path so
            # objects are stored under <physical-bucket>/<identifier>/...
            orig_bucket = kwargs.pop('_orig_bucket', None)

            if self.prefix_path:
                # If a prefix_path is configured, include it and then the identifier
                if orig_bucket:
                    fnm = f"{self.prefix_path}/{orig_bucket}/{fnm}"
                else:
                    fnm = f"{self.prefix_path}/{fnm}"
            else:
                # No prefix_path configured. If orig_bucket exists and the
                # physical bucket equals configured default, use orig_bucket as a path.
                if orig_bucket and bucket == self.bucket:
                    fnm = f"{orig_bucket}/{fnm}"

            return method(self, bucket, fnm, *args, **kwargs)

        return wrapper

    def __open__(self):
        try:
            if self.conn:
                self.__close__()
        except Exception:
            pass

        try:
            self.conn = Minio(settings.MINIO["host"],
                              access_key=settings.MINIO["user"],
                              secret_key=settings.MINIO["password"],
                              secure=False
                              )
        except Exception:
            logging.exception(
                "Fail to connect %s " % settings.MINIO["host"])

    def __close__(self):
        del self.conn
        self.conn = None

    def health(self):
        """
        Check MinIO service availability.
        """
        try:
            if self.bucket:
                # Single-bucket mode: check bucket exists only (no side effects)
                exists = self.conn.bucket_exists(self.bucket)

                # Historical:
                # - Previously wrote "_health_check" to verify write permissions
                # - Previously auto-created bucket if missing

                return exists
            else:
                # Multi-bucket mode: verify MinIO service connectivity
                self.conn.list_buckets()
                return True
        except (S3Error, ServerError, InvalidResponseError):
            return False
        except Exception as e:
            logging.warning(f"Unexpected error in MinIO health check: {e}")
            return False

    @use_default_bucket
    @use_prefix_path
    def put(self, bucket, fnm, binary, tenant_id=None):
        """写对象: 多桶模式下桶不存在先 make_bucket; 失败重连重试 3 次。"""
        for _ in range(3):
            try:
                # Note: bucket must already exist - we don't have permission to create buckets
                if not self.bucket and not self.conn.bucket_exists(bucket):
                    self.conn.make_bucket(bucket)

                r = self.conn.put_object(bucket, fnm,
                                         BytesIO(binary),
                                         len(binary)
                                         )
                return r
            except Exception:
                logging.exception(f"Fail to put {bucket}/{fnm}:")
                self.__open__()
                time.sleep(1)

    @use_default_bucket
    @use_prefix_path
    def rm(self, bucket, fnm, tenant_id=None):
        try:
            self.conn.remove_object(bucket, fnm)
        except Exception:
            logging.exception(f"Fail to remove {bucket}/{fnm}:")

    @use_default_bucket
    @use_prefix_path
    def get(self, bucket, filename, tenant_id=None):
        """读对象(整个读进内存返回 bytes); 失败重连重试 1 次。"""
        for _ in range(1):
            try:
                r = self.conn.get_object(bucket, filename)
                return r.read()
            except Exception:
                logging.exception(f"Fail to get {bucket}/{filename}")
                self.__open__()
                time.sleep(1)
        return

    @use_default_bucket
    @use_prefix_path
    def obj_exist(self, bucket, filename, tenant_id=None):
        """对象是否存在: NoSuchKey/NoSuchBucket 等 S3 错误码视为不存在。"""
        try:
            if not self.conn.bucket_exists(bucket):
                return False
            if self.conn.stat_object(bucket, filename):
                return True
            else:
                return False
        except S3Error as e:
            if e.code in ["NoSuchKey", "NoSuchBucket", "ResourceNotFound"]:
                return False
        except Exception:
            logging.exception(f"obj_exist {bucket}/{filename} got exception")
            return False

    @use_default_bucket
    def bucket_exists(self, bucket):
        try:
            if not self.conn.bucket_exists(bucket):
                return False
            else:
                return True
        except S3Error as e:
            if e.code in ["NoSuchKey", "NoSuchBucket", "ResourceNotFound"]:
                return False
        except Exception:
            logging.exception(f"bucket_exist {bucket} got exception")
            return False

    @use_default_bucket
    @use_prefix_path
    def get_presigned_url(self, bucket, fnm, expires, tenant_id=None):
        """生成临时访问链接(缩略图/下载场景), 失败重连重试 10 次。"""
        for _ in range(10):
            try:
                return self.conn.get_presigned_url("GET", bucket, fnm, expires)
            except Exception:
                logging.exception(f"Fail to get_presigned {bucket}/{fnm}:")
                self.__open__()
                time.sleep(1)
        return

    @use_default_bucket
    def remove_bucket(self, bucket, **kwargs):
        """删除知识库时清空其对象: 多桶模式删真实桶, 单桶模式只清该 kb 前缀下的对象。"""
        orig_bucket = kwargs.pop('_orig_bucket', None)
        try:
            if self.bucket:
                # Single bucket mode: remove objects with prefix
                prefix = ""
                if self.prefix_path:
                    prefix = f"{self.prefix_path}/"
                if orig_bucket:
                    prefix += f"{orig_bucket}/"

                # List objects with prefix
                objects_to_delete = self.conn.list_objects(bucket, prefix=prefix, recursive=True)
                for obj in objects_to_delete:
                    self.conn.remove_object(bucket, obj.object_name)
                # Do NOT remove the physical bucket
            else:
                if self.conn.bucket_exists(bucket):
                    objects_to_delete = self.conn.list_objects(bucket, recursive=True)
                    for obj in objects_to_delete:
                        self.conn.remove_object(bucket, obj.object_name)
                    self.conn.remove_bucket(bucket)
        except Exception:
            logging.exception(f"Fail to remove bucket {bucket}")

    def _resolve_bucket_and_path(self, bucket, fnm):
        """copy/move 用: 把逻辑 bucket 解析成 物理桶/对象路径(不含装饰器, 手动等价)。"""
        if self.bucket:
            if self.prefix_path:
                fnm = f"{self.prefix_path}/{bucket}/{fnm}"
            else:
                fnm = f"{bucket}/{fnm}"
            bucket = self.bucket
        elif self.prefix_path:
            fnm = f"{self.prefix_path}/{fnm}"
        return bucket, fnm

    def copy(self, src_bucket, src_path, dest_bucket, dest_path):
        try:
            src_bucket, src_path = self._resolve_bucket_and_path(src_bucket, src_path)
            dest_bucket, dest_path = self._resolve_bucket_and_path(dest_bucket, dest_path)

            if not self.conn.bucket_exists(dest_bucket):
                self.conn.make_bucket(dest_bucket)

            try:
                self.conn.stat_object(src_bucket, src_path)
            except Exception as e:
                logging.exception(f"Source object not found: {src_bucket}/{src_path}, {e}")
                return False

            self.conn.copy_object(
                dest_bucket,
                dest_path,
                CopySource(src_bucket, src_path),
            )
            return True

        except Exception:
            logging.exception(f"Fail to copy {src_bucket}/{src_path} -> {dest_bucket}/{dest_path}")
            return False

    def move(self, src_bucket, src_path, dest_bucket, dest_path):
        try:
            if self.copy(src_bucket, src_path, dest_bucket, dest_path):
                self.rm(src_bucket, src_path)
                return True
            else:
                logging.error(f"Copy failed, move aborted: {src_bucket}/{src_path}")
                return False
        except Exception:
            logging.exception(f"Fail to move {src_bucket}/{src_path} -> {dest_bucket}/{dest_path}")
            return False