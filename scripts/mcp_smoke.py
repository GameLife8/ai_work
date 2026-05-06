"""端到端冒烟：连本地 MCP server，列出 tool，调一次 platform_list_skills。"""

import asyncio

from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession


URL = "http://127.0.0.1:8765/mcp"


async def main() -> None:
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"tools count: {len(tools.tools)}")
            for t in tools.tools[:5]:
                print(f"  - {t.name}: {t.description[:40]}...")
            print()
            result = await session.call_tool("platform_list_skills", {})
            print("platform_list_skills returned content blocks:", len(result.content))


if __name__ == "__main__":
    asyncio.run(main())
