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

"""Sophon 下载链路：清单 protobuf 解码、zstd 解压、清单解析与分块下载。

整条链路（上游的等价流程）::

    getGameBranches                      拿 package_id / branch / password / tag
        └─ getBuild(plat_app=biz, ...)   拿 manifests[]（每个 matching_field 一条）
            └─ GET {manifest_download.url_prefix}/{manifest.id}
                → zstd 解压 → protobuf 解析 → assets[]
                    └─ GET {chunk_download.url_prefix}/{chunk_name}
                        → zstd 解压 → 按 chunk_on_file_offset 写入目标文件
                        → 校验 chunk 解压后 MD5 → 校验整文件 MD5

关键不变式：

    * ``matching_field`` 决定清单类别，主资源用 ``game``
    * chunk 是按**偏移直写**，不是先落临时文件再合并

清单与差分档案的正文都是 protobuf。这里用 protobuf 官方运行时按 schema 构造消息类，
不再自研 wire format 解码：手写的长度前缀解析分不出 ``bytes`` / ``string`` / 嵌套消息，
只能靠预先声明「哪些字段号是消息」来补，内层解析失败还要吞掉异常退回原始 bytes。
也不入库 ``protoc`` 生成物——生成物把 schema 编译成不可读的序列化字节，评审与后续
维护都得先装工具链；显式写出字段号反而能逐条对照上游 ``.proto``。解出的消息一律转成
小 dataclass 再交给上层，上游的 ``PascalCase`` 字段名不外泄。

上游原始定义（MIT，Hi3Helper.Sophon 项目，署名见 :mod:`app.services.gi_updater`）::

    message SophonManifestProto { repeated SophonManifestAssetProperty Assets = 1; }
    message SophonManifestAssetProperty {
      string AssetName = 1; repeated SophonManifestAssetChunk AssetChunks = 2;
      int32 AssetType = 3; int64 AssetSize = 4; string AssetHashMd5 = 5;
    }
    message SophonManifestAssetChunk {
      string ChunkName = 1; string ChunkDecompressedHashMd5 = 2;
      int64 ChunkOnFileOffset = 3; int64 ChunkSize = 4; int64 ChunkSizeDecompressed = 5;
    }

    message SophonPatchProto {
      repeated SophonPatchAssetProperty PatchAssets = 1;
      repeated SophonUnusedAssetProperty UnusedAssets = 2;
    }
    message SophonPatchAssetProperty {
      string AssetName = 1; int64 AssetSize = 2; string AssetHashMd5 = 3;
      repeated SophonPatchAssetInfo AssetInfos = 4;
    }
    message SophonPatchAssetInfo { string VersionTag = 1; SophonPatchAssetChunk Chunk = 2; }
    message SophonPatchAssetChunk {
      string PatchName = 1; string VersionTag = 2; string BuildId = 3; int64 PatchSize = 4;
      string PatchMd5 = 5; int64 PatchOffset = 6; int64 PatchLength = 7;
      string OriginalFileName = 8; int64 OriginalFileLength = 9; string OriginalFileMd5 = 10;
    }
    message SophonUnusedAssetProperty { string VersionTag = 1; repeated SophonUnusedAssetInfo AssetInfos = 2; }
    message SophonUnusedAssetInfo { repeated SophonUnusedAssetFile Assets = 1; }
    message SophonUnusedAssetFile { string FileName = 1; int64 FileSize = 2; string FileMd5 = 3; }

zstd 侧统一走 ``zstandard`` 包（``pyproject.toml`` 里是硬依赖），不做多后端降级探测。

本模块是协议层，不含任何游戏知识。差分清单与补丁落盘见 :mod:`patch`。
"""

from __future__ import annotations

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, List, Optional, Sequence

import zstandard
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from app.services.gi_updater.api import HttpClient
from app.services.gi_updater.common import (
    ProgressBase,
    UpdateAborted,
    get_logger,
    safe_join,
    summarize_size,
)

__all__ = [
    "SophonManifestProto",
    "SophonAssetProperty",
    "SophonAssetChunk",
    "SophonPatchProto",
    "SophonPatchAssetProperty",
    "SophonPatchAssetInfo",
    "SophonPatchChunk",
    "SophonUnusedAssetProperty",
    "SophonUnusedAssetInfo",
    "SophonUnusedAssetFile",
    "parse_sophon_manifest",
    "parse_sophon_patch",
    "decompress",
    "iter_decompress",
    "ZstdError",
    "SophonManifestInfo",
    "SophonChunksInfo",
    "SophonChunkManifestInfoPair",
    "SophonChunk",
    "SophonAsset",
    "SophonManifest",
    "SophonDownloader",
    "SophonError",
]


