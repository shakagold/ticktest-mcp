"""ticktest-mcp 入口 — 支持 python -m ticktest_mcp"""

import asyncio
from ticktest_mcp.server import main

if __name__ == "__main__":
    asyncio.run(main())
