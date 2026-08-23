"""llm_service.py — LLM 表服务与统一模型调用入口, 对齐官方 v0.24.0(486 行)。

学习要点:
- LLMService: llm 表 CRUD, CommonService 一行搞定
- get_init_tenant_llm: 注册租户时初始化 tenant_llm —— 从 settings 的 5 类模型
  配置(CHAT/EMBEDDING/ASR/IMAGE2TEXT/RERANK)取厂商, 再从 llm 表捞出该厂商
  全部型号批量入库; api_key/base_url 先用对应类型的专属配置, 缺了才回退厂商
  配置; 最后按 (tenant_id, llm_factory, llm_name) 保序去重
- LLMBundle(继承 LLM4Tenant): 全仓库调模型的统一入口, 各业务方法都是同一套
  模板: langfuse 埋点 → 调 self.mdl → TenantLLMService.increase_usage 记账 →
  generation.end() 收尾。几个特殊点:
  - encode: 先估 token, 超 max_length 的文本截到 95%
  - stream_transcription: 双路径 —— 模型支持流式就透传事件流(靠 final 事件
    取全文记账), 否则降级为一次性转录再包成单个 final 事件
  - tts: 流里冒出 int 即 token 总用量(chat_model 的私有约定), 记账后返回
  - _run_coroutine_sync / _sync_from_async_stream / _bridge_sync_stream:
    同步世界 ↔ async 世界的三个桥, 分别解决"跑协程"/"消费异步流"/"喂同步流"
  - _clean_param: inspect 反射真实函数签名, 过滤掉当前模型不支持的 kwargs
- async_chat_streamly 每次 yield 累计全文, async_chat_streamly_delta 只 yield
  本次增量 —— 两方法除这一行外逐字相同
- 有意差异: 官方 logging 混用 .format 与 f-string, 此处统一为 f-string

⚠️ 官方疑似笔误, 保持原样: describe(image, max_tokens=300) 的 max_tokens
从未传给 mdl.describe。
"""

import asyncio
from functools import partial
import inspect
import logging
import queue
import re
import threading
from typing import Generator

from api.db.db_models import LLM
from api.db.services.common_service import CommonService
from api.db.services.tenant_llm_service import LLM4Tenant, TenantLLMService
from common import settings
from common.constants import LLMType
from common.token_utils import num_tokens_from_string


class LLMService(CommonService):
    model = LLM


def get_init_tenant_llm(user_id):
    tenant_llm = []

    model_configs = {
        LLMType.CHAT: settings.CHAT_CFG,
        LLMType.EMBEDDING: settings.EMBEDDING_CFG,
        LLMType.SPEECH2TEXT: settings.ASR_CFG,
        LLMType.IMAGE2TEXT: settings.IMAGE2TEXT_CFG,
        LLMType.RERANK: settings.RERANK_CFG,
    }

    seen = set()  # 同一厂商可能同时配在多类模型里(如 CHAT/RERANK 都用 OpenAI), 只查一次库
    factory_configs = []
    for factory_config in [
        settings.CHAT_CFG,
        settings.EMBEDDING_CFG,
        settings.ASR_CFG,
        settings.IMAGE2TEXT_CFG,
        settings.RERANK_CFG,
    ]:
        factory_name = factory_config["factory"]
        if factory_name not in seen:
            seen.add(factory_name)
            factory_configs.append(factory_config)

    for factory_config in factory_configs:
        for llm in LLMService.query(fid=factory_config["factory"]):
            tenant_llm.append(
                {
                    "tenant_id": user_id,
                    "llm_factory": factory_config["factory"],
                    "llm_name": llm.llm_name,
                    "model_type": llm.model_type,
                    "api_key": model_configs.get(llm.model_type, {}).get("api_key", factory_config["api_key"]),
                    "api_base": model_configs.get(llm.model_type, {}).get("base_url", factory_config["base_url"]),
                    "max_tokens": llm.max_tokens if llm.max_tokens else 8192,
                }
            )

    unique = {}  # 不同模型类型可能解析出同一组 (tenant, factory, llm_name), 保序去重
    for item in tenant_llm:
        key = (item["tenant_id"], item["llm_factory"], item["llm_name"])
        if key not in unique:
            unique[key] = item
    return list(unique.values())