_PROTO_PACKAGE = "Hi3Helper.Sophon.Protos"
_proto_pool = descriptor_pool.DescriptorPool()

_STRING = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
_INT32 = descriptor_pb2.FieldDescriptorProto.TYPE_INT32
_INT64 = descriptor_pb2.FieldDescriptorProto.TYPE_INT64
_MESSAGE = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
_SCALAR = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
_REPEATED = descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED

#: 一条字段定义：``(字段名, 字段号, 类型, label, 嵌套类型名)``；标量类型的嵌套名传空串
_FieldSpec = tuple[str, int, int, int, str]


def _add_message(
    file_proto: descriptor_pb2.FileDescriptorProto,
    name: str,
    fields: Sequence[_FieldSpec],
) -> None:
    """往文件描述符里加一个 message 定义。

    Args:
        file_proto: 目标文件描述符。
        name: message 名。
        fields: 字段定义序列，每项为 ``(字段名, 字段号, 类型, label, 嵌套类型名)``。
    """
    message = file_proto.message_type.add()
    message.name = name
    for field_name, number, field_type, label, type_name in fields:
        entry = message.field.add()
        entry.name = field_name
        entry.number = number
        entry.type = field_type
        entry.label = label
        if type_name:
            entry.type_name = f".{_PROTO_PACKAGE}.{type_name}"


def _build_proto_file(
    file_name: str,
    messages: Sequence[tuple[str, Sequence[_FieldSpec]]],
) -> None:
    """注册一个 proto 文件描述符。

    Args:
        file_name: 文件名（仅用于描述符内标识）。
        messages: ``(message 名, 字段序列)`` 序列。
    """
    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = file_name
    file_proto.package = _PROTO_PACKAGE
    file_proto.syntax = "proto3"
    for name, fields in messages:
        _add_message(file_proto, name, fields)
    _proto_pool.Add(file_proto)


def _message_class(name: str) -> type:
    """取已注册 message 的 Python 类。

    Args:
        name: message 名（不含包名）。

    Returns:
        可用于 ``ParseFromString`` 的消息类。
    """
    descriptor = _proto_pool.FindMessageTypeByName(f"{_PROTO_PACKAGE}.{name}")
    return message_factory.GetMessageClass(descriptor)


_build_proto_file(
    "SophonManifestProto.proto",
    [
        (
            "SophonManifestProto",
            [("Assets", 1, _MESSAGE, _REPEATED, "SophonManifestAssetProperty")],
        ),
        (
            "SophonManifestAssetProperty",
            [
                ("AssetName", 1, _STRING, _SCALAR, ""),
                ("AssetChunks", 2, _MESSAGE, _REPEATED, "SophonManifestAssetChunk"),
                ("AssetType", 3, _INT32, _SCALAR, ""),
                ("AssetSize", 4, _INT64, _SCALAR, ""),
                ("AssetHashMd5", 5, _STRING, _SCALAR, ""),
            ],
        ),
        (
            "SophonManifestAssetChunk",
            [
                ("ChunkName", 1, _STRING, _SCALAR, ""),
                ("ChunkDecompressedHashMd5", 2, _STRING, _SCALAR, ""),
                ("ChunkOnFileOffset", 3, _INT64, _SCALAR, ""),
                ("ChunkSize", 4, _INT64, _SCALAR, ""),
                ("ChunkSizeDecompressed", 5, _INT64, _SCALAR, ""),
            ],
        ),
    ],
)

_build_proto_file(
    "SophonPatchProto.proto",
    [
        (
            "SophonPatchProto",
            [
                ("PatchAssets", 1, _MESSAGE, _REPEATED, "SophonPatchAssetProperty"),
                ("UnusedAssets", 2, _MESSAGE, _REPEATED, "SophonUnusedAssetProperty"),
            ],
        ),
        (
            "SophonPatchAssetProperty",
            [
                ("AssetName", 1, _STRING, _SCALAR, ""),
                ("AssetSize", 2, _INT64, _SCALAR, ""),
                ("AssetHashMd5", 3, _STRING, _SCALAR, ""),
                ("AssetInfos", 4, _MESSAGE, _REPEATED, "SophonPatchAssetInfo"),
            ],
        ),
        (
            "SophonPatchAssetInfo",
            [
                ("VersionTag", 1, _STRING, _SCALAR, ""),
                ("Chunk", 2, _MESSAGE, _SCALAR, "SophonPatchAssetChunk"),
            ],
        ),
        (
            "SophonPatchAssetChunk",
            [
                ("PatchName", 1, _STRING, _SCALAR, ""),
                ("VersionTag", 2, _STRING, _SCALAR, ""),
                ("BuildId", 3, _STRING, _SCALAR, ""),
                ("PatchSize", 4, _INT64, _SCALAR, ""),
                ("PatchMd5", 5, _STRING, _SCALAR, ""),
                ("PatchOffset", 6, _INT64, _SCALAR, ""),
                ("PatchLength", 7, _INT64, _SCALAR, ""),
                ("OriginalFileName", 8, _STRING, _SCALAR, ""),
                ("OriginalFileLength", 9, _INT64, _SCALAR, ""),
                ("OriginalFileMd5", 10, _STRING, _SCALAR, ""),
            ],
        ),
        (
            "SophonUnusedAssetProperty",
            [
                ("VersionTag", 1, _STRING, _SCALAR, ""),
                ("AssetInfos", 2, _MESSAGE, _REPEATED, "SophonUnusedAssetInfo"),
            ],
        ),
        (
            "SophonUnusedAssetInfo",
            [("Assets", 1, _MESSAGE, _REPEATED, "SophonUnusedAssetFile")],
        ),
        (
            "SophonUnusedAssetFile",
            [
                ("FileName", 1, _STRING, _SCALAR, ""),
                ("FileSize", 2, _INT64, _SCALAR, ""),
                ("FileMd5", 3, _STRING, _SCALAR, ""),
            ],
        ),
    ],
)

