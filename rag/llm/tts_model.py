"""rag/llm/tts_model.py — 语音合成模型(Qwen 精简版), 对齐官方 v0.24.0。

用户手写版(94 行 vs 官方 451): 请求模型 + Base + QwenTTS(dashscope SpeechSynthesizer)。
补全了缺失的 import 头。
"""

import re
import time
from abc import ABC
from typing import Annotated, Literal
from pydantic import BaseModel, conint

from common.token_utils import num_tokens_from_string


class ServeReferenceAudio(BaseModel):
    audio: bytes
    text: str


class ServeTTSRequest(BaseModel):
    text: str
    chunk_length: Annotated[int, conint(ge=100, le=300, strict=True)] = 200
    # Audio format
    format: Literal["wav", "pcm", "mp3"] = "mp3"
    mp3_bitrate: Literal[64, 128, 192] = 128
    # References audios for in-context learning
    references: list[ServeReferenceAudio] = []
    # Reference id
    # For example, if you want use https://fish.audio/m/7f92f8afb8ec43bf81429cc1c9199cb1/
    # Just pass 7f92f8afb8ec43bf81429cc1c9199cb1
    reference_id: str | None = None
    # Normalize text for en & zh, this increase stability for numbers
    normalize: bool = True
    # Balance mode will reduce latency to 300ms, but may decrease stability
    latency: Literal["normal", "balanced"] = "normal"


class Base(ABC):
    def __init__(self, key, model_name, base_url, **kwargs):
        """
        Abstract base class constructor.
        Parameters are not stored; subclasses should handle their own initialization.
        """
        pass

    def tts(self, audio):
        pass

    def normalize_text(self, text):
        return re.sub(r"(\*\*|##\d+\$\$|#)", "", text)


class QwenTTS(Base):
    _FACTORY_NAME = "Tongyi-Qianwen"

    def __init__(self, key, model_name, base_url=""):
        import dashscope

        self.model_name = model_name
        dashscope.api_key = key

    def tts(self, text):
        from collections import deque

        from dashscope.api_entities.dashscope_response import SpeechSynthesisResponse
        from dashscope.audio.tts import ResultCallback, SpeechSynthesisResult, SpeechSynthesizer

        class Callback(ResultCallback):
            def __init__(self) -> None:
                self.dque = deque()

            def _run(self):
                while True:
                    if not self.dque:
                        time.sleep(0)
                        continue
                    val = self.dque.popleft()
                    if val:
                        yield val
                    else:
                        break

            def on_open(self):
                pass

            def on_complete(self):
                self.dque.append(None)

            def on_error(self, response: SpeechSynthesisResponse):
                raise RuntimeError(str(response))

            def on_close(self):
                pass

            def on_event(self, result: SpeechSynthesisResult):
                if result.get_audio_frame() is not None:
                    self.dque.append(result.get_audio_frame())

        text = self.normalize_text(text)
        callback = Callback()
        SpeechSynthesizer.call(model=self.model_name, text=text, callback=callback, format="mp3")
        try:
            for data in callback._run():
                yield data
            yield num_tokens_from_string(text)

        except Exception as e:
            raise RuntimeError(f"**ERROR**: {e}")
