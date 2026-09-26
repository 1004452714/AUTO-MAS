#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team

#   This file is part of AUTO-MAS.

#   AUTO-MAS is free software: you can redistribute it and/or modify
#   it under the terms of the GNU Affero General Public License as
#   published by the Free Software Foundation, either version 3 of
#   the License, or (at your option) any later version.

#   AUTO-MAS is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
#   Affero General Public License for more details.

#   You should have received a copy of the GNU Affero General Public License
#   along with AUTO-MAS. If not, see <https://www.gnu.org/licenses/>.

"""
zstd 解压。

Sophon 的清单与 chunk 都是 zstd 压缩帧，必须用 libzstd 解压；Python 侧统一走
``zstandard`` 包——它在 ``pyproject.toml`` 里是硬依赖，因此不做多后端降级探测。

提供两个入口：
    ``decompress(data) -> bytes``           一次性解压
    ``iter_decompress(stream) -> iterator`` 流式解压（大 chunk 用）
"""

from __future__ import annotations

import threading
from typing import Any, Iterator

import zstandard

__all__ = [
    "decompress",
    "iter_decompress",
    "ZstdError",
]


class ZstdError(RuntimeError):
    """zstd 解压失败。"""


#: 线程各自的解压器：``zstandard.ZstdDecompressor`` 内部持有可复用的解压上下文，
#: **不是线程安全的**。chunk 下载是多线程并发解压，共享单例会出现
#: ``Data corruption detected`` / ``Unknown frame descriptor`` 之类的交叉损坏
#: （实测 2026-09-25），所以按 ``threading.local`` 每线程惰性独享一个实例。
_ZSTD_LOCAL = threading.local()


def _decompressor() -> zstandard.ZstdDecompressor:
    """返回当前线程独享的 ``zstandard.ZstdDecompressor``。

    Returns:
        惰性创建、绑定在当前线程上的解压器实例。
    """
    impl = getattr(_ZSTD_LOCAL, "impl", None)
    if impl is None:
        impl = _ZSTD_LOCAL.impl = zstandard.ZstdDecompressor()
    return impl


def decompress(data: bytes) -> bytes:
    """一次性解压一段 zstd 数据。

    Args:
        data: 压缩后的字节。

    Returns:
        解压后的原始字节。

    Raises:
        ZstdError: zstandard 解压失败时。
    """
    try:
        return _decompressor().decompress(data)
    except zstandard.ZstdError as exc:
        raise ZstdError(f"zstd 解压失败: {exc}") from exc


def iter_decompress(stream: Any, chunk_size: int = 65536) -> Iterator[bytes]:
    """流式解压，边读边产出，内存友好（大 chunk 用）。

    Args:
        stream: 提供 ``read()`` 的类文件对象（原始压缩流）。
        chunk_size: 每块最大字节数。

    Yields:
        解压后的数据块（``bytes``）。

    Raises:
        ZstdError: zstandard 解压失败时。
    """
    try:
        with _decompressor().stream_reader(stream) as reader:
            while True:
                chunk = reader.read(chunk_size)
                if not chunk:
                    break
                yield chunk
    except zstandard.ZstdError as exc:
        raise ZstdError(f"zstd 解压失败: {exc}") from exc