_ManifestMessage = _message_class("SophonManifestProto")
_PatchMessage = _message_class("SophonPatchProto")


# --------------------------------------------------------------------------- #
# Sophon 清单（SophonManifestProto）
# --------------------------------------------------------------------------- #


@dataclass
class SophonAssetChunk:
    """清单里一个 asset 所引用的数据块。"""

    chunk_name: str = ""
    chunk_decompressed_hash_md5: str = ""
    chunk_on_file_offset: int = 0
    chunk_size: int = 0
    chunk_size_decompressed: int = 0


@dataclass
class SophonAssetProperty:
    """清单里的一个资源条目。"""

    asset_name: str = ""
    asset_type: int = 0
    asset_size: int = 0
    asset_hash_md5: str = ""
    asset_chunks: List[SophonAssetChunk] = field(default_factory=list)

    @property
    def is_directory(self) -> bool:
        """目录项的判定：``asset_type != 0``。"""
        return self.asset_type != 0


@dataclass
class SophonManifestProto:
    """解析后的 Sophon 主清单。"""

    assets: List[SophonAssetProperty] = field(default_factory=list)


def parse_sophon_manifest(data: bytes) -> SophonManifestProto:
    """解析 Sophon 清单 protobuf。

    Args:
        data: 原始清单字节（通常已 zstd 解压）。

    Returns:
        :class:`SophonManifestProto`，含 ``assets`` 列表。

    Raises:
        google.protobuf.message.DecodeError: 字节不符合清单 schema 时。
    """
    message = _ManifestMessage()
    message.ParseFromString(data)

    return SophonManifestProto(
        assets=[
            SophonAssetProperty(
                asset_name=asset.AssetName,
                asset_type=asset.AssetType,
                asset_size=asset.AssetSize,
                asset_hash_md5=asset.AssetHashMd5,
                asset_chunks=[
                    SophonAssetChunk(
                        chunk_name=chunk.ChunkName,
                        chunk_decompressed_hash_md5=chunk.ChunkDecompressedHashMd5,
                        chunk_on_file_offset=chunk.ChunkOnFileOffset,
                        chunk_size=chunk.ChunkSize,
                        chunk_size_decompressed=chunk.ChunkSizeDecompressed,
                    )
                    for chunk in asset.AssetChunks
                ],
            )
            for asset in message.Assets
        ]
    )


# --------------------------------------------------------------------------- #
# Sophon 差分（SophonPatchProto）
# --------------------------------------------------------------------------- #


@dataclass
class SophonPatchChunk:
    """差分档案里一条分片的坐标与基线信息。"""

    patch_name: str = ""
    version_tag: str = ""
    build_id: str = ""
    patch_size: int = 0
    patch_md5: str = ""
    patch_offset: int = 0
    patch_length: int = 0
    original_file_name: str = ""
    original_file_length: int = 0
    original_file_md5: str = ""


@dataclass
class SophonPatchAssetInfo:
    """某个可升级基线对应的差分信息。

    Note:
        上游把 ``Chunk`` 声明为单值，这里也按单值取：没有该字段时 ``chunks``
        为空列表，与上层 ``info.chunks[0] if info.chunks else None`` 的口径一致。
    """

    version_tag: str = ""
    chunks: List[SophonPatchChunk] = field(default_factory=list)


@dataclass
class SophonPatchAssetProperty:
    """差分清单里的一个 asset 条目。"""

    asset_name: str = ""
    asset_size: int = 0
    asset_hash_md5: str = ""
    asset_infos: List[SophonPatchAssetInfo] = field(default_factory=list)


