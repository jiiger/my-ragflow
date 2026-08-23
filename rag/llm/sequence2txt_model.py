import base64
import io
from abc import ABC

from common.token_utils import num_tokens_from_string


class Base(ABC):
    def __init__(self, key, model_name, **kwargs):
        """
        Abstract base class constructor.
        Parameters are not stored; initialization is left to subclasses.
        """
        pass

    def transcription(self, audio_path, **kwargs):
        audio_file = open(audio_path, "rb")
        transcription = self.client.audio.transcriptions.create(model=self.model_name, file=audio_file)
        return transcription.text.strip(), num_tokens_from_string(transcription.text.strip())

    def audio2base64(self, audio):
        if isinstance(audio, bytes):
            return base64.b64encode(audio).decode("utf-8")
        if isinstance(audio, io.BytesIO):
            return base64.b64encode(audio.getvalue()).decode("utf-8")
        raise TypeError("The input audio file should be in binary format.")


class QWenSeq2txt(Base):
    _FACTORY_NAME = "Tongyi-Qianwen"

    def __init__(self, key, model_name="qwen-audio-asr", **kwargs):
        import dashscope

        dashscope.api_key = key
        self.model_name = model_name

    def transcription(self, audio_path):
        import dashscope

        if audio_path.startswith("http"):
            audio_input = audio_path
        else:
            audio_input = f"file://{audio_path}"

        messages = [{"role": "system", "content": [{"text": ""}]}, {"role": "user", "content": [{"audio": audio_input}]}]

        resp = dashscope.MultiModalConversation.call(model=self.model_name, messages=messages, result_format="message", asr_options={"enable_lid": True, "enable_itn": False})

        try:
            text = resp["output"]["choices"][0]["message"].content[0]["text"]
        except Exception as e:
            text = "**ERROR**: " + str(e)
        return text, num_tokens_from_string(text)

    def stream_transcription(self, audio_path):
        import dashscope

        if audio_path.startswith("http"):
            audio_input = audio_path
        else:
            audio_input = f"file://{audio_path}"

        messages = [{"role": "system", "content": [{"text": ""}]}, {"role": "user", "content": [{"audio": audio_input}]}]

        stream = dashscope.MultiModalConversation.call(model=self.model_name, messages=messages, result_format="message", stream=True, asr_options={"enable_lid": True, "enable_itn": False})

        full = ""
        for chunk in stream:
            try:
                piece = chunk["output"]["choices"][0]["message"].content[0]["text"]
                full = piece
                yield {"event": "delta", "text": piece}
            except Exception as e:
                yield {"event": "error", "text": str(e)}

        yield {"event": "final", "text": full}
