"""Re-export VPN tools for AI (legacy import path)."""

from support_bot.vpn_tools import (
    TOOL_DEFINITIONS_READONLY,
    TOOL_DEFINITIONS_STAFF,
    execute_vpn_tool as execute_tool,
)

__all__ = ["TOOL_DEFINITIONS_READONLY", "TOOL_DEFINITIONS_STAFF", "execute_tool"]