@dataclass
class SophonUnusedAssetFile:
    """升级后不再被引用的一个旧文件。"""

    file_name: str = ""
    file_size: int = 0
    file_md5: str = ""


@dataclass
class SophonUnusedAssetInfo:
    """一批不再被引用的旧文件。"""

    assets: List[SophonUnusedAssetFile] = field(default_factory=list)


@dataclass
class SophonUnusedAssetProperty:
    """某个基线升上来之后变成无用的文件清单。

    Note:
        这里列的是「从某个基线升上来后就不再被引用」的旧文件，跨基线混在一起，
        因此不能直接照单全删——目标清单里仍然存在的同名文件必须留下。
    """

    version_tag: str = ""
    asset_infos: List[SophonUnusedAssetInfo] = field(default_factory=list)


@dataclass
class SophonPatchProto:
    """解析后的 Sophon 差分清单。"""

    patch_assets: List[SophonPatchAssetProperty] = field(default_factory=list)
    unused_assets: List[SophonUnusedAssetProperty] = field(default_factory=list)


def _to_patch_chunk(chunk: Any) -> SophonPatchChunk:
    """把上游的差分分片转成内部 dataclass。

    Args:
        chunk: ``SophonPatchAssetChunk`` 消息实例。

    Returns:
        对应的 :class:`SophonPatchChunk`。
    """
    return SophonPatchChunk(
        patch_name=chunk.PatchName,
        version_tag=chunk.VersionTag,
        build_id=chunk.BuildId,
        patch_size=chunk.PatchSize,
        patch_md5=chunk.PatchMd5,
        patch_offset=chunk.PatchOffset,
        patch_length=chunk.PatchLength,
        original_file_name=chunk.OriginalFileName,
        original_file_length=chunk.OriginalFileLength,
        original_file_md5=chunk.OriginalFileMd5,
    )


def parse_sophon_patch(data: bytes) -> SophonPatchProto:
    """解析 Sophon 差分清单 protobuf。

    Args:
        data: 原始差分清单字节（通常已 zstd 解压）。

    Returns:
        :class:`SophonPatchProto`，含 ``patch_assets`` 与 ``unused_assets``。

    Raises:
        google.protobuf.message.DecodeError: 字节不符合差分 schema 时。
    """
    message = _PatchMessage()
    message.ParseFromString(data)

    return SophonPatchProto(
        patch_assets=[
            SophonPatchAssetProperty(
                asset_name=asset.AssetName,
                asset_size=asset.AssetSize,
                asset_hash_md5=asset.AssetHashMd5,
                asset_infos=[
                    SophonPatchAssetInfo(
                        version_tag=info.VersionTag,
                        chunks=(
                            [_to_patch_chunk(info.Chunk)]
                            if info.HasField("Chunk")
                            else []
                        ),
                    )
                    for info in asset.AssetInfos
                ],
            )
            for asset in message.PatchAssets
        ],
        unused_assets=[
            SophonUnusedAssetProperty(
                version_tag=unused.VersionTag,
                asset_infos=[
                    SophonUnusedAssetInfo(
                        assets=[
                            SophonUnusedAssetFile(
                                file_name=item.FileName,
                                file_size=item.FileSize,
                                file_md5=item.FileMd5,
                            )
                            for item in info.Assets
                        ]
                    )
                    for info in unused.AssetInfos
                ],
            )
            for unused in message.UnusedAssets
        ],
    )


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


#: 主资源
MAIN_MATCHING_FIELD = "game"


class SophonError(RuntimeError):
    """Sophon 链路异常。"""


# --------------------------------------------------------------------------- #
# 结构定义
# --------------------------------------------------------------------------- #


@dataclass
class SophonManifestInfo:
    """清单的下载地址信息。"""

    manifest_base_url: str = ""
    manifest_id: str = ""
    manifest_checksum_md5: str = ""
    is_use_compression: bool = False
    manifest_size: int = 0
    manifest_compressed_size: int = 0
    matching_field: str = ""
    category_id: int = 0
    category_name: str = ""

    @property
    def manifest_file_url(self) -> str:
        """清单文件的完整下载地址。"""
        return f"{self.manifest_base_url.rstrip('/')}/{self.manifest_id}"


@dataclass
class SophonChunksInfo:
    """数据块的下载地址与数量信息。"""

    chunks_base_url: str = ""
    chunks_count: int = 0
    files_count: int = 0
    total_size: int = 0
    total_compressed_size: int = 0
    is_use_compression: bool = False
    matching_field: str = ""
    category_id: int = 0
    category_name: str = ""

    def chunk_url(self, chunk_name: str) -> str:
        """拼接单个 chunk 的下载地址。

        Args:
            chunk_name: chunk 文件名（清单里的 ``chunk_name``）。

        Returns:
            完整 URL 字符串。
        """
        return f"{self.chunks_base_url.rstrip('/')}/{chunk_name}"


