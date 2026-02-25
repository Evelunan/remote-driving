from typing import Optional

from logger import get_logger

logger = get_logger(__name__)


class SessionManager:
    """
    管理车辆和驾驶端之间的会话绑定关系。
    """

    def __init__(self):
        # session_id -> {"car": protocol, "driving": protocol}
        self._sessions: dict[str, dict] = {}

        # protocol -> session_id
        self._conn_to_session: dict[object, str] = {}

    def bind_peer(self, session_id: str, role: str, protocol) -> None:
        """
        绑定 car/driving 协议到一个 session。

        Args:
            session_id: 会话ID。
            role: 角色，如 "car" 或 "driving"。
            protocol: 连接协议对象。
        """
        peer = self._sessions.setdefault(session_id, {})
        peer[role] = protocol
        self._conn_to_session[protocol] = session_id

    def unbind(self, protocol) -> Optional[str]:
        """
        断开连接时移除 session 绑定。

        Args:
            protocol: 需要解绑的协议对象。

        Returns:
            解绑的 session_id，如果未找到则返回 None。
        """
        session_id = self._conn_to_session.pop(protocol, None)
        if session_id and session_id in self._sessions:
            # 从会话字典中删除对应的协议
            roles_to_remove = [
                role
                for role, proto in self._sessions[session_id].items()
                if proto is protocol
            ]
            for role in roles_to_remove:
                self._sessions[session_id].pop(role)

            # 如果会话中的所有角色都已断开，则删除整个会话
            if not self._sessions[session_id]:
                self._sessions.pop(session_id)

        return session_id

    def get_peer(self, session_id: str, role: str) -> Optional[object]:
        """
        获取 session 中的指定角色对应的协议。

        Args:
            session_id: 会话ID。
            role: 角色，如 "car" 或 "driving"。

        Returns:
            对应的协议对象，如果会话或角色不存在则返回 None。
        """
        return self._sessions.get(session_id, {}).get(role)

    def get_session_id(self, protocol) -> Optional[str]:
        """
        根据协议对象获取其所属的会话ID。

        Args:
            protocol: 协议对象。

        Returns:
            会话ID，如果协议未绑定则返回 None。
        """
        return self._conn_to_session.get(protocol)

    def has_active_session(self, protocol) -> bool:
        """
        检查协议是否仍处于活动会话中。

        Args:
            protocol: 协议对象。

        Returns:
            如果协议仍被会话引用则返回 True，否则返回 False。
        """
        return protocol in self._conn_to_session


# 创建全局单例
session_manager = SessionManager()