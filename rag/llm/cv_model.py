"""rag/llm/cv_model.py — 图像/视频理解模型(Qwen 精简版), 对齐官方 v0.24.0。

用户手写版(238 行 vs 官方 1157): Base + GptV4 + QWenCV。
补全了缺失的 import 头; vision_llm_describe_prompt 依赖 rag.prompts(未移植),
延迟到方法内 import。
"""

import os
import base64
import tempfile
from abc import ABC
from io import BytesIO
from pathlib import Path

from openai import OpenAI, AsyncOpenAI

from common.token_utils import num_tokens_from_string, total_token_count_from_response
from rag.nlp import is_english


class Base(ABC):
    def __init__(self, **kwargs):
        # Configure retry parameters
        self.max_retries = kwargs.get("max_retries", int(os.environ.get("LLM_MAX_RETRIES", 5)))
        self.base_delay = kwargs.get("retry_interval", float(os.environ.get("LLM_BASE_DELAY", 2.0)))
        self.max_rounds = kwargs.get("max_rounds", 5)
        self.is_tools = False
        self.tools = []
        self.toolcall_sessions = {}
        self.extra_body = None

    def describe(self, image):
        raise NotImplementedError("Please implement encode method!")

    def describe_with_prompt(self, image, prompt=None):
        raise NotImplementedError("Please implement encode method!")

    def _form_history(self, system, history, images=None):
        hist = []
        if system:
            hist.append({"role": "system", "content": system})
        for h in history:
            if images and h["role"] == "user":
                h["content"] = self._image_prompt(h["content"], images)
                images = []
            hist.append(h)
        return hist

    def _image_prompt(self, text, images):
        if not images:
            return text

        if isinstance(images, str) or "bytes" in type(images).__name__:
            images = [images]

        pmpt = [{"type": "text", "text": text}]
        for img in images:
            pmpt.append({"type": "image_url", "image_url": {"url": img if isinstance(img, str) and img.startswith("data:") else f"data:image/png;base64,{img}"}})
        return pmpt

    async def async_chat(self, system, history, gen_conf, images=None, **kwargs):
        try:
            response = await self.async_client.chat.completions.create(
                model=self.model_name,
                messages=self._form_history(system, history, images),
                extra_body=self.extra_body,
            )
            return response.choices[0].message.content.strip(), response.usage.total_tokens
        except Exception as e:
            return "**ERROR**: " + str(e), 0

    async def async_chat_streamly(self, system, history, gen_conf, images=None, **kwargs):
        ans = ""
        tk_count = 0
        try:
            response = await self.async_client.chat.completions.create(
                model=self.model_name,
                messages=self._form_history(system, history, images),
                stream=True,
                extra_body=self.extra_body,
            )
            async for resp in response:
                if not resp.choices[0].delta.content:
                    continue
                delta = resp.choices[0].delta.content
                ans = delta
                if resp.choices[0].finish_reason == "length":
                    ans += "...\nFor the content length reason, it stopped, continue?" if is_english([ans]) else "······\n由于长度的原因，回答被截断了，要继续吗？"
                if resp.choices[0].finish_reason == "stop":
                    tk_count += resp.usage.total_tokens
                yield ans
        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield tk_count

    @staticmethod
    def image2base64_rawvalue(self, image):
        # Return a base64 string without data URL header
        if isinstance(image, bytes):
            b64 = base64.b64encode(image).decode("utf-8")
            return b64
        if isinstance(image, BytesIO):
            data = image.getvalue()
            b64 = base64.b64encode(data).decode("utf-8")
            return b64
        with BytesIO() as buffered:
            try:
                image.save(buffered, format="JPEG")
            except Exception:
                # reset buffer before saving PNG
                buffered.seek(0)
                buffered.truncate()
                image.save(buffered, format="PNG")
            data = buffered.getvalue()
            b64 = base64.b64encode(data).decode("utf-8")
        return b64

    @staticmethod
    def image2base64(image):
        # Return a data URL with the correct MIME to avoid provider mismatches
        if isinstance(image, bytes):
            # Best-effort magic number sniffing
            mime = "image/png"
            if len(image) >= 2 and image[0] == 0xFF and image[1] == 0xD8:
                mime = "image/jpeg"
            b64 = base64.b64encode(image).decode("utf-8")
            return f"data:{mime};base64,{b64}"
        if isinstance(image, BytesIO):
            data = image.getvalue()
            mime = "image/png"
            if len(data) >= 2 and data[0] == 0xFF and data[1] == 0xD8:
                mime = "image/jpeg"
            b64 = base64.b64encode(data).decode("utf-8")
            return f"data:{mime};base64,{b64}"
        with BytesIO() as buffered:
            fmt = "jpeg"
            try:
                image.save(buffered, format="JPEG")
            except Exception:
                # reset buffer before saving PNG
                buffered.seek(0)
                buffered.truncate()
                image.save(buffered, format="PNG")
                fmt = "png"
            data = buffered.getvalue()
            b64 = base64.b64encode(data).decode("utf-8")
            mime = f"image/{fmt}"
        return f"data:{mime};base64,{b64}"

    def prompt(self, b64):
        return [
            {
                "role": "user",
                "content": self._image_prompt(
                    "请用中文详细描述一下图中的内容，比如时间，地点，人物，事情，人物心情等，如果有数据请提取出数据。"
                    if self.lang.lower() == "chinese"
                    else "Please describe the content of this picture, like where, when, who, what happen. If it has number data, please extract them out.",
                    b64,
                ),
            }
        ]

    def vision_llm_prompt(self, b64, prompt=None):
        # ⚠️ 延迟 import: vision_llm_describe_prompt 在 rag/prompts/generator(学习版未移植), 移植后移到顶部
        from rag.prompts.generator import vision_llm_describe_prompt
        return [{"role": "user", "content": self._image_prompt(prompt if prompt else vision_llm_describe_prompt(), b64)}]