@dataclass
class SophonChunkManifestInfoPair:
    """清单 + chunk 的信息对。"""

    manifest_info: Optional[SophonManifestInfo] = None
    chunks_info: Optional[SophonChunksInfo] = None
    matching_field: str = ""
    category_id: int = 0
    category_name: str = ""
    is_found: bool = False
    return_code: int = 0
    return_message: str = ""


@dataclass
class SophonChunk:
    """单个数据块及其校验信息。"""

    chunk_name: str = ""
    #: 解压后的 MD5（校验用）
    chunk_hash_decompressed: str = ""
    chunk_offset: int = 0
    chunk_size: int = 0
    chunk_size_decompressed: int = 0


@dataclass
class SophonAsset:
    """清单里的一个文件。"""

    asset_name: str = ""
    asset_size: int = 0
    asset_hash: str = ""
    is_directory: bool = False
    chunks: List[SophonChunk] = field(default_factory=list)
    chunks_info: Optional[SophonChunksInfo] = None
    matching_field: str = ""
    category_id: int = 0
    category_name: str = ""

    @property
    def chunk_count(self) -> int:
        """该 asset 包含的 chunk 数。

        Returns:
            ``chunks`` 列表长度。
        """
        return len(self.chunks)

    def __str__(self) -> str:  # pragma: no cover
        """人类可读表示：``资产名 (可读大小)``。"""
        return f"{self.asset_name} ({summarize_size(self.asset_size)})"


# --------------------------------------------------------------------------- #
# 清单获取
# --------------------------------------------------------------------------- #


def _identity_to_pair(identity: Any) -> SophonChunkManifestInfoPair:
    """把 ``SophonManifestBuildIdentity`` 转成信息对。

    Args:
        identity: 从 getBuild 响应解析出的某 matching_field 身份对象。

    Returns:
        填充好的 :class:`SophonChunkManifestInfoPair`（``is_found=True``）。
    """
    manifest_info = SophonManifestInfo(
        manifest_base_url=identity.manifest_download.url_prefix,
        manifest_id=identity.manifest.id,
        manifest_checksum_md5=identity.manifest.checksum,
        is_use_compression=identity.manifest_download.is_compressed,
        manifest_size=identity.manifest.uncompressed_size,
        manifest_compressed_size=identity.manifest.compressed_size,
        matching_field=identity.matching_field,
        category_id=identity.category_id,
        category_name=identity.category_name,
    )
    chunks_info = SophonChunksInfo(
        chunks_base_url=identity.chunk_download.url_prefix,
        chunks_count=identity.stats.chunk_count,
        files_count=identity.stats.file_count,
        total_size=identity.stats.uncompressed_size,
        total_compressed_size=identity.stats.compressed_size,
        is_use_compression=identity.chunk_download.is_compressed,
        matching_field=identity.matching_field,
        category_id=identity.category_id,
        category_name=identity.category_name,
    )
    return SophonChunkManifestInfoPair(
        manifest_info=manifest_info,
        chunks_info=chunks_info,
        matching_field=identity.matching_field,
        category_id=identity.category_id,
        category_name=identity.category_name,
        is_found=True,
    )


