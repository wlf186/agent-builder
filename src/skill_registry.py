"""
Skill注册表 - 管理Skills的注册、扫描和删除
"""
import json
import hashlib
import os
import zipfile
import tempfile
import shutil
import re
import stat
import threading
import unicodedata
import uuid
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime

from .models import SkillConfig, SkillSource
from .security import (
    SecurityValidationError,
    resolve_contained_path,
    validate_archive_member_name,
)
from .storage_paths import ensure_real_directory, validate_regular_file


class SkillRegistry:
    """全局Skill注册表"""

    MAX_ARCHIVE_SIZE = 25 * 1024 * 1024
    MAX_ARCHIVE_MEMBERS = 512
    MAX_ARCHIVE_UNCOMPRESSED_SIZE = 100 * 1024 * 1024
    MAX_ARCHIVE_MEMBER_SIZE = 25 * 1024 * 1024
    MAX_COMPRESSION_RATIO = 100
    MAX_PREVIEW_SIZE = 2 * 1024 * 1024
    MAX_USER_SKILLS = 50

    def __init__(self, data_dir: Path, skills_dir: Path):
        self.data_dir = ensure_real_directory(Path(data_dir))
        self.skills_dir = ensure_real_directory(Path(skills_dir))
        self.builtin_dir = ensure_real_directory(self.skills_dir / "builtin")
        self.user_dir = ensure_real_directory(self.skills_dir / "user")
        self.index_file = self.data_dir / "skills_index.json"
        self.skills: Dict[str, SkillConfig] = {}
        self._lock = threading.RLock()
        self.runtime_tmp_dir = ensure_real_directory(
            self.skills_dir.parent / ".runtime" / "tmp"
        )
        self._load_index()
        self._migrate_user_directories()
        self._scan_builtin_skills()

    def _load_index(self):
        """加载索引文件"""
        if self.index_file.exists():
            try:
                validate_regular_file(self.index_file, allow_missing=False)
                with open(self.index_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for name, config in data.items():
                        self.skills[name] = SkillConfig(**config)
            except Exception as e:
                print(f"加载Skills索引失败: error_type={type(e).__name__}")

    def _save_index(self):
        """保存索引文件"""
        with self._lock:
            ensure_real_directory(self.data_dir)
            validate_regular_file(self.index_file, allow_missing=True)
            data = {
                name: config.model_dump()
                for name, config in self.skills.items()
            }
            encoded = json.dumps(
                data,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if self.index_file.exists():
                try:
                    existing = json.loads(self.index_file.read_text(encoding="utf-8"))
                    if existing == data:
                        return
                except (OSError, json.JSONDecodeError):
                    pass
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.index_file.name}.",
                suffix=".tmp",
                dir=self.index_file.parent,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary_name, 0o600)
                os.replace(temporary_name, self.index_file)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)

    def _normalize_skill_name(self, name: str) -> str:
        """
        规范化Skill名称为小写连字符格式

        规则:
        1. 统一转为小写
        2. 空格替换为连字符
        3. 多个连续连字符合并为一个
        4. 移除首尾连字符

        Args:
            name: 原始名称

        Returns:
            规范化后的名称
        """
        if not name:
            return name

        normalized = unicodedata.normalize("NFKC", str(name)).casefold().strip()
        normalized = re.sub(r"[^\w.-]+", "-", normalized, flags=re.UNICODE)
        normalized = re.sub(r"[-_.]{2,}", "-", normalized).strip("-_.")
        return normalized[:100]

    @staticmethod
    def _user_directory_name(name: str) -> str:
        slug = re.sub(r"[^\w.-]+", "-", name, flags=re.UNICODE).strip("-_.")
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
        return f"{slug[:70]}-{digest}"

    def _migrate_user_directories(self) -> None:
        """Move legacy user directories to unique keys; disable ambiguous aliases."""
        by_path: Dict[str, List[SkillConfig]] = {}
        for config in self.skills.values():
            if config.source == SkillSource.USER:
                by_path.setdefault(config.skill_path, []).append(config)

        changed = False
        for old_relative, configs in by_path.items():
            old_path = (self.skills_dir / old_relative).resolve()
            try:
                old_path.relative_to(self.user_dir.resolve())
            except ValueError:
                for config in configs:
                    config.enabled = False
                changed = True
                continue

            if len(configs) > 1:
                for config in configs:
                    config.skill_path = f"user/{self._user_directory_name(config.name)}"
                    config.enabled = False
                changed = True
                continue

            config = configs[0]
            new_relative = f"user/{self._user_directory_name(config.name)}"
            if config.skill_path == new_relative:
                continue
            new_path = self.skills_dir / new_relative
            if old_path.exists() and not new_path.exists():
                old_path.replace(new_path)
            config.skill_path = new_relative
            if not new_path.exists():
                config.enabled = False
            changed = True

        if changed:
            self._save_index()

    def _parse_skill_md(self, skill_path: Path) -> Tuple[str, str, Dict[str, Any]]:
        """
        解析SKILL.md文件，提取元数据
        返回: (name, description, metadata)

        名称优先级：
        1. frontmatter中的name字段（规范化为小写连字符格式）
        2. 标题中的第一个单词（作为fallback）
        """
        skill_md_path = skill_path / "SKILL.md"
        if not skill_md_path.exists():
            return "", "", {}

        try:
            with open(skill_md_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 提取元数据（从frontmatter）
            metadata = {}

            # 尝试解析 YAML frontmatter
            if content.startswith('---'):
                frontmatter_match = re.search(r'^---\n(.*?)\n---', content, re.DOTALL)
                if frontmatter_match:
                    frontmatter = frontmatter_match.group(1)
                    for line in frontmatter.split('\n'):
                        if ':' in line:
                            key, value = line.split(':', 1)
                            metadata[key.strip()] = value.strip().strip('"').strip("'")

            # 优先使用frontmatter中的name（规范化）
            name = ""
            if 'name' in metadata:
                name = self._normalize_skill_name(metadata['name'])

            # 如果没有frontmatter的name，从标题提取第一个单词作为fallback
            if not name:
                title_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
                if title_match:
                    title = title_match.group(1).strip()
                    # 只取第一个单词（如 "AB-DOCX creation..." -> "AB-DOCX"）
                    first_word = title.split()[0] if title.split() else title
                    name = self._normalize_skill_name(first_word)

            # 提取描述（优先从frontmatter，其次从内容）
            description = ""
            if 'description' in metadata:
                description = metadata['description']
            else:
                # 从内容中提取描述（标题后的第一段非空内容）
                lines = content.split('\n')
                found_title = False
                for line in lines:
                    if line.startswith('# ') and not found_title:
                        found_title = True
                        continue
                    if found_title and line.strip() and not line.startswith('#'):
                        description = line.strip()
                        break

            # 从内容中提取标签
            tags_match = re.search(r'标签[：:]\s*(.+)$', content, re.MULTILINE)
            if tags_match:
                tags_str = tags_match.group(1).strip()
                metadata['tags'] = [t.strip() for t in tags_str.split(',') if t.strip()]

            # 从内容中提取版本
            version_match = re.search(r'版本[：:]\s*([\d.]+)', content)
            if version_match:
                metadata['version'] = version_match.group(1)

            # 从内容中提取作者
            author_match = re.search(r'作者[：:]\s*(.+)$', content, re.MULTILINE)
            if author_match:
                metadata['author'] = author_match.group(1).strip()

            return name, description, metadata

        except Exception as e:
            print(f"解析SKILL.md失败: error_type={type(e).__name__}")
            return "", "", {}

    def _scan_builtin_skills(self):
        """扫描预置Skills目录"""
        if not self.builtin_dir.exists():
            return

        for skill_path in self.builtin_dir.iterdir():
            if skill_path.is_dir() and (skill_path / "SKILL.md").exists():
                name, description, metadata = self._parse_skill_md(skill_path)
                if name:
                    # 获取文件列表
                    files = self._get_skill_files(skill_path)

                    skill_config = SkillConfig(
                        name=name,
                        description=description or f"Skill: {name}",
                        source=SkillSource.BUILTIN,
                        skill_path=f"builtin/{skill_path.name}",
                        version=metadata.get("version", "1.0.0"),
                        author=metadata.get("author"),
                        tags=metadata.get("tags", []),
                        files=files,
                        enabled=True,
                        created_at=metadata.get("created_at"),
                        updated_at=metadata.get("updated_at")
                    )

                    # 如果已存在则更新，否则添加
                    if name in self.skills:
                        # 保留用户可能修改的enabled状态
                        existing = self.skills[name]
                        skill_config.enabled = existing.enabled
                    self.skills[name] = skill_config

        self._save_index()

    def _get_skill_files(self, skill_path: Path) -> List[str]:
        """获取Skill目录下的所有文件"""
        files = []
        for file_path in skill_path.rglob("*"):
            if file_path.is_file():
                rel_path = file_path.relative_to(skill_path)
                files.append(str(rel_path))
        return sorted(files)

    def register_skill(self, config: SkillConfig) -> bool:
        """注册一个Skill"""
        with self._lock:
            existing = self.skills.get(config.name)
            if existing and existing.source == SkillSource.BUILTIN:
                print(f"不能覆盖预置Skill: {config.name}")
                return False
            user_count = sum(
                skill.source == SkillSource.USER for skill in self.skills.values()
            )
            if existing is None and user_count >= self.MAX_USER_SKILLS:
                return False

            config.updated_at = datetime.now().isoformat()
            if not config.created_at:
                config.created_at = config.updated_at

            previous = self.skills.get(config.name)
            self.skills[config.name] = config
            try:
                self._save_index()
            except Exception:
                if previous is None:
                    self.skills.pop(config.name, None)
                else:
                    self.skills[config.name] = previous
                return False
            return True

    def unregister_skill(self, name: str) -> bool:
        """注销一个Skill"""
        with self._lock:
            skill = self.skills.get(name)
            if skill is None:
                return False
            if skill.source == SkillSource.BUILTIN:
                print(f"不能删除预置Skill: {name}")
                return False

            skill_path = (self.skills_dir / skill.skill_path).resolve()
            backup_path = self.user_dir / f".{self._user_directory_name(name)}.{uuid.uuid4().hex}.delete"
            moved = False
            if skill_path.exists() and skill_path.parent == self.user_dir.resolve():
                skill_path.replace(backup_path)
                moved = True

            del self.skills[name]
            try:
                self._save_index()
            except Exception:
                self.skills[name] = skill
                if moved and backup_path.exists():
                    backup_path.replace(skill_path)
                return False
            if moved:
                shutil.rmtree(backup_path, ignore_errors=True)
            return True

    def get_skill(self, name: str) -> Optional[SkillConfig]:
        """获取Skill配置"""
        with self._lock:
            config = self.skills.get(name)
            return config.model_copy(deep=True) if config is not None else None

    def fuzzy_match_skill(self, query_name: str) -> Optional[str]:
        """
        模糊匹配Skill名称

        匹配策略（按优先级）：
        1. 精确匹配
        2. 规范化后精确匹配（大小写不敏感）
        3. 前缀匹配（如"AB-DOCX"匹配"ab-docx"）
        4. 包含匹配（如"docx"匹配"ab-docx"）

        Args:
            query_name: 查询的Skill名称

        Returns:
            匹配到的规范化Skill名称，未匹配返回None
        """
        if not query_name:
            return None

        with self._lock:
            skill_names = tuple(self.skills)

        # 1. 精确匹配
        if query_name in skill_names:
            return query_name

        # 2. 规范化后精确匹配
        normalized_query = self._normalize_skill_name(query_name)
        if normalized_query in skill_names:
            return normalized_query

        # 3. 遍历所有skill进行模糊匹配
        best_match = None
        best_score = 0

        for skill_name in skill_names:
            normalized_skill = self._normalize_skill_name(skill_name)

            # 规范化后精确匹配
            if normalized_query == normalized_skill:
                return skill_name

            # 前缀匹配（查询是skill的前缀，或skill是查询的前缀）
            if normalized_skill.startswith(normalized_query) or normalized_query.startswith(normalized_skill):
                score = max(len(normalized_query), len(normalized_skill))
                if score > best_score:
                    best_score = score
                    best_match = skill_name

            # 包含匹配
            elif normalized_query in normalized_skill or normalized_skill in normalized_query:
                score = min(len(normalized_query), len(normalized_skill))
                if score > best_score:
                    best_score = score
                    best_match = skill_name

        return best_match

    def normalize_skill_name(self, name: str) -> str:
        """
        公共方法：规范化Skill名称

        Args:
            name: 原始名称

        Returns:
            规范化后的名称
        """
        return self._normalize_skill_name(name)

    def get_available_skill_names(self) -> List[str]:
        """获取所有可用的Skill名称列表"""
        with self._lock:
            return list(self.skills.keys())

    def list_skills(self) -> List[SkillConfig]:
        """列出所有Skills"""
        with self._lock:
            return [config.model_copy(deep=True) for config in self.skills.values()]

    def get_skills_by_names(self, names: List[str]) -> List[SkillConfig]:
        """根据名称列表获取Skills"""
        with self._lock:
            return [
                self.skills[name].model_copy(deep=True)
                for name in names
                if name in self.skills
            ]

    def skill_exists(self, name: str) -> bool:
        """检查Skill是否存在"""
        with self._lock:
            return name in self.skills

    def _commit_extracted_skill(
        self,
        extracted_root: Path,
        name: str,
        description: str,
        metadata: Dict[str, Any],
    ) -> Tuple[bool, str, Optional[SkillConfig]]:
        """Atomically publish one fully validated user Skill."""
        with self._lock:
            previous = self.skills.get(name)
            if previous is not None and previous.source == SkillSource.BUILTIN:
                return False, f"Skill名称 '{name}' 与预置Skill冲突", None
            user_count = sum(
                skill.source == SkillSource.USER for skill in self.skills.values()
            )
            if previous is None and user_count >= self.MAX_USER_SKILLS:
                return False, "用户 Skill 数量已达到 50 个上限", None

            safe_name = self._user_directory_name(name)
            dest_path = self.user_dir / safe_name
            if dest_path.is_symlink():
                return False, "Skill 目标目录不安全", None
            staging_path = self.user_dir / f".{safe_name}.{uuid.uuid4().hex}.upload"
            backup_path = self.user_dir / f".{safe_name}.{uuid.uuid4().hex}.backup"
            try:
                # Extraction already occurred under this checkout on the same
                # filesystem.  Rename the validated tree instead of writing a
                # second full copy, which materially reduces SSD wear for
                # large, frequently updated Skill archives.
                extracted_root.replace(staging_path)
                if dest_path.exists():
                    dest_path.replace(backup_path)
                staging_path.replace(dest_path)
            except Exception:
                shutil.rmtree(staging_path, ignore_errors=True)
                if backup_path.exists() and not dest_path.exists():
                    backup_path.replace(dest_path)
                raise

            raw_tags = metadata.get("tags", [])
            if isinstance(raw_tags, str):
                raw_tags = [
                    item.strip(" []'\"")
                    for item in raw_tags.split(",")
                    if item.strip(" []'\"")
                ]
            tags = [str(tag)[:100] for tag in list(raw_tags)[:50]]
            author = metadata.get("author")
            skill_config = SkillConfig(
                name=name,
                description=(description or f"用户上传的Skill: {name}")[:10_000],
                source=SkillSource.USER,
                skill_path=f"user/{safe_name}",
                version=str(metadata.get("version", "1.0.0"))[:100],
                author=str(author)[:200] if author is not None else None,
                tags=tags,
                files=self._get_skill_files(dest_path),
                enabled=True,
                created_at=datetime.now().isoformat(),
                updated_at=datetime.now().isoformat(),
            )

            self.skills[name] = skill_config
            try:
                self._save_index()
            except Exception:
                if previous is None:
                    self.skills.pop(name, None)
                else:
                    self.skills[name] = previous
                shutil.rmtree(dest_path, ignore_errors=True)
                if backup_path.exists():
                    backup_path.replace(dest_path)
                raise

            shutil.rmtree(backup_path, ignore_errors=True)
            if previous and previous.source == SkillSource.USER:
                previous_path = (self.skills_dir / previous.skill_path).resolve()
                if (
                    previous_path != dest_path.resolve()
                    and previous_path.parent == self.user_dir.resolve()
                ):
                    shutil.rmtree(previous_path, ignore_errors=True)

            return True, f"Skill '{name}' 上传成功", skill_config.model_copy(deep=True)

    def extract_zip_and_register(self, zip_path: Path, skill_name: Optional[str] = None) -> Tuple[bool, str, Optional[SkillConfig]]:
        """
        解压zip包并注册Skill
        返回: (success, message, skill_config)
        """
        try:
            zip_path = Path(zip_path)
            if not zip_path.is_file() or zip_path.is_symlink():
                return False, "Zip包路径无效", None
            if zip_path.stat().st_size > self.MAX_ARCHIVE_SIZE:
                return False, "Zip包过大，最大支持25MB", None

            with tempfile.TemporaryDirectory(dir=self.runtime_tmp_dir) as temp_dir:
                temp_path = Path(temp_dir)

                # Validate the complete central directory before extracting a
                # single byte.  zipfile.extractall() is intentionally avoided:
                # explicit streaming limits protect against traversal, symlink
                # and decompression-bomb archives.
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    members = zip_ref.infolist()
                    if len(members) > self.MAX_ARCHIVE_MEMBERS:
                        return False, "Zip包文件数量超过限制", None

                    total_declared_size = 0
                    seen_names = set()
                    validated_members = []
                    for member in members:
                        pure_path = validate_archive_member_name(member.filename.rstrip('/'))
                        normalised_name = pure_path.as_posix().casefold()
                        if normalised_name in seen_names:
                            return False, "Zip包包含重复文件名", None
                        seen_names.add(normalised_name)

                        unix_mode = (member.external_attr >> 16) & 0o170000
                        if unix_mode and stat.S_ISLNK(unix_mode):
                            return False, "Zip包不允许包含软链接", None
                        if unix_mode and not (stat.S_ISREG(unix_mode) or stat.S_ISDIR(unix_mode)):
                            return False, "Zip包不允许包含特殊文件", None
                        if member.flag_bits & 0x1:
                            return False, "Zip包不允许包含加密文件", None
                        if member.compress_type not in {
                            zipfile.ZIP_STORED,
                            zipfile.ZIP_DEFLATED,
                        }:
                            return False, "Zip包使用了不支持的压缩算法", None
                        if member.file_size > self.MAX_ARCHIVE_MEMBER_SIZE:
                            return False, "Zip包中的单个文件过大", None
                        total_declared_size += member.file_size
                        if total_declared_size > self.MAX_ARCHIVE_UNCOMPRESSED_SIZE:
                            return False, "Zip包解压后总大小超过限制", None
                        if (
                            member.file_size > 0
                            and member.file_size / max(member.compress_size, 1) > self.MAX_COMPRESSION_RATIO
                        ):
                            return False, "Zip包压缩比异常", None
                        validated_members.append((member, pure_path))

                    total_written = 0
                    for member, pure_path in validated_members:
                        target = temp_path.joinpath(*pure_path.parts)
                        if member.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                            os.chmod(target, 0o700)
                            continue

                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.chmod(target.parent, 0o700)
                        member_written = 0
                        with zip_ref.open(member, "r") as source, open(target, "xb") as destination:
                            while True:
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                member_written += len(chunk)
                                total_written += len(chunk)
                                if member_written > self.MAX_ARCHIVE_MEMBER_SIZE:
                                    raise SecurityValidationError("Zip包中的单个文件超过限制")
                                if total_written > self.MAX_ARCHIVE_UNCOMPRESSED_SIZE:
                                    raise SecurityValidationError("Zip包解压后总大小超过限制")
                                destination.write(chunk)
                        if member_written != member.file_size:
                            raise SecurityValidationError("Zip包成员实际大小与声明不符")
                        os.chmod(target, 0o600)

                # 查找SKILL.md文件
                skill_files = list(temp_path.rglob("SKILL.md"))
                if not skill_files:
                    return False, "Zip包中未找到SKILL.md文件", None
                if len(skill_files) != 1:
                    return False, "Zip包必须且只能包含一个SKILL.md文件", None
                skill_md_path = skill_files[0]
                extracted_root = skill_md_path.parent
                if skill_md_path.stat().st_size > self.MAX_PREVIEW_SIZE:
                    return False, "SKILL.md文件过大", None

                # 解析元数据
                name, description, metadata = self._parse_skill_md(extracted_root)
                if not name:
                    # 如果没有从md中提取到名称，使用文件夹名或参数
                    name = self._normalize_skill_name(skill_name or extracted_root.name)
                if not name:
                    return False, "Skill名称无效", None

                return self._commit_extracted_skill(
                    extracted_root,
                    name,
                    description,
                    metadata,
                )

        except (zipfile.BadZipFile, zipfile.LargeZipFile):
            return False, "无效的zip文件", None
        except SecurityValidationError:
            return False, "Skill压缩包未通过安全校验", None
        except Exception as e:
            return False, f"解压失败 ({type(e).__name__})", None

    def get_skill_file_content(self, name: str, file_path: str) -> Optional[str]:
        """获取Skill文件内容"""
        skill = self.get_skill(name)
        if not skill:
            return None

        try:
            skill_root = resolve_contained_path(
                self.skills_dir,
                skill.skill_path,
                must_exist=True,
                require_file=False,
            )
            skill_full_path = resolve_contained_path(skill_root, file_path)
        except SecurityValidationError:
            return None

        if skill_full_path.stat().st_size > self.MAX_PREVIEW_SIZE:
            return None

        try:
            with open(skill_full_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            print(f"读取文件失败: error_type={type(e).__name__}")
            return None
