"""
环境创建后台任务管理器

负责在后台异步创建项目内 uv 环境，避免阻塞 API 响应。
"""
import asyncio
from typing import Dict, Optional
from datetime import datetime

from .environment_manager import EnvironmentManager
from .models import EnvironmentStatus


class EnvironmentCreator:
    """环境创建后台任务管理器"""

    def __init__(
        self,
        environment_manager: EnvironmentManager,
        max_concurrent: int = 3
    ):
        """
        初始化环境创建器

        Args:
            environment_manager: 环境管理器实例
            max_concurrent: 最大并发创建数量
        """
        self.environment_manager = environment_manager
        self.max_concurrent = max_concurrent
        self._tasks: Dict[str, asyncio.Task] = {}  # agent_name -> task
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._task_lock = asyncio.Lock()

    async def create(
        self,
        agent_name: str,
        python_version: str = "3.11"
    ) -> bool:
        """
        异步创建 uv 环境

        此方法立即返回，在后台任务中执行实际创建工作。

        Args:
            agent_name: 智能体名称
            python_version: Python版本

        Returns:
            bool: 是否成功启动创建任务
        """
        async with self._task_lock:
            # Check-and-register atomically so duplicate requests cannot replace
            # the tracked task and make cancellation miss the real creator.
            existing_task = self._tasks.get(agent_name)
            if existing_task is not None and not existing_task.done():
                print(f"[ENV_CREATOR] 环境创建任务已在进行中: {agent_name}")
                return True

            task = asyncio.create_task(
                self._create_with_semaphore(agent_name, python_version)
            )
            self._tasks[agent_name] = task

        print(f"[ENV_CREATOR] 已启动环境创建任务: {agent_name}")
        return True

    async def _create_with_semaphore(
        self,
        agent_name: str,
        python_version: str
    ):
        """带并发控制的环境创建"""
        current = asyncio.current_task()
        try:
            async with self._semaphore:
                await self._do_create(agent_name, python_version)
        finally:
            # This also runs if cancellation happens while waiting for capacity.
            # Do not let an older task remove a newer registration.
            async with self._task_lock:
                if self._tasks.get(agent_name) is current:
                    self._tasks.pop(agent_name, None)

    async def _do_create(
        self,
        agent_name: str,
        python_version: str
    ):
        """实际执行环境创建"""
        try:
            # 检查环境是否已存在且就绪
            existing = await self.environment_manager.get_environment_status(agent_name)
            if existing and existing.status == EnvironmentStatus.READY:
                print(f"[ENV_CREATOR] 环境已存在且就绪: {agent_name}")
                return

            # 如果环境处于错误状态，先清理
            if existing and existing.status == EnvironmentStatus.ERROR:
                print(f"[ENV_CREATOR] 清理失败的环境，重新创建: {agent_name}")
                try:
                    await self.environment_manager.delete_environment(agent_name)
                except Exception as e:
                    print(
                        "[ENV_CREATOR] 清理失败环境时出错: "
                        f"error_type={type(e).__name__}"
                    )

            # 执行环境创建
            print(f"[ENV_CREATOR] 开始创建环境: {agent_name}")
            start_time = datetime.now()

            await self.environment_manager.create_environment(
                agent_name=agent_name,
                python_version=python_version
            )

            elapsed = (datetime.now() - start_time).total_seconds()
            print(f"[ENV_CREATOR] 环境创建完成: {agent_name}, 耗时: {elapsed:.1f}秒")

        except Exception as e:
            print(
                f"[ENV_CREATOR] 环境创建失败: {agent_name}, "
                f"error_type={type(e).__name__}"
            )
            # EnvironmentManager persists a sanitized ERROR state while holding
            # the per-Agent writer gate. Writing it here could race deletion and
            # resurrect metadata after the environment was removed.

    async def cancel(self, agent_name: str) -> bool:
        """
        取消进行中的环境创建任务

        Args:
            agent_name: 智能体名称

        Returns:
            bool: 是否成功取消
        """
        task = self._tasks.get(agent_name)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                self._tasks.pop(agent_name, None)
            print(f"[ENV_CREATOR] 已取消环境创建任务: {agent_name}")
            return True
        return False

    async def get_task_status(self, agent_name: str) -> Optional[str]:
        """
        获取任务状态

        Args:
            agent_name: 智能体名称

        Returns:
            "running" | "done" | "failed" | None
        """
        task = self._tasks.get(agent_name)
        if not task:
            return None

        if task.done():
            if task.exception():
                return "failed"
            return "done"

        return "running"

    def has_running_task(self, agent_name: str) -> bool:
        """
        检查是否有运行中的任务

        Args:
            agent_name: 智能体名称

        Returns:
            bool: 是否有运行中的任务
        """
        task = self._tasks.get(agent_name)
        return task is not None and not task.done()

    async def shutdown(self):
        """关闭所有进行中的任务"""
        for agent_name, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._tasks.clear()
        print("[ENV_CREATOR] 所有后台任务已关闭")

    def get_active_tasks(self) -> list[str]:
        """获取当前活跃的任务列表"""
        return [
            name for name, task in self._tasks.items()
            if not task.done()
        ]

    def get_concurrent_count(self) -> int:
        """获取当前并发创建数量"""
        return self.max_concurrent - self._semaphore._value
