"""
模型供应商测试器 - 测试连通性和获取模型列表
"""
import httpx
import json
from typing import Dict, List, Tuple, Optional
from .models import ModelProvider
from .security import SecurityValidationError, validate_outbound_url


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_MODELS = 500


async def _get_json_limited(
    client: httpx.AsyncClient,
    url: str,
    headers: Optional[Dict[str, str]] = None,
) -> tuple[int, object]:
    async with client.stream("GET", url, headers=headers) as response:
        declared = response.headers.get("content-length")
        if declared:
            try:
                declared_size = int(declared)
            except ValueError as exc:
                raise ValueError("响应 Content-Length 无效") from exc
            if declared_size < 0 or declared_size > MAX_RESPONSE_BYTES:
                raise ValueError("响应过大或长度无效")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("响应过大")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        return response.status_code, data


def _bounded_models(raw: object) -> List[str]:
    if not isinstance(raw, list):
        return []
    models: List[str] = []
    for item in raw[:MAX_MODELS]:
        if not isinstance(item, dict):
            continue
        value = item.get("id", item.get("name", ""))
        if isinstance(value, str) and 0 < len(value) <= 200:
            models.append(value)
    return sorted(set(models))


class ModelProviderTester:
    """模型供应商测试器"""

    @staticmethod
    async def test_connection(
        provider: ModelProvider,
        base_url: str,
        api_key: Optional[str] = None
    ) -> Tuple[bool, List[str], str]:
        """
        测试连接并获取可用模型列表

        Returns:
            Tuple[success, models, message]
        """
        try:
            base_url = await validate_outbound_url(base_url)
        except SecurityValidationError:
            return False, [], "URL被安全策略拒绝"

        if provider == ModelProvider.ZHIPU:
            return await ModelProviderTester._test_zhipu(base_url, api_key)
        elif provider == ModelProvider.ALIBABA_BAILIAN:
            return await ModelProviderTester._test_alibaba_bailian(base_url, api_key)
        elif provider == ModelProvider.OLLAMA:
            return await ModelProviderTester._test_ollama(base_url)
        else:
            return False, [], f"不支持的供应商: {provider}"

    @staticmethod
    async def _test_zhipu(base_url: str, api_key: Optional[str]) -> Tuple[bool, List[str], str]:
        """测试智谱AI连接"""
        if not api_key:
            return False, [], "API Key 不能为空"

        try:
            async with httpx.AsyncClient(timeout=30.0, trust_env=False, follow_redirects=False) as client:
                # 智谱AI使用OpenAI兼容接口获取模型列表
                models_url = f"{base_url.rstrip('/')}/models"
                status_code, data = await _get_json_limited(
                    client,
                    models_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    }
                )
                if status_code == 200:
                    raw_models = data.get("data", []) if isinstance(data, dict) else data
                    return True, _bounded_models(raw_models), "连接成功"
                else:
                    fallback = f"请求失败: HTTP {status_code}"
                    return False, [], fallback

        except httpx.TimeoutException:
            return False, [], "连接超时，请检查网络或URL是否正确"
        except httpx.ConnectError:
            return False, [], "无法连接到服务器，请检查URL是否正确"
        except Exception as e:
            return False, [], f"连接失败 ({type(e).__name__})"

    @staticmethod
    async def _test_alibaba_bailian(base_url: str, api_key: Optional[str]) -> Tuple[bool, List[str], str]:
        """测试阿里云百炼连接"""
        if not api_key:
            return False, [], "API Key 不能为空"

        try:
            async with httpx.AsyncClient(timeout=30.0, trust_env=False, follow_redirects=False) as client:
                # 阿里云百炼使用OpenAI兼容接口获取模型列表
                models_url = f"{base_url.rstrip('/')}/models"
                status_code, data = await _get_json_limited(
                    client,
                    models_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    }
                )
                if status_code == 200:
                    raw_models = data.get("data", []) if isinstance(data, dict) else data
                    return True, _bounded_models(raw_models), "连接成功"
                else:
                    fallback = f"请求失败: HTTP {status_code}"
                    return False, [], fallback

        except httpx.TimeoutException:
            return False, [], "连接超时，请检查网络或URL是否正确"
        except httpx.ConnectError:
            return False, [], "无法连接到服务器，请检查URL是否正确"
        except Exception as e:
            return False, [], f"连接失败 ({type(e).__name__})"

    @staticmethod
    async def _test_ollama(base_url: str) -> Tuple[bool, List[str], str]:
        """测试Ollama连接"""
        try:
            async with httpx.AsyncClient(timeout=30.0, trust_env=False, follow_redirects=False) as client:
                # Ollama获取模型列表的API
                # 尝试两种可能的API路径
                tags_url = f"{base_url.rstrip('/')}/api/tags"

                # 如果base_url是/v1结尾的OpenAI兼容格式，需要调整
                if base_url.rstrip("/").endswith("/v1"):
                    base = base_url.rstrip("/")[:-3]  # 移除 /v1
                    tags_url = f"{base}/api/tags"

                status_code, data = await _get_json_limited(client, tags_url)
                if status_code == 200:
                    raw_models = data.get("models", []) if isinstance(data, dict) else []
                    return True, _bounded_models(raw_models), "连接成功"
                else:
                    return False, [], f"请求失败: HTTP {status_code}"

        except httpx.TimeoutException:
            return False, [], "连接超时，请检查Ollama服务是否运行"
        except httpx.ConnectError:
            return False, [], "无法连接到Ollama服务，请确认服务是否运行"
        except Exception as e:
            return False, [], f"连接失败 ({type(e).__name__})"


async def test_model_service_connection(
    provider: ModelProvider,
    base_url: str,
    api_key: Optional[str] = None
) -> Dict:
    """
    测试模型服务连接

    Returns:
        {
            "success": bool,
            "models": List[str],
            "message": str
        }
    """
    success, models, message = await ModelProviderTester.test_connection(
        provider, base_url, api_key
    )
    return {
        "success": success,
        "models": models,
        "message": message
    }
