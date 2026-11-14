"""LangChain tools that route every call through a Gateway."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.tools import StructuredTool

if TYPE_CHECKING:
    from .gateway import Gateway


def langchain_tools(gw: Gateway) -> list[StructuredTool]:
    def list_dir(path: str = ".") -> str:
        """List the files and folders in a directory of the repo. Folders end with '/'."""
        return gw.call("list_dir", {"path": path})

    def read_file(path: str) -> str:
        """Read a file from the repo. Paths are relative to the repo root."""
        return gw.call("read_file", {"path": path})

    def grep(pattern: str, path: str = ".") -> str:
        """Search files under a path for a regex pattern. Returns path:line: text for each match."""
        return gw.call("grep", {"pattern": pattern, "path": path})

    def write_file(path: str, content: str) -> str:
        """Create or overwrite a file in the repo with the full new content."""
        return gw.call("write_file", {"path": path, "content": content})

    def run_tests() -> str:
        """Run the repo's test suite and return the result."""
        return gw.call("run_tests", {})

    def request_write(path: str, reason: str) -> str:
        """Ask a person for permission to write a file that was blocked. Explain why it's needed."""
        return gw.call("request_write", {"path": path, "reason": reason})

    return [
        StructuredTool.from_function(fn)
        for fn in (list_dir, read_file, grep, write_file, run_tests, request_write)
    ]