class SophonManifest:
    """静态方法的等价物。"""

    @staticmethod
    def create_info_pair(
        client: HttpClient,
        url: str,
        matching_field: str = MAIN_MATCHING_FIELD,
        *,
        throw_if_not_found: bool = True,
        method: str = "GET",
        logger: Any = None,
    ) -> SophonChunkManifestInfoPair:
        """拉 ``getBuild`` 并组装成清单信息对。

        ``getBuild`` 主清单用 GET、**差分（patch）用 POST**：
        两者 URL 完全相同，只有 HTTP 方法不同。

        Args:
            client: HTTP 客户端。
            url: getBuild 请求地址。
            matching_field: 清单类别；默认 ``game``（主资源）。
            throw_if_not_found: 未找到对应清单时是否抛异常。
            method: HTTP 方法，``"GET"``（主清单）或 ``"POST"``（差分）。
            logger: 可选日志器。

        Returns:
            定位到的信息对（``is_found`` 指示是否成功）。

        Raises:
            SophonError: ``throw_if_not_found=True`` 且对应 matching_field
                在响应中不存在时。
        """
        logger = logger or get_logger()
        payload = client.request(method, url).json()
        from app.services.gi_updater.api import parse_build

        build = parse_build(payload)

        if not build.manifests:
            return SophonChunkManifestInfoPair(
                matching_field=matching_field,
                is_found=False,
                return_code=build.retcode,
                return_message=build.message,
            )

        field_name = matching_field or MAIN_MATCHING_FIELD
        identity = build.find(field_name)
        if identity is None:
            if throw_if_not_found:
                raise SophonError(f"未找到 matching_field={field_name} 的 Sophon 清单")
            return SophonChunkManifestInfoPair(
                matching_field=field_name,
                is_found=False,
                return_code=404,
                return_message=f"Sophon manifest with matching field: {field_name} is not found!",
            )

        return _identity_to_pair(identity)

    @staticmethod
    def create_patch_info_pair(
        client: HttpClient,
        url: str,
        version_update_from: str,
        matching_field: str = MAIN_MATCHING_FIELD,
        *,
        logger: Any = None,
    ) -> SophonChunkManifestInfoPair:
        """拉差分 ``getPatchBuild`` 并组装成清单信息对。

        与 :meth:`create_info_pair` 的三点差异：

        1. 端点是 ``getPatchBuild``（不是 ``getBuild``），且只收 **POST**；
        2. 清单条目类型是 ``SophonManifestPatchIdentity``，
           chunk 信息来自 ``diff_download`` + ``stats``（重建为
           ``diff_tagged_info``，键是基线版本）；
        3. 拿不到 ``DiffTaggedInfo`` 就认为「没有可用的差分」→ ``is_found=False``。

        Args:
            client: HTTP 客户端。
            url: patch 分支 ``getPatchBuild`` 请求地址（用 POST）。
            version_update_from: 起始版本号，用于取 ``diff_tagged_info``。
            matching_field: 清单类别；默认 ``game``。
            logger: 可选日志器。

        Returns:
            定位到的信息对（``is_found`` 指示是否成功）。
        """
        logger = logger or get_logger()
        from app.services.gi_updater.api import parse_patch_build

        payload = client.request("POST", url).json()
        branch = parse_patch_build(payload)

        if not branch.manifests:
            return SophonChunkManifestInfoPair(
                matching_field=matching_field,
                is_found=False,
                return_code=branch.retcode,
                return_message=branch.message,
            )

        field_name = matching_field or MAIN_MATCHING_FIELD
        identity = branch.find(field_name)
        if identity is None:
            return SophonChunkManifestInfoPair(
                matching_field=field_name,
                is_found=False,
                return_code=404,
                return_message=f"Sophon patch with matching field: {field_name} is not found!",
            )

        stats = identity.diff_tagged_info.get(version_update_from)
        if stats is None:
            return SophonChunkManifestInfoPair(
                matching_field=field_name,
                is_found=False,
                return_code=404,
                return_message=(
                    "Sophon patch diff tagged info with version: "
                    f"{version_update_from} is not found!"
                ),
            )

        manifest_info = SophonManifestInfo(
            manifest_base_url=identity.manifest_download.url_prefix,
            manifest_id=identity.manifest.id,
            manifest_checksum_md5=identity.manifest.checksum,
            is_use_compression=identity.manifest_download.is_compressed,
            manifest_size=identity.manifest.uncompressed_size,
            manifest_compressed_size=identity.manifest.compressed_size,
            matching_field=identity.matching_field,
            category_id=identity.category_id,
            category_name=identity.category_name,
        )
        chunks_info = SophonChunksInfo(
            chunks_base_url=identity.diff_download.url_prefix,
            chunks_count=stats.chunk_count,
            files_count=stats.file_count,
            total_size=stats.uncompressed_size,
            total_compressed_size=stats.compressed_size,
            is_use_compression=identity.diff_download.is_compressed,
            matching_field=identity.matching_field,
            category_id=identity.category_id,
            category_name=identity.category_name,
        )
        return SophonChunkManifestInfoPair(
            manifest_info=manifest_info,
            chunks_info=chunks_info,
            matching_field=identity.matching_field,
            category_id=identity.category_id,
            category_name=identity.category_name,
            is_found=True,
        )

    @staticmethod
    def enumerate_assets(
        client: HttpClient,
        pair: SophonChunkManifestInfoPair,
        *,
        logger: Any = None,
    ) -> List[SophonAsset]:
        """把清单枚举成资源列表。

        下载清单 → 按需 zstd 解压 → protobuf 解析 → ``SophonAsset`` 列表。

        Args:
            client: HTTP 客户端。
            pair: 清单/分片信息对。
            logger: 可选日志器。

        Returns:
            ``SophonAsset`` 列表；``manifest_info`` / ``chunks_info`` 缺失时返回空列表。

        Note:
            清单 MD5 不一致只告警、不中断（仍继续解析）。
        """
        logger = logger or get_logger()
        if pair.manifest_info is None or pair.chunks_info is None:
            return []

        url = pair.manifest_info.manifest_file_url
        logger.debug(
            "下载 Sophon 清单：%s（matching_field=%s）", url, pair.matching_field
        )

        response = client.get(url, stream=True)
        try:
            if pair.manifest_info.is_use_compression:
                raw = b"".join(
                    iter_decompress(response._stream or _BytesStream(response.content))
                )
            else:
                raw = response.content
        finally:
            response.close()

        if pair.manifest_info.manifest_checksum_md5:
            actual = hashlib.md5(raw).hexdigest()
            if actual.lower() != pair.manifest_info.manifest_checksum_md5.lower():
                logger.warning(
                    "清单 MD5 不一致（期望 %s，实际 %s），仍继续解析",
                    pair.manifest_info.manifest_checksum_md5,
                    actual,
                )

        return _assets_from_proto(raw, pair)