class GptV4(Base):
    _FACTORY_NAME = "OpenAI"

    def __init__(self, key, model_name="gpt-4-vision-preview", lang="Chinese", base_url="https://api.openai.com/v1", **kwargs):
        if not base_url:
            base_url = "https://api.openai.com/v1"
        self.api_key = key
        self.client = OpenAI(api_key=key, base_url=base_url)
        self.async_client = AsyncOpenAI(api_key=key, base_url=base_url)
        self.model_name = model_name
        self.lang = lang
        super().__init__(**kwargs)

    def describe(self, image):
        b64 = self.image2base64(image)
        res = self.client.chat.completions.create(model=self.model_name, messages=self.prompt(b64), extra_body=self.extra_body)
        return res.choices[0].message.content.strip(), total_token_count_from_response(res)

    def describe_with_prompt(self, image, prompt=None):
        b64 = self.image2base64(image)
        res = self.client.chat.completions.create(
            model=self.model_name,
            messages=self.vision_llm_prompt(b64, prompt),
            extra_body=self.extra_body,
        )
        return res.choices[0].message.content.strip(), total_token_count_from_response(res)


class QWenCV(GptV4):
    _FACTORY_NAME = "Tongyi-Qianwen"

    def __init__(self, key, model_name="qwen-vl-chat-v1", lang="Chinese", base_url=None, **kwargs):
        if not base_url:
            base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        super().__init__(key, model_name, lang=lang, base_url=base_url, **kwargs)

    async def async_chat(self, system, history, gen_conf, images=None, video_bytes=None, filename="", **kwargs):
        if video_bytes:
            try:
                summary, summary_num_tokens = self._process_video(video_bytes, filename)
                return summary, summary_num_tokens
            except Exception as e:
                return "**ERROR**: " + str(e), 0

        return "**ERROR**: Method chat not supported yet.", 0

    def _process_video(self, video_bytes, filename):
        from dashscope import MultiModalConversation

        video_suffix = Path(filename).suffix or ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=video_suffix) as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name

            video_path = f"file://{tmp_path}"
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "video": video_path,
                            "fps": 2,
                        },
                        {
                            "text": "Please summarize this video in proper sentences.",
                        },
                    ],
                }
            ]

            def call_api():
                response = MultiModalConversation.call(
                    api_key=self.api_key,
                    model=self.model_name,
                    messages=messages,
                )
                if response.get("message"):
                    raise Exception(response["message"])
                summary = response["output"]["choices"][0]["message"].content[0]["text"]
                return summary, num_tokens_from_string(summary)

            try:
                return call_api()
            except Exception as e1:
                import dashscope

                dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"
                try:
                    return call_api()
                except Exception as e2:
                    raise RuntimeError(f"Both default and intl endpoint failed.\nFirst error: {e1}\nSecond error: {e2}")
