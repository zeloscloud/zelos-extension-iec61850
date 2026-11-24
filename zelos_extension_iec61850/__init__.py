"""Zelos GOOSE extension.

IEC 61850 GOOSE subscriber: live capture (Linux AF_PACKET, macOS /dev/bpf, Windows Npcap) and
pcap replay, decoded by the Rust-cored `zelos-packet` package.
"""

from . import _agent_env  # noqa: F401  (first: sets the environment before anything reads it)
from .capture import (
    CaptureDeniedError,
    CaptureSession,
    ConfigError,
    InterfaceConfig,
    parse_interfaces,
    replay_pcap,
)

#: The namespace actions are addressed under. One definition for both the live
#: registration (`zelos_sdk.init(name=ACTION_PREFIX, actions=True)`) and the
#: at-rest inventory the packaging step dumps from `main.py`, which re-exports
#: it. A mismatch silently produces two unrelated action trees.
#:
#: Equals `name` in `extension.toml`, so actions have one address whether or
#: not the extension is running, and it needs no quoting in a shell.
ACTION_PREFIX = "IEC-61850"

__all__ = [
    "ACTION_PREFIX",
    "CaptureDeniedError",
    "CaptureSession",
    "ConfigError",
    "InterfaceConfig",
    "parse_interfaces",
    "replay_pcap",
]