class _BytesStream:
    """把 bytes 包装成有 ``read()`` 的流（供 iter_decompress 使用）。"""

    def __init__(self, data: bytes) -> None:
        """用一段 bytes 构造最小只读流适配器。

        Args:
            data: 待被读取的字节（供 ``iter_decompress`` 使用）。
        """
        self._data = data
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        """从缓冲读取至多 ``size`` 字节（``-1``/``None`` 表示读完剩余）。

        Args:
            size: 最多读取的字节数；负数或 ``None`` 时读到末尾。

        Returns:
            读到的字节；已到末尾返回空 ``bytes``。
        """
        if size is None or size < 0:
            chunk = self._data[self._offset :]
            self._offset = len(self._data)
            return chunk
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


def _assets_from_proto(
    raw: bytes, pair: SophonChunkManifestInfoPair
) -> List[SophonAsset]:
    """protobuf -> ``SophonAsset``。

    Args:
        raw: 已解压的清单 protobuf 字节。
        pair: 清单/分片信息对（为资产附带 ``chunks_info`` 等）。

    Returns:
        ``SophonAsset`` 列表；目录条目以 ``is_directory=True`` 表示。
    """
    proto = parse_sophon_manifest(raw)
    assets: List[SophonAsset] = []

    for item in proto.assets:
        if item.is_directory or not item.asset_hash_md5:
            # 目录条目：只登记名字，不写文件内容
            assets.append(
                SophonAsset(
                    asset_name=item.asset_name,
                    is_directory=True,
                    chunks_info=pair.chunks_info,
                    matching_field=pair.matching_field,
                    category_id=pair.category_id,
                    category_name=pair.category_name,
                )
            )
            continue

        assets.append(
            SophonAsset(
                asset_name=item.asset_name,
                asset_size=item.asset_size,
                asset_hash=item.asset_hash_md5,
                is_directory=False,
                chunks=[
                    SophonChunk(
                        chunk_name=chunk.chunk_name,
                        chunk_hash_decompressed=chunk.chunk_decompressed_hash_md5,
                        chunk_offset=chunk.chunk_on_file_offset,
                        chunk_size=chunk.chunk_size,
                        chunk_size_decompressed=chunk.chunk_size_decompressed,
                    )
                    for chunk in item.asset_chunks
                ],
                chunks_info=pair.chunks_info,
                matching_field=pair.matching_field,
                category_id=pair.category_id,
                category_name=pair.category_name,
            )
        )

    return assets


# --------------------------------------------------------------------------- #
# 下载器
# --------------------------------------------------------------------------- #