class LLMBundle(LLM4Tenant):
    def __init__(self, tenant_id, llm_type, llm_name=None, lang="Chinese", **kwargs):
        super().__init__(tenant_id, llm_type, llm_name, lang, **kwargs)

    def bind_tools(self, toolcall_session, tools):
        if not self.is_tools:
            logging.warning(f"Model {self.llm_name} does not support tool call, but you have assigned one or more tools to it!")
            return
        self.mdl.bind_tools(toolcall_session, tools)

    def encode(self, texts: list):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="encode", model=self.llm_name, input={"texts": texts})
        safe_texts = []  # 超 max_length 的文本截到 95%, 给分词器的计数误差留余量

        for text in texts:
            token_size = num_tokens_from_string(text)
            if token_size > self.max_length:
                target_len = int(self.max_length * 0.95)
                safe_texts.append(text[:target_len])
            else:
                safe_texts.append(text)
        embeddings, used_tokens = self.mdl.encode(safe_texts)

        llm_name = getattr(self, "llm_name", None)
        if not TenantLLMService.increase_usage(self.tenant_id, self.llm_type, used_tokens, llm_name):
            logging.error(f"LLMBundle.encode can't update token usage for <tenant redacted>/EMBEDDING used_tokens: {used_tokens}")
        if self.langfuse:
            generation.update(usage_details={"total_tokens": used_tokens})
            generation.end()

        return embeddings, used_tokens

    def encode_queries(self, query: str):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="encode_queries", model=self.llm_name, input={"query": query})

        emd, used_tokens = self.mdl.encode_queries(query)
        llm_name = getattr(self, "llm_name", None)
        if not TenantLLMService.increase_usage(self.tenant_id, self.llm_type, used_tokens, llm_name):
            logging.error(f"LLMBundle.encode_queries can't update token usage for <tenant redacted>/EMBEDDING used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(usage_details={"total_tokens": used_tokens})
            generation.end()

        return emd, used_tokens

    def similarity(self, query: str, texts: list):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="similarity", model=self.llm_name, input={"query": query, "texts": texts})

        sim, used_tokens = self.mdl.similarity(query, texts)
        if not TenantLLMService.increase_usage(self.tenant_id, self.llm_type, used_tokens):
            logging.error(f"LLMBundle.similarity can't update token usage for {self.tenant_id}/RERANK used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(usage_details={"total_tokens": used_tokens})
            generation.end()

        return sim, used_tokens

    def describe(self, image, max_tokens=300):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="describe", metadata={"model": self.llm_name})

        txt, used_tokens = self.mdl.describe(image)  # ⚠️ 官方未把 max_tokens 传下去, 保持原样
        if not TenantLLMService.increase_usage(self.tenant_id, self.llm_type, used_tokens):
            logging.error(f"LLMBundle.describe can't update token usage for {self.tenant_id}/IMAGE2TEXT used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})
            generation.end()

        return txt

    def describe_with_prompt(self, image, prompt):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="describe_with_prompt", metadata={"model": self.llm_name, "prompt": prompt})

        txt, used_tokens = self.mdl.describe_with_prompt(image, prompt)
        if not TenantLLMService.increase_usage(self.tenant_id, self.llm_type, used_tokens):
            logging.error(f"LLMBundle.describe can't update token usage for {self.tenant_id}/IMAGE2TEXT used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})
            generation.end()

        return txt

    def transcription(self, audio):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="transcription", metadata={"model": self.llm_name})

        txt, used_tokens = self.mdl.transcription(audio)
        if not TenantLLMService.increase_usage(self.tenant_id, self.llm_type, used_tokens):
            logging.error(f"LLMBundle.transcription can't update token usage for {self.tenant_id}/SEQUENCE2TXT used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})
            generation.end()

        return txt

    def stream_transcription(self, audio):
        # 双路径: 模型原生支持流式就透传事件流; 不支持则降级为一次性转录, 包成单个 final 事件
        mdl = self.mdl
        supports_stream = hasattr(mdl, "stream_transcription") and callable(mdl.stream_transcription)
        if supports_stream:
            if self.langfuse:
                generation = self.langfuse.start_generation(
                    trace_context=self.trace_context,
                    name="stream_transcription",
                    metadata={"model": self.llm_name},
                )
            final_text = ""
            used_tokens = 0

            try:
                for evt in mdl.stream_transcription(audio):
                    if evt.get("event") == "final":
                        final_text = evt.get("text", "")

                    yield evt

            except Exception as e:
                err = {"event": "error", "text": str(e)}
                yield err
                final_text = final_text or ""
            finally:
                if final_text:
                    used_tokens = num_tokens_from_string(final_text)
                    TenantLLMService.increase_usage(self.tenant_id, self.llm_type, used_tokens)

                if self.langfuse:
                    generation.update(
                        output={"output": final_text},
                        usage_details={"total_tokens": used_tokens},
                    )
                    generation.end()

            return

        if self.langfuse:
            generation = self.langfuse.start_generation(
                trace_context=self.trace_context,
                name="stream_transcription",
                metadata={"model": self.llm_name},
            )

        full_text, used_tokens = mdl.transcription(audio)
        if not TenantLLMService.increase_usage(self.tenant_id, self.llm_type, used_tokens):
            logging.error(f"LLMBundle.stream_transcription can't update token usage for {self.tenant_id}/SEQUENCE2TXT used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(
                output={"output": full_text},
                usage_details={"total_tokens": used_tokens},
            )
            generation.end()

        yield {
            "event": "final",
            "text": full_text,
            "streaming": False,
        }

    def tts(self, text: str) -> Generator[bytes, None, None]:
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="tts", input={"text": text})

        for chunk in self.mdl.tts(text):  # 约定: 流中出现 int 即本次合成总 token 数(见 chat_model.tts)
            if isinstance(chunk, int):
                if not TenantLLMService.increase_usage(self.tenant_id, self.llm_type, chunk, self.llm_name):
                    logging.error(f"LLMBundle.tts can't update token usage for {self.tenant_id}/TTS")
                return
            yield chunk

        if self.langfuse:
            generation.end()

    def _remove_reasoning_content(self, txt: str) -> str:
        # 剥推理模型的思维链: 取首个 <think> 到最后一个 </think>, 只留其后正文;
        # 标签不成对或顺序颠倒时原文返回
        first_think_start = txt.find("<think>")
        if first_think_start == -1:
            return txt

        last_think_end = txt.rfind("</think>")
        if last_think_end == -1:
            return txt

        if last_think_end < first_think_start:
            return txt

        return txt[last_think_end + len("</think>") :]

    @staticmethod
    def _clean_param(chat_partial, **kwargs):
        func = chat_partial.func  # partial 对象没有可用签名, 反射它包装的原函数
        sig = inspect.signature(func)
        support_var_args = False
        allowed_params = set()

        for param in sig.parameters.values():
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                support_var_args = True
            elif param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
                allowed_params.add(param.name)
        if support_var_args:
            return kwargs
        else:
            return {k: v for k, v in kwargs.items() if k in allowed_params}

    def _run_coroutine_sync(self, coro):
        # 没有运行中的事件循环 → 直接 asyncio.run;
        # 已身处事件循环内 → 开子线程另起 loop 跑, 规避"嵌套 asyncio.run"报错
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        result_queue: queue.Queue = queue.Queue()

        def runner():
            try:
                result_queue.put((True, asyncio.run(coro)))
            except Exception as e:
                result_queue.put((False, e))

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join()

        success, value = result_queue.get_nowait()
        if success:
            return value
        raise value

    def _sync_from_async_stream(self, async_gen_fn, *args, **kwargs):
        # 子线程开独立事件循环消费异步生成器, 经普通 Queue 转成同步生成器
        result_queue: queue.Queue = queue.Queue()

        def runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def consume():
                try:
                    async for item in async_gen_fn(*args, **kwargs):
                        result_queue.put(item)
                except Exception as e:
                    result_queue.put(e)
                finally:
                    result_queue.put(StopIteration)

            loop.run_until_complete(consume())
            loop.close()

        threading.Thread(target=runner, daemon=True).start()

        while True:
            item = result_queue.get()
            if item is StopIteration:
                break
            if isinstance(item, Exception):
                raise item
            yield item

    def _bridge_sync_stream(self, gen):
        # 反向桥接: 后台线程消费同步生成器, 经 call_soon_threadsafe 灌进 asyncio.Queue,
        # 以 StopAsyncIteration 作结束哨兵, 供 async 侧 async for 消费
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def worker():
            try:
                for item in gen:
                    loop.call_soon_threadsafe(queue.put_nowait, item)
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, StopAsyncIteration)

        threading.Thread(target=worker, daemon=True).start()
        return queue

    async def async_chat(self, system: str, history: list, gen_conf: dict = {}, **kwargs):
        # 选路: 模型支持工具调用则优先走 *_with_tools 版本
        if self.is_tools and getattr(self.mdl, "is_tools", False) and hasattr(self.mdl, "async_chat_with_tools"):
            base_fn = self.mdl.async_chat_with_tools
        elif hasattr(self.mdl, "async_chat"):
            base_fn = self.mdl.async_chat
        else:
            raise RuntimeError(f"Model {self.mdl} does not implement async_chat or async_chat_with_tools")

        generation = None
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="chat", model=self.llm_name, input={"system": system, "history": history})

        chat_partial = partial(base_fn, system, history, gen_conf)
        use_kwargs = self._clean_param(chat_partial, **kwargs)

        try:
            txt, used_tokens = await chat_partial(**use_kwargs)
        except Exception as e:
            if generation:
                generation.update(output={"error": str(e)})
                generation.end()
            raise

        txt = self._remove_reasoning_content(txt)
        if not self.verbose_tool_use:
            # 非 verbose 模式抹掉 <tool_call> 过程痕迹, 只留最终回答
            txt = re.sub(r"<tool_call>.*?</tool_call>", "", txt, flags=re.DOTALL)

        if used_tokens and not TenantLLMService.increase_usage(self.tenant_id, self.llm_type, used_tokens, self.llm_name):
            logging.error(f"LLMBundle.async_chat can't update token usage for {self.tenant_id}/CHAT llm_name: {self.llm_name}, used_tokens: {used_tokens}")

        if generation:
            generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})
            generation.end()

        return txt

    async def async_chat_streamly(self, system: str, history: list, gen_conf: dict = {}, **kwargs):
        total_tokens = 0
        ans = ""
        if self.is_tools and getattr(self.mdl, "is_tools", False) and hasattr(self.mdl, "async_chat_streamly_with_tools"):
            stream_fn = getattr(self.mdl, "async_chat_streamly_with_tools", None)
        elif hasattr(self.mdl, "async_chat_streamly"):
            stream_fn = getattr(self.mdl, "async_chat_streamly", None)
        else:
            raise RuntimeError(f"Model {self.mdl} does not implement async_chat or async_chat_with_tools")

        generation = None
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="chat_streamly", model=self.llm_name, input={"system": system, "history": history})

        if stream_fn:
            chat_partial = partial(stream_fn, system, history, gen_conf)
            use_kwargs = self._clean_param(chat_partial, **kwargs)
            try:
                async for txt in chat_partial(**use_kwargs):
                    if isinstance(txt, int):
                        total_tokens = txt
                        break

                    if txt.endswith("</think>"):
                        ans = ans[: -len("</think>")]

                    if not self.verbose_tool_use:
                        txt = re.sub(r"<tool_call>.*?</tool_call>", "", txt, flags=re.DOTALL)

                    ans += txt
                    # 注意: 这里 yield 的是累计全文(完整前缀), 不是增量
                    yield ans
            except Exception as e:
                if generation:
                    generation.update(output={"error": str(e)})
                    generation.end()
                raise
            if total_tokens and not TenantLLMService.increase_usage(self.tenant_id, self.llm_type, total_tokens, self.llm_name):
                logging.error(f"LLMBundle.async_chat_streamly can't update token usage for {self.tenant_id}/CHAT llm_name: {self.llm_name}, used_tokens: {total_tokens}")
            if generation:
                generation.update(output={"output": ans}, usage_details={"total_tokens": total_tokens})
                generation.end()
            return

    async def async_chat_streamly_delta(self, system: str, history: list, gen_conf: dict = {}, **kwargs):
        total_tokens = 0
        ans = ""
        if self.is_tools and getattr(self.mdl, "is_tools", False) and hasattr(self.mdl, "async_chat_streamly_with_tools"):
            stream_fn = getattr(self.mdl, "async_chat_streamly_with_tools", None)
        elif hasattr(self.mdl, "async_chat_streamly"):
            stream_fn = getattr(self.mdl, "async_chat_streamly", None)
        else:
            raise RuntimeError(f"Model {self.mdl} does not implement async_chat or async_chat_with_tools")

        generation = None
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="chat_streamly", model=self.llm_name, input={"system": system, "history": history})

        if stream_fn:
            chat_partial = partial(stream_fn, system, history, gen_conf)
            use_kwargs = self._clean_param(chat_partial, **kwargs)
            try:
                async for txt in chat_partial(**use_kwargs):
                    if isinstance(txt, int):
                        total_tokens = txt
                        break

                    if txt.endswith("</think>"):
                        ans = ans[: -len("</think>")]

                    if not self.verbose_tool_use:
                        txt = re.sub(r"<tool_call>.*?</tool_call>", "", txt, flags=re.DOTALL)

                    ans += txt
                    # 与 async_chat_streamly 唯一区别: 只 yield 本次增量, 由调用方自行拼接
                    yield txt
            except Exception as e:
                if generation:
                    generation.update(output={"error": str(e)})
                    generation.end()
                raise
            if total_tokens and not TenantLLMService.increase_usage(self.tenant_id, self.llm_type, total_tokens, self.llm_name):
                logging.error(f"LLMBundle.async_chat_streamly can't update token usage for {self.tenant_id}/CHAT llm_name: {self.llm_name}, used_tokens: {total_tokens}")
            if generation:
                generation.update(output={"output": ans}, usage_details={"total_tokens": total_tokens})
                generation.end()
            return
