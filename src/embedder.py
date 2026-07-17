"""
文本向量化器
使用 Sentence Transformers 加载 BGE 模型进行文本编码
"""
import logging
import threading
from typing import List, Optional

logger = logging.getLogger(__name__)


class Embedder:
    """文本向量化器

    负责：
    - 加载嵌入模型（延迟加载）
    - 批量编码文本
    - 编码结果缓存

    默认模型: BAAI/bge-small-zh-v1.5
    - 向量维度: 512
    - 最大输入: 512 tokens
    - 模型大小: ~400MB
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        device: Optional[str] = None
    ):
        """初始化向量化器

        Args:
            model_name: 模型名称（HuggingFace 格式）
            device: 运行设备（cpu/cuda，默认自动检测）
        """
        self.model_name = model_name
        self.device = device
        self._model = None  # 延迟加载
        self._dimension = 512  # bge-small-zh-v1.5 的维度
        self._lock = threading.RLock()

    @property
    def dimension(self) -> int:
        """获取向量维度"""
        return self._dimension

    def _load_model(self):
        """延迟加载模型（首次调用时加载）"""
        if self._model is not None:
            return

        with self._lock:
            if self._model is not None:
                return
            try:
                from sentence_transformers import SentenceTransformer

                logger.info(f"正在加载嵌入模型: {self.model_name}")
                self._model = SentenceTransformer(
                    self.model_name,
                    device=self.device
                )
                self._dimension = self._model.get_sentence_embedding_dimension()
                logger.info(f"模型加载完成，向量维度: {self._dimension}")
            except ImportError as e:
                raise ImportError(
                    "sentence_transformers 未安装。请运行: pip install sentence-transformers"
                ) from e
            except Exception as e:
                logger.error("模型加载失败: error_type=%s", type(e).__name__)
                raise

    def encode(
        self,
        texts: List[str],
        normalize: bool = True,
        batch_size: int = 32
    ) -> List[List[float]]:
        """批量编码文本为向量

        Args:
            texts: 文本列表
            normalize: 是否归一化向量（默认 True，适用于余弦相似度）
            batch_size: 批处理大小

        Returns:
            List[List[float]]: 向量列表
        """
        if not texts:
            return []

        self._load_model()

        try:
            with self._lock:
                embeddings = self._model.encode(
                    texts,
                    normalize_embeddings=normalize,
                    batch_size=batch_size,
                    show_progress_bar=False,
                    convert_to_numpy=True
                )

            # 转换为列表格式
            return embeddings.tolist()

        except Exception as e:
            logger.error("文本编码失败: error_type=%s", type(e).__name__)
            raise

    def encode_single(self, text: str, normalize: bool = True) -> List[float]:
        """编码单个文本

        Args:
            text: 待编码文本
            normalize: 是否归一化

        Returns:
            List[float]: 向量
        """
        if not text:
            return [0.0] * self._dimension

        result = self.encode([text], normalize=normalize)
        return result[0] if result else []

    def is_ready(self) -> bool:
        """检查模型是否已加载"""
        return self._model is not None

    def unload(self):
        """卸载模型，释放内存"""
        with self._lock:
            if self._model is not None:
                del self._model
                self._model = None
                logger.info("模型已卸载")


# 全局单例（可选，用于跨请求共享模型）
_global_embedder: Optional[Embedder] = None


def get_global_embedder(model_name: str = "BAAI/bge-small-zh-v1.5") -> Embedder:
    """获取全局单例 Embedder

    Args:
        model_name: 模型名称

    Returns:
        Embedder: 全局向量化器实例
    """
    global _global_embedder

    if _global_embedder is None:
        _global_embedder = Embedder(model_name)

    return _global_embedder
