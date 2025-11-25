"""
WebSocket 连接管理模块
管理所有活跃连接、过滤规则和消息广播
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class ConnectionFilter:
    """连接过滤设置"""
    enabled: bool = False
    allowed_groups: Set[str] = field(default_factory=set)
    
    def should_receive(self, group_id: str) -> bool:
        """判断是否应该接收该群组的消息"""
        if not self.enabled:
            return True  # 未启用过滤，接收所有消息
        return str(group_id) in self.allowed_groups


@dataclass
class ManagedConnection:
    """被管理的 WebSocket 连接"""
    websocket: WebSocket
    filter: ConnectionFilter = field(default_factory=ConnectionFilter)
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    async def send_json(self, data: Dict[str, Any]) -> bool:
        """发送 JSON 数据"""
        try:
            await self.websocket.send_text(json.dumps(data, default=str))
            return True
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False


class ConnectionManager:
    """
    WebSocket 连接管理器（单例模式）
    负责管理所有活跃连接、过滤规则和消息广播
    """
    
    _instance: Optional["ConnectionManager"] = None
    
    def __new__(cls) -> "ConnectionManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._connections: List[ManagedConnection] = []
        self._session_to_group_cache: Dict[str, str] = {}
        
        # 全局过滤状态（新连接继承）
        self._global_filter_enabled: bool = False
        self._global_allowed_groups: Set[str] = set()
        
        self._initialized = True
        logger.info("ConnectionManager 初始化完成")
    
    @property
    def connection_count(self) -> int:
        """获取当前连接数"""
        return len(self._connections)
    
    @property
    def global_filter_enabled(self) -> bool:
        return self._global_filter_enabled
    
    @property
    def global_allowed_groups(self) -> List[str]:
        return list(self._global_allowed_groups)
    
    async def connect(self, websocket: WebSocket) -> ManagedConnection:
        """
        接受新的 WebSocket 连接
        新连接继承当前全局过滤状态
        """
        await websocket.accept()
        
        # 创建连接并继承全局过滤状态
        conn_filter = ConnectionFilter(
            enabled=self._global_filter_enabled,
            allowed_groups=self._global_allowed_groups.copy()
        )
        connection = ManagedConnection(websocket=websocket, filter=conn_filter)
        self._connections.append(connection)
        
        logger.info(f"新连接已建立，当前连接数: {self.connection_count}")
        
        # 广播连接数更新
        await self.broadcast_stats()
        
        return connection
    
    def disconnect(self, connection: ManagedConnection) -> None:
        """断开连接"""
        if connection in self._connections:
            self._connections.remove(connection)
            logger.info(f"连接已断开，当前连接数: {self.connection_count}")
    
    async def broadcast_stats(self) -> None:
        """广播统计信息"""
        stats = {
            "type": "stats",
            "connections": self.connection_count,
        }
        await self.broadcast_to_all(stats)
    
    async def broadcast_to_all(self, data: Dict[str, Any]) -> int:
        """
        向所有连接广播消息
        返回成功发送的连接数
        """
        success_count = 0
        failed_connections = []
        
        for conn in self._connections:
            if await conn.send_json(data):
                success_count += 1
            else:
                failed_connections.append(conn)
        
        # 移除失败的连接
        for conn in failed_connections:
            self.disconnect(conn)
        
        return success_count
    
    async def broadcast_danmaku(
        self, 
        group_id: str,
        user_id: str,
        content: str,
        message_id: str,
        timestamp: datetime
    ) -> int:
        """
        广播弹幕消息（带过滤）
        只发送给订阅了该群组的连接
        """
        danmaku_data = {
            "type": "danmaku",
            "group_id": str(group_id),
            "user_id": str(user_id),
            "content": content,
            "message_id": message_id,
            "time": timestamp.isoformat() if timestamp else None
        }
        
        sent_count = 0
        failed_connections = []
        
        for conn in self._connections:
            if conn.filter.should_receive(group_id):
                if await conn.send_json(danmaku_data):
                    sent_count += 1
                else:
                    failed_connections.append(conn)
        
        # 移除失败的连接
        for conn in failed_connections:
            self.disconnect(conn)
        
        logger.debug(f"弹幕已发送给 {sent_count}/{self.connection_count} 个连接")
        return sent_count
    
    async def broadcast_setting(self, key: str, value: Any) -> None:
        """广播设置更新"""
        await self.broadcast_to_all({
            "type": "setting_update",
            "key": key,
            "value": value
        })
        logger.info(f"已广播设置更新: {key}={value}")
    
    def set_global_filter(self, enabled: bool, allowed_groups: List[str]) -> None:
        """
        设置全局过滤状态
        同时更新所有现有连接的过滤器
        """
        self._global_filter_enabled = enabled
        self._global_allowed_groups = set(allowed_groups)
        
        # 更新所有连接的过滤器
        for conn in self._connections:
            conn.filter.enabled = enabled
            conn.filter.allowed_groups = self._global_allowed_groups.copy()
        
        logger.info(
            f"全局过滤器已更新: enabled={enabled}, "
            f"groups={allowed_groups}, 影响 {self.connection_count} 个连接"
        )
    
    async def broadcast_filter_update(self) -> None:
        """广播过滤器更新通知"""
        await self.broadcast_to_all({
            "type": "broadcast_filter_update",
            "filter_enabled": self._global_filter_enabled,
            "allowed_groups": list(self._global_allowed_groups)
        })
    
    # Session ID 到 Group ID 的缓存管理
    def cache_session_mapping(self, session_id: str, group_id: str) -> None:
        """缓存 session_id 到 group_id 的映射"""
        self._session_to_group_cache[str(session_id)] = str(group_id)
    
    def get_cached_group_id(self, session_id: str) -> Optional[str]:
        """从缓存获取 group_id"""
        return self._session_to_group_cache.get(str(session_id))
    
    def clear_cache(self) -> None:
        """清除所有缓存"""
        self._session_to_group_cache.clear()
        logger.info("缓存已清除")


# 获取全局单例
def get_connection_manager() -> ConnectionManager:
    """获取 ConnectionManager 单例"""
    return ConnectionManager()

