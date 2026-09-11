"""MCP server exposing specky's tools over stdio. Tools beyond `ping` land in later phases."""

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("specky")


@mcp.tool()
def ping() -> str:
    """Health-check tool used to verify the specky MCP server is wired up correctly."""
    return "pong"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
