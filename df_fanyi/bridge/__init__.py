"""DF-FanYi 游戏↔引擎桥(ticket-008)。

JSON-RPC 2.0 over JSON Lines; 传输: loopback TCP(游戏侧 DFHack luasocket
官方 API 仅 TCP, 见 docs/audits/DFHACK_INTEGRATION.md §5)与 unix socket
(主机侧工具/测试)双支持, 同一协议层。
"""