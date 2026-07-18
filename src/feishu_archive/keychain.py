from __future__ import annotations

import subprocess
from dataclasses import dataclass


KEYCHAIN_SERVICE = "com.fermiwang.feishu-archive"


class KeychainError(RuntimeError):
    pass


@dataclass
class KeychainStore:
    service: str = KEYCHAIN_SERVICE

    def set(self, account: str, value: str) -> None:
        if not value:
            raise ValueError("拒绝保存空的钥匙串值")
        result = subprocess.run(
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-s",
                self.service,
                "-a",
                account,
                "-w",
                value,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise KeychainError(result.stderr.strip() or "无法写入 macOS 钥匙串")

    def get(self, account: str) -> str | None:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                self.service,
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 44:
            return None
        if result.returncode != 0:
            raise KeychainError(result.stderr.strip() or "无法读取 macOS 钥匙串")
        return result.stdout.rstrip("\n")

    def delete(self, account: str) -> None:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "delete-generic-password",
                "-s",
                self.service,
                "-a",
                account,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode not in (0, 44):
            raise KeychainError(result.stderr.strip() or "无法删除 macOS 钥匙串项目")


class MemoryTokenStore:
    """Test double with the same interface as KeychainStore."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, account: str, value: str) -> None:
        self.values[account] = value

    def get(self, account: str) -> str | None:
        return self.values.get(account)

    def delete(self, account: str) -> None:
        self.values.pop(account, None)