@dataclass
class SophonDownloader:
    """把 Sophon 清单里的 asset 落到磁盘。"""

    client: HttpClient
    #: chunk 级并发（调用方传入，钳制在 1–32）
    chunk_threads: int = 8
    progress: Optional[ProgressBase] = None
    logger: Any = None
    #: 协作式中止判定：在资产与数据块边界轮询，返回真即抛 ``UpdateAborted``
    should_abort: Optional[Callable[[], bool]] = None

    def __post_init__(self) -> None:
        """补默认日志器（未显式传入时）。"""
        if self.logger is None:
            self.logger = get_logger()

    def _check_abort(self) -> None:
        """在安全边界检查是否该收工。

        Raises:
            UpdateAborted: ``should_abort`` 返回真时。
        """
        if self.should_abort is not None and self.should_abort():
            raise UpdateAborted("更新已中止")

    # ---------------------------------------------------------------- 单文件

    def download_asset(
        self,
        asset: SophonAsset,
        game_path: str,
        *,
        verify: bool = True,
    ) -> bool:
        """下载并组装单个 asset。

        每个 chunk 按 ``ChunkOnFileOffset`` 写进同一个目标文件流。

        Args:
            asset: 目标资产（含 chunk 列表与 ``asset_name``）。
            game_path: 游戏根目录（资产名相对其解析）。
            verify: 写入后是否整体校验 MD5。

        Returns:
            ``True`` 表示成功（或已完整被复用）；目录/无 chunk 资产或
            校验失败返回 ``False``（不抛异常）。
        """
        if asset.is_directory or asset.chunks_info is None:
            return False

        target = _resolve_target_path(game_path, asset.asset_name)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)

        if self._is_asset_complete(target, asset):
            self.logger.debug("跳过已完整文件：%s", asset.asset_name)
            if self.progress:
                self.progress.advance(asset.asset_size)
            return True

        # 先把目标文件预分配到最终大小
        with open(target, "a+b") as handle:
            if handle.tell() < asset.asset_size:
                handle.truncate(asset.asset_size)

        if self.progress:
            self.progress.set_current_file(asset.asset_name, asset.asset_size)

        self._run_chunks(asset, target)

        if verify and not self._is_asset_complete(target, asset):
            self.logger.error("文件校验失败：%s", target)
            return False
        return True

    def _run_chunks(self, asset: SophonAsset, target: str) -> None:
        """下载各 chunk 并按 ``chunk_offset`` 并发写入目标文件。

        Args:
            asset: 目标资产（chunk 列表）。
            target: 本地目标文件路径。

        Note:
            用线程池并发；每个 worker 下载一个 chunk 后 ``seek(offset)``
            写入同一文件。
        """
        chunks_info = asset.chunks_info
        assert chunks_info is not None

        def worker(chunk: SophonChunk) -> None:
            self._check_abort()
            data = self._fetch_chunk(chunks_info, chunk)
            with open(target, "r+b") as handle:
                handle.seek(chunk.chunk_offset)
                handle.write(data)
            if self.progress:
                self.progress.advance(len(data))

        with ThreadPoolExecutor(max_workers=max(1, self.chunk_threads)) as pool:
            futures = [pool.submit(worker, chunk) for chunk in asset.chunks]
            for future in as_completed(futures):
                future.result()

    def _fetch_chunk(self, chunks_info: SophonChunksInfo, chunk: SophonChunk) -> bytes:
        """下载并（按需）解压一个 chunk，同时校验解压后 MD5。

        Args:
            chunks_info: chunk 所属清单分组，提供 ``chunk_download.url_prefix``。
            chunk: 目标 chunk 的元数据（名称、压缩后长度、解压后 MD5 等）。

        Returns:
            解压后的 chunk 原始字节。

        Raises:
            SophonError: 压缩后长度不符、解压失败或 MD5 与清单不一致时。
        """
        url = chunks_info.chunk_url(chunk.chunk_name)
        response = self.client.get(url, stream=True)
        try:
            if chunks_info.is_use_compression:
                data = b"".join(
                    iter_decompress(response._stream or _BytesStream(response.content))
                )
            else:
                data = response.content
        finally:
            response.close()

        if chunk.chunk_hash_decompressed:
            actual = hashlib.md5(data).hexdigest()
            if actual.lower() != chunk.chunk_hash_decompressed.lower():
                raise SophonError(
                    f"chunk {chunk.chunk_name} MD5 校验失败"
                    f"（期望 {chunk.chunk_hash_decompressed}，实际 {actual}）"
                )
        expected = chunk.chunk_size_decompressed or chunk.chunk_size
        if expected and len(data) != expected:
            raise SophonError(
                f"chunk {chunk.chunk_name} 长度不符（期望 {expected}，实际 {len(data)}）"
            )
        return data

    # ---------------------------------------------------------------- 校验

    def _is_asset_complete(self, target: str, asset: SophonAsset) -> bool:
        """校验目标文件大小与 MD5 是否与资产一致。

        Args:
            target: 本地目标文件路径。
            asset: 资产元数据（提供期望大小与 MD5）。

        Returns:
            ``True`` 表示大小一致且 MD5 匹配（无 ``asset_hash`` 时仅看大小）；
            文件不存在或不符返回 ``False``。
        """
        if not os.path.isfile(target):
            return False
        if os.path.getsize(target) != asset.asset_size:
            return False
        if not asset.asset_hash:
            return True
        digest = hashlib.md5()
        with open(target, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest().lower() == asset.asset_hash.lower()


def _resolve_target_path(game_path: str, asset_name: str) -> str:
    """把清单里的资产路径解析为本地绝对路径，越界即拒绝。

    清单路径以 ``\\``（或 ``/``）分隔且相对游戏根目录。清单是服务端下发的
    不可信输入，这里走 :func:`common.paths.safe_join` 做防穿越校验——
    ``..``、盘符、UNC 与解析后越出根目录的路径一律拒绝，与 Collapse 上游
    的路径安全行为对齐。

    Args:
        game_path: 游戏根目录。
        asset_name: 清单里的资产名（含相对路径）。

    Returns:
        拼接后的本地绝对路径。

    Raises:
        UnsafePathError: 路径形态非法或越出游戏根目录时。
    """
    return safe_join(game_path, asset_name)
