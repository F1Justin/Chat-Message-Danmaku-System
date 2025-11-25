"""
配置管理模块
使用 Pydantic BaseSettings 进行类型安全的配置管理
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class DatabaseSettings(BaseSettings):
    """数据库配置"""
    
    model_config = SettingsConfigDict(
        env_prefix="DB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
    
    user: str = Field(..., description="数据库用户名")
    password: str = Field(..., description="数据库密码")
    host: str = Field(..., description="数据库主机")
    port: int = Field(default=5432, description="数据库端口")
    name: str = Field(..., description="数据库名称")
    
    @property
    def async_url(self) -> str:
        """获取异步数据库连接 URL"""
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"
    
    @property
    def dsn(self) -> str:
        """获取 asyncpg 原生 DSN"""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class AppSettings(BaseSettings):
    """应用程序配置"""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
    
    # 服务器配置
    host: str = Field(default="127.0.0.1", alias="APP_HOST")
    port: int = Field(default=8000, alias="APP_PORT")
    
    # 日志级别
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    
    # 安全配置
    allowed_hosts: List[str] = Field(
        default=["127.0.0.1", "::1", "localhost"],
        description="允许访问的主机列表"
    )
    
    # 弹幕配置
    default_danmaku_speed: int = Field(
        default=10, 
        ge=5, 
        le=60,
        description="默认弹幕速度（秒）"
    )
    max_danmaku_count: int = Field(
        default=100,
        description="最大同时显示的弹幕数量"
    )
    
    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v_upper = v.upper()
        if v_upper not in valid_levels:
            raise ValueError(f"无效的日志级别: {v}. 可选: {valid_levels}")
        return v_upper


class RuntimeConfig:
    """
    运行时配置管理
    管理群组别名、常用群组等可变配置
    """
    
    def __init__(self, config_file: str = "config.json"):
        self.config_file = Path(config_file)
        self.group_aliases: Dict[str, str] = {}
        self.favorite_groups: List[str] = []
        self.active_group_id: Optional[str] = None
        self.danmaku_speed: int = 10
        self._load()
    
    def _load(self) -> None:
        """从文件加载配置"""
        try:
            if self.config_file.exists():
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.group_aliases = data.get("group_aliases", {})
                    self.favorite_groups = data.get("favorite_groups", [])
                    self.active_group_id = data.get("active_group_id")
                    self.danmaku_speed = data.get("danmaku_speed", 10)
                    logger.info(
                        f"已加载配置: 别名={len(self.group_aliases)} "
                        f"常用群={len(self.favorite_groups)} "
                        f"速度={self.danmaku_speed}s"
                    )
            else:
                logger.info("配置文件不存在，使用默认设置")
        except Exception as e:
            logger.error(f"加载配置出错: {e}")
    
    def save(self) -> None:
        """保存配置到文件"""
        try:
            data = {
                "group_aliases": self.group_aliases,
                "favorite_groups": self.favorite_groups,
                "active_group_id": self.active_group_id,
                "danmaku_speed": self.danmaku_speed
            }
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info("配置已保存")
        except Exception as e:
            logger.error(f"保存配置出错: {e}")
    
    def set_group_alias(self, group_id: str, alias: str) -> None:
        """设置群组别名"""
        self.group_aliases[str(group_id)] = alias
        self.save()
    
    def toggle_favorite(self, group_id: str, is_favorite: bool) -> None:
        """切换常用群组状态"""
        group_id = str(group_id)
        if is_favorite and group_id not in self.favorite_groups:
            self.favorite_groups.append(group_id)
        elif not is_favorite and group_id in self.favorite_groups:
            self.favorite_groups.remove(group_id)
        self.save()
    
    def set_danmaku_speed(self, speed: int) -> bool:
        """设置弹幕速度"""
        if 5 <= speed <= 60:
            self.danmaku_speed = speed
            self.save()
            return True
        return False


# 全局配置实例（延迟初始化）
_db_settings: Optional[DatabaseSettings] = None
_app_settings: Optional[AppSettings] = None
_runtime_config: Optional[RuntimeConfig] = None


def get_db_settings() -> DatabaseSettings:
    """获取数据库配置单例"""
    global _db_settings
    if _db_settings is None:
        _db_settings = DatabaseSettings()
    return _db_settings


def get_app_settings() -> AppSettings:
    """获取应用配置单例"""
    global _app_settings
    if _app_settings is None:
        _app_settings = AppSettings()
    return _app_settings


def get_runtime_config() -> RuntimeConfig:
    """获取运行时配置单例"""
    global _runtime_config
    if _runtime_config is None:
        _runtime_config = RuntimeConfig()
    return _runtime_config

