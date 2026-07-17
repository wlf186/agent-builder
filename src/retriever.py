"""
向量检索器
使用 ChromaDB 进行语义检索
"""
import logging
from typing import List, Optional, Any

from src.models import RetrievalResult
from src.embedder import Embedder

logger = logging.getLogger(__name__)


class Retriever:
    """向量检索器

    负责：
    - 查询向量化
    - ChromaDB 相似度检索
    - 结果过滤和格式化
    """

    def __init__(
        self,
        collection: Any,  # chromadb.Collection
        embedder: Embedder
    ):
        """初始化检索器

        Args:
            collection: ChromaDB 集合
            embedder: 向量化器实例
        """
        self.collection = collection
        self.embedder = embedder

    def search(
        self,
        query: str,
        top_k: int = 3,
        score_threshold: float = 0.6
    ) -> List[RetrievalResult]:
        """检索相关文档片段

        Args:
            query: 查询文本
            top_k: 返回结果数量
            score_threshold: 相似度阈值 (0-1)

        Returns:
            List[RetrievalResult]: 检索结果列表（按相似度降序）
        """
        if not query:
            return []

        try:
            # 1. 查询向量化
            query_embedding = self.embedder.encode_single(query)

            # 2. 向量检索（多取一些用于过滤）
            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k * 2, 50),  # 最多取50个
                include=["documents", "metadatas", "distances"]
            )

            # 3. 格式化并过滤结果
            formatted_results = self._format_results(
                results,
                score_threshold,
                top_k
            )

            logger.debug(
                "检索完成: query_chars=%d results=%d requested=%d",
                len(query),
                len(formatted_results),
                top_k,
            )
            return formatted_results

        except Exception as exc:
            # Empty results and a failed vector store are materially different:
            # callers must be able to report an outage instead of hallucinating
            # that the knowledge base contained no matching information.
            logger.error("向量检索失败: error_type=%s", type(exc).__name__)
            raise

    def _format_results(
        self,
        raw_results: dict,
        score_threshold: float,
        top_k: int
    ) -> List[RetrievalResult]:
        """格式化 ChromaDB 查询结果

        Args:
            raw_results: ChromaDB 原始返回
            score_threshold: 相似度阈值
            top_k: 最大返回数量

        Returns:
            List[RetrievalResult]: 格式化结果
        """
        if not raw_results or not raw_results.get("ids"):
            return []

        formatted = []

        # ChromaDB 返回格式: {ids: [[id1, id2]], distances: [[d1, d2]], ...}
        ids = raw_results.get("ids", [[]])[0]
        distances = raw_results.get("distances", [[]])[0]
        documents = raw_results.get("documents", [[]])[0]
        metadatas = raw_results.get("metadatas", [[]])[0]

        for i, (doc_id, distance, content, metadata) in enumerate(
            zip(ids, distances, documents, metadatas)
        ):
            # collection 使用 hnsw:space=cosine，Chroma 返回 cosine distance。
            # 对应相似度为 1 - distance，并限制到 API 声明的 0..1 范围。
            score = max(0.0, min(1.0, 1.0 - float(distance)))

            if score < score_threshold:
                continue

            result = RetrievalResult(
                content=content,
                doc_id=metadata.get("doc_id", doc_id),
                filename=metadata.get("filename", "unknown"),
                score=score,
                chunk_index=metadata.get("chunk_index", 0)
            )
            formatted.append(result)

            if len(formatted) >= top_k:
                break

        # 按相似度降序排序
        formatted.sort(key=lambda x: x.score, reverse=True)

        return formatted

    def search_with_embeddings(
        self,
        query_embedding: List[float],
        top_k: int = 3,
        score_threshold: float = 0.6
    ) -> List[RetrievalResult]:
        """使用预编码的查询向量检索

        当查询已预先编码时使用，避免重复编码。

        Args:
            query_embedding: 查询向量
            top_k: 返回结果数量
            score_threshold: 相似度阈值

        Returns:
            List[RetrievalResult]: 检索结果列表
        """
        if not query_embedding:
            return []

        try:
            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k * 2, 50),
                include=["documents", "metadatas", "distances"]
            )

            return self._format_results(results, score_threshold, top_k)

        except Exception as exc:
            logger.error("预编码向量检索失败: error_type=%s", type(exc).__name__)
            raise

    def get_collection_size(self) -> int:
        """获取集合中的文档块数量

        Returns:
            int: 文档块数量
        """
        try:
            return self.collection.count()
        except Exception as exc:
            logger.error("获取集合大小失败: error_type=%s", type(exc).__name__)
            raise


class HybridRetriever(Retriever):
    """混合检索器（预留扩展）

    结合向量检索和关键词检索（BM25）提高准确率
    """

    def __init__(
        self,
        collection: Any,
        embedder: Embedder,
        bm25_weight: float = 0.3
    ):
        """初始化混合检索器

        Args:
            collection: ChromaDB 集合
            embedder: 向量化器
            bm25_weight: BM25 权重（0-1），默认 0.3
        """
        super().__init__(collection, embedder)
        self.bm25_weight = bm25_weight
        self._bm25_index = None  # 预留：BM25 索引

    def search(
        self,
        query: str,
        top_k: int = 3,
        score_threshold: float = 0.6
    ) -> List[RetrievalResult]:
        """混合检索（向量 + BM25）

        Args:
            query: 查询文本
            top_k: 返回结果数量
            score_threshold: 相似度阈值

        Returns:
            List[RetrievalResult]: 检索结果列表
        """
        # MVP 阶段：仅使用向量检索
        # TODO: 实现 BM25 检索和结果融合
        return super().search(query, top_k, score_threshold)
