from abc import ABC

import numpy as np

from common.log_utils import log_exception
from common.token_utils import total_token_count_from_response


class Base(ABC):
    def __init__(self, key, model_name, **kwargs):
        """
        Abstract base class constructor.
        Parameters are not stored; initialization is left to subclasses.
        """

    def similarity(self, query: str, texts: list):
        raise NotImplementedError("Please implement encode method!")

    @staticmethod
    def _normalize_rank(rank: np.ndarray) -> np.ndarray:
        min_rank = np.min(rank)
        max_rank = np.max(rank)

        if not np.isclose(min_rank, max_rank, atol=1e-3):
            rank = (rank - min_rank) / (max_rank - min_rank)
        else:
            rank = np.zeros_like(rank)
        return rank


class QWenRerank(Base):
    _FACTORY_NAME = "Tongyi-Qianwen"

    def __init__(self, key, model_name="gte-rerank", base_url=None, **kwargs):
        import dashscope

        self.api_key = key
        self.model_name = dashscope.TextReRank.Models.gte_rerank if model_name is None else model_name

    def similarity(self, query: str, texts: list):
        from http import HTTPStatus

        import dashscope

        resp = dashscope.TextReRank.call(api_key=self.api_key, model=self.model_name, query=query, documents=texts, top_n=len(texts), return_documents=False)
        rank = np.zeros(len(texts), dtype=float)
        if resp.status_code == HTTPStatus.OK:
            try:
                for r in resp.output.results:
                    rank[r.index] = r.relevance_score
            except Exception as _e:
                log_exception(_e, resp)
            return rank, total_token_count_from_response(resp)
        else:
            raise ValueError(f"Error calling QWenRerank model {self.model_name}: {resp.status_code} - {resp.text}")
