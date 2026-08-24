"""
rag.utils.base64_image — 图片工具集。

test_image: llm_app 校验 CV 模型用的一张内置 base64 测试图;
image2id / id2image: chunk 内嵌图片与存储桶对象之间的存取转换(对话/图谱用到)。
来源: 官方 rag/utils/base64_image.py @ v0.24.0
"""
import base64
import logging
from functools import partial
from io import BytesIO

from PIL import Image

from common.misc_utils import thread_pool_exec

test_image_base64 = "iVBORw0KGgoAAAANSUhEUgAAAGQAAABkCAIAAAD/gAIDAAAA6ElEQVR4nO3QwQ3AIBDAsIP9d25XIC+EZE8QZc18w5l9O+AlZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBWYFZgVmBT+IYAHHLHkdEgAAAABJRU5ErkJggg=="
test_image = base64.b64decode(test_image_base64)


async def image2id(d: dict, storage_put_func: partial, objname: str, bucket: str = "imagetemps"):
    """把 chunk 里的 image 字段转成 JPEG 存到对象存储, 换成 img_id 引用。"""
    import logging
    from io import BytesIO
    from rag.svr.task_executor import minio_limiter  # ⚠️ rag/svr 任务执行器未移植, 用到时该分支报错

    if "image" not in d:
        return
    if not d["image"]:
        del d["image"]
        return

    def encode_image():
        with BytesIO() as buf:
            img = d["image"]

            if isinstance(img, bytes):
                buf.write(img)
                buf.seek(0)
                return buf.getvalue()

            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            try:
                img.save(buf, format="JPEG")
            except OSError as e:
                logging.warning(f"Saving image exception: {e}")
                return None

            buf.seek(0)
            return buf.getvalue()

    jpeg_binary = await thread_pool_exec(encode_image)
    if jpeg_binary is None:
        del d["image"]
        return

    async with minio_limiter:
        await thread_pool_exec(
            lambda: storage_put_func(bucket=bucket, fnm=objname, binary=jpeg_binary)
        )

    d["img_id"] = f"{bucket}-{objname}"

    if not isinstance(d["image"], bytes):
        d["image"].close()
    del d["image"]


def id2image(image_id: str | None, storage_get_func: partial):
    """按 img_id(bucket-objname) 从对象存储取回图片对象。"""
    if not image_id:
        return
    arr = image_id.split("-")
    if len(arr) != 2:
        return
    bkt, nm = image_id.split("-")
    try:
        blob = storage_get_func(bucket=bkt, fnm=nm)
        if not blob:
            return
        return Image.open(BytesIO(blob))
    except Exception as e:
        logging.exception(e)