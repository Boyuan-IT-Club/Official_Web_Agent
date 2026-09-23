"""会话注册表:进程内运行时会话的生命周期与删除协调(无 FastAPI 依赖)。

会话 = thread_id;运行时对象(agent / 用户 token / 轮锁)只在进程内存,
会话档案与 checkpoint 在 PG——淘汰只删运行时对象,携原 session_id 重入
由 routes 的 _get_or_create_session 走档案恢复路径,上下文不丢。

时钟作参数注入(evict 的 now 由调用方传),测试不必 sleep。
多副本部署下不得依赖本进程内表做权限判断(恢复走 PG 档案 resolve_thread)。

并发契约:lock 保护下面全部结构;持锁期间不得做磁盘 IO——删除端点
登记后放锁再做清理(见 routes.delete_my_session),否则锁被磁盘 IO
长占,其余会话的读写全部排队。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

from official_agent.graphs.identity import ResolvedIdentity


class SessionState:
    """一个会话的运行时状态:装配好的 agent + 身份 + 用户 JWT。

    checkpointer(记忆)是共享的(进程级,thread_id 隔离);这里只存每个
    会话不能共享的东西:绑定该用户 token 的 agent。配置指纹用于热生效
    (PUT /admin/config 后下一轮比对,不同则重建 agent)。
    """

    def __init__(
        self,
        session_id: str,
        identity: ResolvedIdentity,
        user_token: str,
        agent: Any,
    ) -> None:
        self.session_id = session_id
        self.identity = identity
        self.user_token = user_token
        self.agent = agent
        # 装配 agent 时的配置指纹(HOT_KEYS 值哈希);None = 未记录
        self.applied_config_fingerprint: str | None = None
        # 轮计数(1 起),压缩事件留痕「触发轮」用
        self.turns = 0
        # 同会话并发轮次串行化——轮次全程持有,第二个并发请求按 busy 立即回
        self.turn_lock = asyncio.Lock()


# 注册表:session_id → 运行时状态。有界 TTL+LRU。
sessions: OrderedDict[str, SessionState] = OrderedDict()
# 最近访问时刻(evict 的 TTL 依据;touch/put 时更新)
last_access: dict[str, float] = {}
# 删除协调:正在删除的 session_id——删除期间拒绝新轮次。仅靠 turn_lock 不够:
# 会话不在内存(重启/LRU 淘汰)时,续传路径会为同一 thread_id 重建**新对象 +
# 新锁**,旧锁的检查对它无效。删除开始即登记,新轮次两条入口(内存命中/
# 档案恢复)都先查它。
deleting: set[str] = set()

lock = asyncio.Lock()


def evict_locked(now: float) -> None:
    """TTL+LRU 淘汰(调用方持 lock)。只删运行时,不碰 PG。

    在途轮次(turn_lock 被持)一律不淘汰。淘汰执行中的 SessionState 会让
    同一 session_id 的下一个请求在内存未命中,走档案恢复路径重建出一个
    **新对象 + 新锁**,两个 agent 随后并发写同一 checkpoint thread(旧对象
    还在 astream 里)。容量不足且全部在途时宁可不淘汰,留待轮末再清。
    """
    from official_agent.config import get_settings

    settings = get_settings()
    ttl = max(int(settings.session_registry_ttl_seconds), 1)
    cap = max(int(settings.session_registry_max), 1)

    def _evictable(sid: str) -> bool:
        state = sessions.get(sid)
        return state is not None and not state.turn_lock.locked()

    expired = [sid for sid, ts in last_access.items() if now - ts > ttl and _evictable(sid)]
    for sid in expired:
        sessions.pop(sid, None)
        last_access.pop(sid, None)
    # LRU:从最旧开始挑可淘汰项,在途的跳过(不阻断其余淘汰)
    while len(sessions) > cap:
        victim = next((sid for sid in sessions if _evictable(sid)), None)
        if victim is None:  # 全部在途:放弃本轮淘汰,不制造双锁
            break
        sessions.pop(victim, None)
        last_access.pop(victim, None)
