#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team
#
#   This file is part of AUTO-MAS.
#
#   AUTO-MAS is free software: you can redistribute it and/or modify
#   it under the terms of the GNU Affero General Public License as
#   published by the Free Software Foundation, either version 3 of
#   the License, or (at your option) any later version.
#
#   AUTO-MAS is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
#   Affero General Public License for more details.
#
#   You should have received a copy of the GNU Affero General Public License
#   along with AUTO-MAS. If not, see <https://www.gnu.org/licenses/>.
#
#   第三方来源声明：本模块按上游公开源码所描述的流程与协议用 Python 重新实现，
#   不包含其源文件副本。Sophon 协议定义与流程来自下列 MIT 项目（许可均随上游仓库
#   发行，本目录不另附原文）：
#
#   - CollapseLauncher/Collapse        https://github.com/CollapseLauncher/Collapse
#   - Hi3Helper.Sophon                 https://github.com/CollapseLauncher/Hi3Helper.Sophon

"""由 MAS 自行完成原神 PC 客户端的启动前更新（Sophon 链路，官服 / 国际服）。

设计取舍只在一点：**只做增量**。原神自 5.6 起官方不再下发 zip 分包，更新只能走
Sophon；而「自己下载并打补丁」这个方案里，真正贵的是差分包本身。因此本模块：

- **只应用官方 ldiff 差分包**：向 ``getPatchBuild`` 取差分清单，按本机已装基线挑分片，
  分片带 ``original_file_name`` 的用 hpatchz 打到旧文件上（Patch），不带的说明分片
  本身就是新内容（CopyOver）。**差分清单没点名的文件不动**——把那些也拿来整文件
  重下，会把一次约 10 GB 的增量变成上百 GB 的全量下载。
- **拿不到差分就停手**：全新安装、逐文件全量比对（无可用差分）、预下载都不自动执行，
  由调用方明确指出并建议改用官方启动器。无人值守的任务不该顺手吃掉几十 GB。

调用方（BetterGI 专项）负责读配置、判定渠道、推调度台与决定是否阻断任务；本模块只
负责「算计划」与「执行计划」，不读任何上游脚本的私有配置。

只实现原神（``hk4e``）两个区服。B 服是独立渠道、版本节奏不同，由调用方在渠道识别
阶段拒绝，本模块不做区分。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
import zstandard
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.message import DecodeError

from app.utils import get_logger
from app.utils.io import atomic_write

logger = get_logger("原神更新")

#: 主资源清单的匹配字段（语音包是各语言码，本模块不处理）
MAIN_MATCHING_FIELD = "game"

#: 本地可执行文件的最小体积；小于此值认为不是真客户端
MIN_EXECUTABLE_SIZE = 1 << 16


# =====================================================================================
# 区服预设
# =====================================================================================


@dataclass(frozen=True)
class RegionPreset:
    """一个区服的接口坐标。

    ``info_base`` 出游戏/分支元数据，``downloader_base`` 出清单与分片；
    ``game_id`` 与 ``plat_app`` 是官方接口要求的业务标识。
    """

    key: str
    label: str
    info_base: str
    downloader_base: str
    launcher_id: str
    plat_app: str
    game_id: str

    @property
    def get_build_url(self) -> str:
        """主清单端点（只收 GET）。"""
        return f"{self.downloader_base}/downloader/sophon_chunk/api/getBuild"

    @property
    def get_patch_build_url(self) -> str:
        """差分清单端点（只收 POST，与主清单端点不通用）。"""
        return f"{self.downloader_base}/downloader/sophon_chunk/api/getPatchBuild"

    @property
    def branches_url(self) -> str:
        """Sophon 分支元数据（``package_id`` / ``password`` / ``tag`` 的来源）。"""
        return (
            f"{self.info_base}/getGameBranches"
            f"?launcher_id={self.launcher_id}&game_ids[]={self.game_id}"
        )


_REGION_PRESETS: dict[str, RegionPreset] = {
    "cn": RegionPreset(
        key="cn",
        label="官服",
        info_base="https://hyp-api.mihoyo.com/hyp/hyp-connect/api",
        downloader_base="https://api-takumi.mihoyo.com",
        launcher_id="jGHBHlcOq1",
        plat_app="ddxf5qt290cg",
        game_id="1Z8W5NHUQb",
    ),
    "global": RegionPreset(
        key="global",
        label="国际服",
        info_base="https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api",
        downloader_base="https://sg-public-api.hoyoverse.com",
        launcher_id="VYTpXlbWo8",
        plat_app="ddxf6vlr1reo",
        game_id="gopR6Cufr3",
    ),
}


def get_region_preset(region: str) -> RegionPreset:
    """按短名取区服预设。

    Args:
        region: ``"cn"``（官服）或 ``"global"``（国际服）。

    Returns:
        对应的 :class:`RegionPreset`。

    Raises:
        ValueError: 短名不认识时。
    """
    preset = _REGION_PRESETS.get(region)
    if preset is None:
        raise ValueError(f"未知区服: {region!r}（可用: {', '.join(_REGION_PRESETS)}）")
    return preset


# =====================================================================================
# Sophon 协议的 protobuf 定义
# =====================================================================================
#
# 与上游 Hi3Helper.Sophon 的 ``SophonManifestProto.proto`` / ``SophonPatchProto.proto``
# 逐字段一致。这里用 protobuf 官方运行时按 schema 构造消息类，而不是入库 protoc 生成物：
# 生成物把 schema 编译成不可读的序列化字节，评审与后续维护都得先装工具链；
# 显式写出字段号反而能逐条对照上游 ``.proto``。
#
# 上游原始定义（MIT，Hi3Helper.Sophon 项目，见上方第三方来源声明）：
#
#   message SophonManifestProto { repeated SophonManifestAssetProperty Assets = 1; }
#   message SophonManifestAssetProperty {
#     string AssetName = 1; repeated SophonManifestAssetChunk AssetChunks = 2;
#     int32 AssetType = 3; int64 AssetSize = 4; string AssetHashMd5 = 5;
#   }
#   message SophonManifestAssetChunk {
#     string ChunkName = 1; string ChunkDecompressedHashMd5 = 2;
#     int64 ChunkOnFileOffset = 3; int64 ChunkSize = 4; int64 ChunkSizeDecompressed = 5;
#   }
#
#   message SophonPatchProto {
#     repeated SophonPatchAssetProperty PatchAssets = 1;
#     repeated SophonUnusedAssetProperty UnusedAssets = 2;
#   }
#   message SophonPatchAssetProperty {
#     string AssetName = 1; int64 AssetSize = 2; string AssetHashMd5 = 3;
#     repeated SophonPatchAssetInfo AssetInfos = 4;
#   }
#   message SophonPatchAssetInfo { string VersionTag = 1; SophonPatchAssetChunk Chunk = 2; }
#   message SophonPatchAssetChunk {
#     string PatchName = 1; string VersionTag = 2; string BuildId = 3; int64 PatchSize = 4;
#     string PatchMd5 = 5; int64 PatchOffset = 6; int64 PatchLength = 7;
#     string OriginalFileName = 8; int64 OriginalFileLength = 9; string OriginalFileMd5 = 10;
#   }
#   message SophonUnusedAssetProperty { string VersionTag = 1; repeated SophonUnusedAssetInfo AssetInfos = 2; }
#   message SophonUnusedAssetInfo { repeated SophonUnusedAssetFile Assets = 1; }
#   message SophonUnusedAssetFile { string FileName = 1; int64 FileSize = 2; string FileMd5 = 3; }

_PROTO_PACKAGE = "Hi3Helper.Sophon.Protos"
_proto_pool = descriptor_pool.DescriptorPool()

_STRING = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
_INT32 = descriptor_pb2.FieldDescriptorProto.TYPE_INT32
_INT64 = descriptor_pb2.FieldDescriptorProto.TYPE_INT64
_MESSAGE = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
_SCALAR = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
_REPEATED = descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED


def _add_message(
    file_proto: descriptor_pb2.FileDescriptorProto,
    name: str,
    fields: Sequence[tuple[str, int, int, int, str]],
) -> None:
    """往文件描述符里加一个 message 定义。

    Args:
        file_proto: 目标文件描述符。
        name: message 名。
        fields: ``(字段名, 字段号, 类型, label, 嵌套类型名)`` 序列；标量类型的嵌套名传空串。
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
    messages: Sequence[tuple[str, Sequence[tuple[str, int, int, int, str]]]],
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


def _message_class(name: str) -> Any:
    """取已注册 message 的 Python 类。

    Args:
        name: message 名（不含包名）。

    Returns:
        可用于 ``ParseFromString`` 的消息类。
    """
    descriptor = _proto_pool.FindMessageTypeByName(f"{_PROTO_PACKAGE}.{name}")
    resolver = getattr(message_factory, "GetMessageClass", None)
    if resolver is not None:
        return resolver(descriptor)
    # protobuf < 4.22 没有 GetMessageClass
    return message_factory.MessageFactory(_proto_pool).GetPrototype(descriptor)


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

_SophonManifestProto = _message_class("SophonManifestProto")
_SophonPatchProto = _message_class("SophonPatchProto")


# =====================================================================================
# 结果模型
# =====================================================================================


class InstallState(str, Enum):
    """本机客户端的安装态。"""

    #: 目录里找不到体积达标的可执行文件
    NOT_INSTALLED = "not_installed"
    #: 有客户端但版本落后
    NEEDS_UPDATE = "needs_update"
    #: 已是最新
    UP_TO_DATE = "up_to_date"
    #: 读不出本地版本（config.ini 缺失或没有版本号）
    UNKNOWN = "unknown"


class UpdateKind(str, Enum):
    """本次更新的种类；决定调取方放行还是停手。"""

    #: 无需更新
    NOOP = "noop"
    #: 增量：官方下发可用差分，按 Patch / CopyOver 落盘
    PATCH = "patch"
    #: 非增量：无可用差分，官方启动器会走逐文件全量比对
    FULL_COMPARE = "full_compare"
    #: 非增量：本机没有客户端，等同全新安装
    NOT_INSTALLED = "not_installed"
    #: 官方已开放预下载（本模块不接管）
    PRELOAD = "preload"
    #: 无法判定（接口不可用、读不出本地版本等）
    UNKNOWN = "unknown"


#: 面向用户的种类说法
KIND_LABELS: dict[UpdateKind, str] = {
    UpdateKind.NOOP: "无需更新",
    UpdateKind.PATCH: "增量更新",
    UpdateKind.FULL_COMPARE: "逐文件全量比对",
    UpdateKind.NOT_INSTALLED: "全新安装",
    UpdateKind.PRELOAD: "预下载下一版本",
    UpdateKind.UNKNOWN: "无法判定",
}

#: 增量之外的种类都要求调用方停手并明确告知用户
INCREMENTAL_KINDS = frozenset({UpdateKind.PATCH, UpdateKind.NOOP})


@dataclass(frozen=True)
class PackageInfo:
    """``getGameBranches`` 里一个分支的包信息。"""

    package_id: str
    password: str
    tag: str


@dataclass(frozen=True)
class ManifestRef:
    """一份清单在网上的位置与形态。"""

    matching_field: str
    manifest_id: str
    manifest_url_prefix: str
    manifest_compressed: bool
    chunk_url_prefix: str
    chunk_compressed: bool


#: 分片本身就是新内容，直接落盘
METHOD_COPYOVER = "copyover"

#: 分片是 ldiff 数据，要用 hpatchz 打到旧文件上
METHOD_PATCH = "patch"


@dataclass(frozen=True)
class PatchAsset:
    """一个待更新文件的差分处理信息。"""

    name: str
    target_size: int
    target_md5: str
    method: str
    #: 差分分片所在的 blob 文件名；多个文件共用同一 blob
    patch_name: str
    #: 本文件那一段在 blob 里的位置，配合 Range 取回
    patch_offset: int
    patch_length: int
    #: 打补丁前的旧文件相对路径；CopyOver 时为空
    original_name: str = ""
    #: 旧文件应有的 MD5；打补丁前校验，基线不符就停手
    original_md5: str = ""


@dataclass(frozen=True)
class PatchSummary:
    """一次增量更新的体量统计。"""

    file_count: int
    download_size: int
    patch_count: int
    copyover_count: int
    removals: tuple[str, ...] = ()
    #: 单个目标文件的最大体积，用于估算磁盘峰值
    largest_target: int = 0


@dataclass(frozen=True)
class GenshinPlan:
    """「算计划」的结论，不含任何落盘动作。"""

    region: str
    game_dir: Path
    state: InstallState
    kind: UpdateKind
    local_version: str
    remote_version: str
    patch: PatchSummary | None = None
    message: str = ""
    #: 本次要处理的文件明细；``kind`` 不是 ``PATCH`` 时为空
    assets: tuple[PatchAsset, ...] = ()
    #: 差分分片的基址
    diff_url_prefix: str = ""
    #: 差分分片是否 zstd 压缩（跟随接口各自的来源标志）
    diff_compressed: bool = False

    @property
    def is_incremental(self) -> bool:
        """本次是否为增量（可自动执行）。"""
        return self.kind in INCREMENTAL_KINDS

    @property
    def needs_action(self) -> bool:
        """本次是否真的需要改动磁盘。"""
        return self.kind not in (UpdateKind.NOOP, UpdateKind.UNKNOWN)

    def describe(self) -> str:
        """一行面向用户的概述。"""
        if self.kind is UpdateKind.PATCH and self.patch is not None:
            return (
                f"{KIND_LABELS[self.kind]} · {self.local_version or '?'} -> "
                f"{self.remote_version or '?'} · {self.patch.file_count} 个文件 · "
                f"待下载 {summarize_size(self.patch.download_size)}"
            )
        if self.kind is UpdateKind.NOOP:
            return f"{KIND_LABELS[self.kind]}（{self.local_version or '?'}）"
        return (
            f"{KIND_LABELS[self.kind]} · {self.local_version or '?'} -> "
            f"{self.remote_version or '?'}"
        )


def summarize_size(size: int) -> str:
    """把字节数压成便于阅读的短串。

    Args:
        size: 字节数。

    Returns:
        形如 ``12.3 MiB`` 的串。
    """
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


# =====================================================================================
# 本地状态
# =====================================================================================


def read_local_version(game_dir: Path) -> str:
    """从 ``config.ini`` 的 ``[General]`` 读出本机游戏版本。

    原神的 ``config.ini`` 由官方启动器维护，键值属上游私有格式；这里只做透传读取，
    不建平行模型。行解析刻意不用 ``configparser``：该文件包含重复键与非常规写法，
    标准解析器会直接抛错。

    Args:
        game_dir: 游戏安装目录。

    Returns:
        版本串（如 ``7.0.0``）；读不到返回空串。
    """
    ini_path = game_dir / "config.ini"
    try:
        # utf-8-sig：官方写入不带 BOM，但用户手改过的文件可能带，一并容忍
        text = ini_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as error:
        logger.debug("读取 config.ini 失败: {} - {}", ini_path, error)
        return ""

    in_general = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_general = line[1:-1].strip().casefold() == "general"
            continue
        if not in_general:
            continue
        key, sep, value = line.partition("=")
        if sep and key.strip().casefold() == "game_version":
            return value.strip()
    return ""


def has_installed_executable(game_dir: Path, executable_names: Iterable[str]) -> bool:
    """目录里是否存在体积达标的客户端可执行文件。

    Args:
        game_dir: 游戏安装目录。
        executable_names: 候选可执行文件名。

    Returns:
        存在且体积超过 :data:`MIN_EXECUTABLE_SIZE` 时为真。
    """
    for name in executable_names:
        candidate = game_dir / name
        try:
            if candidate.is_file() and candidate.stat().st_size > MIN_EXECUTABLE_SIZE:
                return True
        except OSError:
            continue
    return False


# =====================================================================================
# 接口访问
# =====================================================================================


class UpdaterError(RuntimeError):
    """协议层失败；调用方据此按「无法判定」放行。"""


async def _request(
    client: httpx.AsyncClient, url: str, *, method: str = "GET"
) -> dict[str, Any]:
    """发一次请求并检查官方响应包封。

    Args:
        client: 复用的 HTTP 客户端。
        url: 完整地址。
        method: ``"GET"`` 或 ``"POST"``。

    Returns:
        解析后的 JSON 对象。

    Raises:
        UpdaterError: 网络失败、非 200 或 ``retcode`` 非 0 时。
    """
    try:
        response = await client.request(method, url)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as error:
        raise UpdaterError(f"请求失败 {method} {url}: {error}") from error
    except ValueError as error:
        raise UpdaterError(f"响应不是合法 JSON: {url}") from error

    if not isinstance(payload, dict):
        # 响应包封按对象解析；数组/标量一律视作协议异常
        raise UpdaterError(f"响应不是 JSON 对象: {url}")
    retcode = payload.get("retcode")
    if retcode not in (0, None):
        raise UpdaterError(
            f"接口返回 retcode={retcode} message={payload.get('message')!r}: {url}"
        )
    return payload


async def _request_bytes(
    client: httpx.AsyncClient, url: str, *, timeout: float | None = None
) -> bytes:
    """下载一份清单原始字节。

    Args:
        client: 复用的 HTTP 客户端。
        url: 清单地址。
        timeout: 覆盖 client 默认超时；清单体积大，用更宽的限。

    Returns:
        原始字节。

    Raises:
        UpdaterError: 网络失败或非 200 时。
    """
    try:
        response = await client.get(url, timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise UpdaterError(f"下载清单失败 {url}: {error}") from error
    return response.content


def _decompress(data: bytes, *, compressed: bool) -> bytes:
    """按接口声明的压缩标志解压清单/分片。

    Args:
        data: 原始字节。
        compressed: 是否 zstd 压缩。

    Returns:
        解压后的字节；``compressed`` 为假时原样返回。
    """
    if not compressed:
        return data
    try:
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=0)
    except zstandard.ZstdError as error:
        # ZstdError 不在 UpdaterError 链上，不接住会从 plan_update 逃逸成任务崩溃
        raise UpdaterError(f"清单解压失败: {error}") from error


def _package_info(branch: dict[str, Any], preset: RegionPreset) -> PackageInfo:
    """把分支条目转成 :class:`PackageInfo`。

    ``getGameBranches`` 的 ``main`` / ``pre_download`` 条目本身就带
    ``package_id`` / ``password`` / ``tag``，无需再绕一层资源包。

    Args:
        branch: ``main`` 或 ``pre_download`` 条目。
        preset: 目标区服（仅用于报错文案）。

    Returns:
        包信息。

    Raises:
        UpdaterError: 缺少 ``package_id`` 或 ``password`` 时。
    """
    package_id = str(branch.get("package_id") or "")
    password = str(branch.get("password") or "")
    if not package_id or not password:
        raise UpdaterError(
            f"{preset.label}：分支条目缺少 package_id/password: {branch!r}"
        )
    return PackageInfo(
        package_id=package_id,
        password=password,
        tag=str(branch.get("tag") or ""),
    )


def _data_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    """取响应里的 ``data`` 映射。

    接口包封里 ``data`` 必须是对象（或缺失）；是数组/标量说明协议变了，
    直接按异常处理，不让它变成下游的 AttributeError。

    Args:
        payload: 已校验为对象的响应包封。

    Returns:
        ``data`` 映射；缺失时为空字典。

    Raises:
        UpdaterError: ``data`` 存在但不是对象时。
    """
    data = payload.get("data")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise UpdaterError(f"响应 data 不是对象: {type(data).__name__}")
    return data


async def fetch_branches(
    client: httpx.AsyncClient, preset: RegionPreset
) -> tuple[PackageInfo, PackageInfo | None]:
    """取主分支与预下载分支的包信息。

    Args:
        client: 复用的 HTTP 客户端。
        preset: 目标区服。

    Returns:
        ``(主分支包信息, 预下载分支包信息或 None)``。

    Raises:
        UpdaterError: 接口异常或缺少主分支时。
    """
    payload = await _request(client, preset.branches_url)
    entries = _data_mapping(payload).get("game_branches") or []
    if not entries:
        raise UpdaterError(f"{preset.label}：getGameBranches 没有返回分支")
    entry = entries[0]
    if not isinstance(entry, dict):
        raise UpdaterError(f"{preset.label}：分支条目不是对象: {entry!r}")
    main = entry.get("main")
    if not isinstance(main, dict):
        raise UpdaterError(f"{preset.label}：分支里没有 main")
    preload = entry.get("pre_download")
    return (
        _package_info(main, preset),
        _package_info(preload, preset) if isinstance(preload, dict) else None,
    )


def _manifest_ref(identity: dict[str, Any], *, from_diff: bool) -> ManifestRef:
    """把一个清单条目转成 :class:`ManifestRef`。

    Args:
        identity: 清单条目（``manifests[]`` 的元素）。
        from_diff: 是否来自差分端点；差分端点的分片基址取 ``diff_download``。

    Returns:
        清单位置与形态。

    Raises:
        UpdaterError: 缺少必要的地址或 id 时。
    """
    manifest = identity.get("manifest") or {}
    manifest_download = identity.get("manifest_download") or {}
    chunk_source = (
        identity.get("diff_download") if from_diff else identity.get("chunk_download")
    ) or {}
    manifest_id = str(manifest.get("id") or "")
    manifest_prefix = str(manifest_download.get("url_prefix") or "")
    chunk_prefix = str(chunk_source.get("url_prefix") or "")
    if not manifest_id or not manifest_prefix or not chunk_prefix:
        raise UpdaterError(f"清单条目缺少地址或 id: {identity!r}")
    return ManifestRef(
        matching_field=str(identity.get("matching_field") or ""),
        manifest_id=manifest_id,
        manifest_url_prefix=manifest_prefix,
        manifest_compressed=bool(manifest_download.get("compression")),
        chunk_url_prefix=chunk_prefix,
        chunk_compressed=bool(chunk_source.get("compression")),
    )


def _find_manifest_entry(
    payload: dict[str, Any], matching_field: str
) -> dict[str, Any] | None:
    """在一份 build 响应里找指定 matching_field 的清单条目。

    Args:
        payload: ``getBuild`` / ``getPatchBuild`` 的 JSON。
        matching_field: 清单类别，主资源为 ``game``。

    Returns:
        清单条目；没有时返回 ``None``。
    """
    for entry in _data_mapping(payload).get("manifests") or []:
        if (
            isinstance(entry, dict)
            and str(entry.get("matching_field") or "").casefold() == matching_field
        ):
            return entry
    return None


def _build_query(preset: RegionPreset, package: PackageInfo, branch: str) -> str:
    """拼 ``getBuild`` / ``getPatchBuild`` 的查询串。

    两个端点查询串同形；差分端点不带 ``tag``，基线版本改由响应里的
    ``diff_tagged_info`` 按本机已装版本挑。

    Args:
        preset: 目标区服。
        package: 主分支包信息。
        branch: 分支名（``main`` / ``predownload``）。

    Returns:
        查询串（不含 ``?``）。
    """
    return (
        f"plat_app={preset.plat_app}&branch={branch}"
        f"&password={package.password}&package_id={package.package_id}"
    )


async def fetch_main_manifest_ref(
    client: httpx.AsyncClient, preset: RegionPreset, package: PackageInfo
) -> ManifestRef:
    """取目标版本主清单的位置。

    Args:
        client: 复用的 HTTP 客户端。
        preset: 目标区服。
        package: 主分支包信息。

    Returns:
        主清单位置。

    Raises:
        UpdaterError: 接口异常或没有 ``game`` 清单时。
    """
    # tag 必须用接口返回的原始 3 段串；补成 4 段会被服务端判 -202 not found
    url = (
        f"{preset.get_build_url}?{_build_query(preset, package, 'main')}"
        f"&tag={package.tag}"
    )
    payload = await _request(client, url)
    entry = _find_manifest_entry(payload, MAIN_MATCHING_FIELD)
    if entry is None:
        raise UpdaterError(f"{preset.label}：主清单里没有 matching_field=game 的条目")
    return _manifest_ref(entry, from_diff=False)


async def fetch_patch_manifest_ref(
    client: httpx.AsyncClient, preset: RegionPreset, package: PackageInfo, baseline: str
) -> ManifestRef | None:
    """取差分清单的位置；没有可用差分时返回 ``None``。

    Args:
        client: 复用的 HTTP 客户端。
        preset: 目标区服。
        package: 主分支包信息。
        baseline: 本机已装的基线版本（3 段串）。

    Returns:
        差分清单位置；该基线没有差分时返回 ``None``。

    Raises:
        UpdaterError: 接口异常时。
    """
    url = f"{preset.get_patch_build_url}?{_build_query(preset, package, 'main')}"
    # 差分端点只收 POST；发 GET 会被服务端判 405
    payload = await _request(client, url, method="POST")
    entry = _find_manifest_entry(payload, MAIN_MATCHING_FIELD)
    if entry is None:
        return None
    # 基线版本不在 URL 上：每份清单的 stats 直接以基线版本为键
    baselines = entry.get("stats") or {}
    if not any(str(tag).casefold() == baseline.casefold() for tag in baselines):
        return None
    return _manifest_ref(entry, from_diff=True)


async def fetch_manifest_bytes(client: httpx.AsyncClient, ref: ManifestRef) -> bytes:
    """下载并解压一份清单。

    Args:
        client: 复用的 HTTP 客户端。
        ref: 清单位置。

    Returns:
        解压后的 protobuf 字节。
    """
    url = f"{ref.manifest_url_prefix.rstrip('/')}/{ref.manifest_id}"
    raw = await _request_bytes(client, url, timeout=_MANIFEST_TIMEOUT)
    return _decompress(raw, compressed=ref.manifest_compressed)


def parse_manifest_assets(data: bytes) -> Any:
    """解析主清单，返回 protobuf 消息对象。

    Args:
        data: 解压后的主清单字节。

    Returns:
        ``SophonManifestProto`` 消息。
    """
    message = _SophonManifestProto()
    try:
        message.ParseFromString(data)
    except DecodeError as error:
        raise UpdaterError(f"主清单解析失败: {error}") from error
    return message


def parse_patch_manifest(data: bytes) -> Any:
    """解析差分清单，返回 protobuf 消息对象。

    Args:
        data: 解压后的差分清单字节。

    Returns:
        ``SophonPatchProto`` 消息。
    """
    message = _SophonPatchProto()
    try:
        message.ParseFromString(data)
    except DecodeError as error:
        raise UpdaterError(f"差分清单解析失败: {error}") from error
    return message


def _pick_asset_info(asset_property: Any, baseline: str) -> Any | None:
    """在差分条目里挑出本机基线对应的那份 ``asset_info``。

    差分清单为每个基线版本都存了一份分片信息；拿错基线会把别的版本的分片
    当成本次要下的量。

    Args:
        asset_property: ``SophonPatchAssetProperty``。
        baseline: 本机基线版本。

    Returns:
        对应的 ``SophonPatchAssetInfo``；没有该基线时返回 ``None``。
    """
    wanted = baseline.casefold()
    for info in asset_property.AssetInfos:
        if str(info.VersionTag).casefold() == wanted:
            return info
    return None


def build_patch_assets(
    patch_proto: Any, target_assets: Any, baseline: str
) -> tuple[tuple[PatchAsset, ...], tuple[str, ...]]:
    """把差分清单与目标清单合并成待处理明细，并收集待删文件。

    以差分清单点名的文件为准：``original_file_name`` 为空的是 CopyOver（分片本身
    即新内容），非空的是 Patch（用 hpatchz 打到旧文件上）。目标清单里未被点名的
    文件本机已有且内容一致，不参与本次更新——把它们也算成「要下」的话，一次约
    10 GB 的增量会变成上百 GB 的全量下载。

    Args:
        patch_proto: ``SophonPatchProto`` 消息。
        target_assets: 目标版本主清单消息。
        baseline: 本机基线版本。

    Returns:
        ``(待处理明细, 待删旧文件)`` 二元组。

    Raises:
        UpdaterError: 差分清单里点名的文件缺少本机基线的分片信息时。
    """
    target_index = {str(asset.AssetName): asset for asset in target_assets.Assets}
    assets: list[PatchAsset] = []

    for asset_property in patch_proto.PatchAssets:
        name = str(asset_property.AssetName)
        target = target_index.get(name)
        if target is None:
            # 差分清单点名但目标清单里没有：文件被移除，交给 unused_assets 处理
            continue
        info = _pick_asset_info(asset_property, baseline)
        if info is None:
            # 点名了却没有本机基线的分片：协议与预期不符。漏掉这个文件却照样写
            # 版本号会产出「混装却标新版」的客户端，宁可停手
            raise UpdaterError(f"差分清单缺少基线 {baseline} 的分片信息: {name}")
        chunk = info.Chunk
        original_name = str(chunk.OriginalFileName)
        assets.append(
            PatchAsset(
                name=name,
                target_size=int(target.AssetSize),
                target_md5=str(target.AssetHashMd5),
                method=METHOD_PATCH if original_name else METHOD_COPYOVER,
                patch_name=str(chunk.PatchName),
                # 要下的长度是 PatchLength：PatchSize 是整个 blob 的大小，
                # 多个文件共用同一 blob，按文件累加会重复计数（实测把 10 GB 报成 85 GB）。
                # CopyOver 时 PatchLength 就等于新文件的完整大小。
                patch_offset=int(chunk.PatchOffset),
                patch_length=int(chunk.PatchLength),
                original_name=original_name,
                original_md5=str(chunk.OriginalFileMd5),
            )
        )

    return tuple(assets), collect_removals(patch_proto, set(target_index))


def summarize_assets(
    assets: Sequence[PatchAsset], removals: Sequence[str]
) -> PatchSummary:
    """把待处理明细压成体量统计。

    Args:
        assets: 待处理明细。
        removals: 待删旧文件。

    Returns:
        体量统计。
    """
    return PatchSummary(
        file_count=len(assets),
        download_size=sum(asset.patch_length for asset in assets),
        patch_count=sum(1 for asset in assets if asset.method == METHOD_PATCH),
        copyover_count=sum(1 for asset in assets if asset.method == METHOD_COPYOVER),
        removals=tuple(removals),
        largest_target=max((asset.target_size for asset in assets), default=0),
    )


#: 无论清单怎么说都不删除的根目录文件（小写比对）：配置与主程序一旦删错，
#: 客户端直接不可用
_PROTECTED_REMOVALS = frozenset({"config.ini", "yuanshen.exe", "genshinimpact.exe"})


def collect_removals(patch_proto: Any, in_target: set[str]) -> tuple[str, ...]:
    """收集需要删除的旧文件。

    ``unused_assets`` 是被淘汰的文件，但目标清单里仍在用的必须留下——判据以目标
    清单为准，不以差分清单的声明为准。比对大小写不敏感：Windows 上同一个文件的
    大小写漂移不该被当成两个，否则会删掉目标清单里仍在用的文件。

    Args:
        patch_proto: ``SophonPatchProto`` 消息。
        in_target: 目标清单里的文件名集合。

    Returns:
        待删除的相对路径元组。
    """
    known = {name.casefold() for name in in_target}
    removed: list[str] = []
    for unused in patch_proto.UnusedAssets:
        for info in unused.AssetInfos:
            for asset in info.Assets:
                name = str(asset.FileName)
                if not name:
                    continue
                key = name.casefold()
                if key in known:
                    continue
                if "/" not in key and "\\" not in key and key in _PROTECTED_REMOVALS:
                    logger.warning("清单要求删除受保护的文件，已跳过: {}", name)
                    continue
                removed.append(name)
    return tuple(sorted(set(removed)))


# =====================================================================================
# 编排：只算不做
# =====================================================================================


async def plan_update(
    game_dir: Path,
    *,
    region: str,
    executable_names: Iterable[str] = ("YuanShen.exe", "GenshinImpact.exe"),
    client: httpx.AsyncClient | None = None,
    timeout: float = 30.0,
) -> GenshinPlan:
    """算出本次要怎么更新，**不写任何文件**。

    Args:
        game_dir: 游戏安装目录（``config.ini`` 与可执行文件所在那一级）。
        region: 区服短名，``"cn"`` 或 ``"global"``。
        executable_names: 判定「已安装」用的候选可执行文件名。
        client: 复用的 HTTP 客户端；``None`` 时临时新建一个。
        timeout: 单次请求超时（秒）。

    Returns:
        :class:`GenshinPlan`。任何协议层异常都转成 ``kind=UNKNOWN`` 的结论，
        由调用方按「无法判定」放行，而不是拦住任务。
    """
    preset = get_region_preset(region)
    local_version = read_local_version(game_dir)
    installed = has_installed_executable(game_dir, executable_names)

    owned = client is None
    http = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    try:
        main_package, preload_package = await fetch_branches(http, preset)
        remote_version = main_package.tag

        if not installed:
            return GenshinPlan(
                region=region,
                game_dir=game_dir,
                state=InstallState.NOT_INSTALLED,
                kind=UpdateKind.NOT_INSTALLED,
                local_version=local_version,
                remote_version=remote_version,
                message="该目录里没有原神客户端，等同全新安装，不自动执行",
            )

        if not local_version:
            return GenshinPlan(
                region=region,
                game_dir=game_dir,
                state=InstallState.UNKNOWN,
                kind=UpdateKind.UNKNOWN,
                local_version="",
                remote_version=remote_version,
                message="读不出本机游戏版本（config.ini 缺失或没有版本号），无法判定",
            )

        if local_version.casefold() == remote_version.casefold():
            if preload_package is not None and preload_package.tag:
                return GenshinPlan(
                    region=region,
                    game_dir=game_dir,
                    state=InstallState.UP_TO_DATE,
                    kind=UpdateKind.PRELOAD,
                    local_version=local_version,
                    remote_version=preload_package.tag,
                    message=(
                        f"官方已开放预下载 {preload_package.tag}，本模块不接管预下载"
                    ),
                )
            return GenshinPlan(
                region=region,
                game_dir=game_dir,
                state=InstallState.UP_TO_DATE,
                kind=UpdateKind.NOOP,
                local_version=local_version,
                remote_version=remote_version,
            )

        target_ref = await fetch_main_manifest_ref(http, preset, main_package)
        patch_ref = await fetch_patch_manifest_ref(
            http, preset, main_package, local_version
        )
        if patch_ref is None:
            return GenshinPlan(
                region=region,
                game_dir=game_dir,
                state=InstallState.NEEDS_UPDATE,
                kind=UpdateKind.FULL_COMPARE,
                local_version=local_version,
                remote_version=remote_version,
                message=(
                    f"官方没有下发 {local_version} -> {remote_version} 的差分包，"
                    "只能逐文件全量比对，不自动执行"
                ),
            )

        patch_bytes = await fetch_manifest_bytes(http, patch_ref)
        target_bytes = await fetch_manifest_bytes(http, target_ref)
        patch_proto = parse_patch_manifest(patch_bytes)
        target_assets = parse_manifest_assets(target_bytes)
        assets, removals = build_patch_assets(patch_proto, target_assets, local_version)
        return GenshinPlan(
            region=region,
            game_dir=game_dir,
            state=InstallState.NEEDS_UPDATE,
            kind=UpdateKind.PATCH,
            local_version=local_version,
            remote_version=remote_version,
            patch=summarize_assets(assets, removals),
            assets=assets,
            diff_url_prefix=patch_ref.chunk_url_prefix,
            diff_compressed=patch_ref.chunk_compressed,
        )
    except UpdaterError as error:
        logger.warning("原神更新计划失败: {}", error)
        return GenshinPlan(
            region=region,
            game_dir=game_dir,
            state=InstallState.UNKNOWN,
            kind=UpdateKind.UNKNOWN,
            local_version=local_version,
            remote_version="",
            message=str(error),
        )
    except Exception as error:  # noqa: BLE001
        # 兜底：响应形状异常（字段类型不符等）也不能逃逸成任务崩溃，
        # 一律按「无法判定」交给调用方放行。CancelledError 属 BaseException，不受影响
        logger.opt(exception=True).warning("原神更新计划异常: {}", error)
        return GenshinPlan(
            region=region,
            game_dir=game_dir,
            state=InstallState.UNKNOWN,
            kind=UpdateKind.UNKNOWN,
            local_version=local_version,
            remote_version="",
            message=f"检查更新时出现异常: {error}",
        )
    finally:
        if owned:
            await http.aclose()


# =====================================================================================
# 编排：执行计划
# =====================================================================================

#: 一行面向用户的进度文案
ProgressHook = Callable[[str], Awaitable[None]]

#: 中止判定；为真时在文件批次边界收工
AbortHook = Callable[[], bool]

#: 同时处理的文件数；下载与打补丁都在这个上限内
_WORKERS = 6

#: 中间产物目录（建在游戏目录内，保证与目标同卷，替换才是原子改名）
_TEMP_DIR_NAME = "_mas_update"

#: 已完成文件的流水账，用于中断后接着更新
_JOURNAL_NAME = "done.txt"

#: 替换目标文件前额外要求的磁盘余量
_DISK_MARGIN_BYTES = 2 * 1024**3

#: 单次差分分片请求的超时（秒）
_CHUNK_TIMEOUT = 120.0

#: 单次清单下载的超时（秒）；清单可达数十 MB，比元数据查询宽
_MANIFEST_TIMEOUT = 300.0

#: 单次 hpatchz 调用的超时（秒）；正常远快于此，挂住即视为失败
_HPATCHZ_TIMEOUT = 600.0

#: 进度行的最小间隔（秒）
_PROGRESS_INTERVAL = 1.0

_dir_locks: dict[str, asyncio.Lock] = {}


class PathUnsafeError(RuntimeError):
    """清单里的路径不可信。"""


@dataclass(frozen=True)
class InstallResult:
    """一次增量执行的结论。"""

    success: bool
    message: str
    version: str = ""
    files_done: int = 0
    files_total: int = 0
    bytes_downloaded: int = 0
    removed: int = 0
    #: 被中止判定打断；已落盘的合法文件保留
    aborted: bool = False


def game_dir_lock(game_dir: Path) -> asyncio.Lock:
    """取某个游戏目录的互斥锁。

    同一个客户端可能被多个用户共用，两轮更新并行会互相破坏文件。

    Args:
        game_dir: 游戏安装目录。

    Returns:
        该目录对应的锁（进程内单例）。
    """
    key = str(game_dir.resolve()).casefold()
    lock = _dir_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _dir_locks[key] = lock
    return lock


def cleanup_temp_dir(game_dir: Path) -> None:
    """清掉游戏目录里的中间产物目录。

    流水账只对「同一基线 -> 同一目标」的续传有意义；版本已追平（NOOP）或
    更新被官方启动器接管后就不再可信，留着只会污染目录。

    Args:
        game_dir: 游戏安装目录。
    """
    shutil.rmtree(game_dir / _TEMP_DIR_NAME, ignore_errors=True)


def safe_relative(name: str) -> Path:
    """把清单里的相对路径转成可信路径。

    清单内容完全来自网络，直接拼进文件系统会被路径穿越攻击：绝对路径、盘符、
    ``..`` 分量或指向目录自身的写法都可能写到游戏目录之外。

    Args:
        name: 清单里的相对路径。

    Returns:
        校验通过的相对路径。

    Raises:
        PathUnsafeError: 路径非法时。
    """
    raw = str(name or "").strip().replace("\\", "/")
    if not raw:
        raise PathUnsafeError("清单条目缺少路径")
    if "\x00" in raw:
        # Windows 上带 NUL 的路径会让 resolve() 抛 ValueError，那不在调用方的
        # 捕获范围内，会在更外层变成任务崩溃
        raise PathUnsafeError(f"清单条目含 NUL 字符: {name!r}")
    candidate = Path(raw)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        raise PathUnsafeError(f"清单条目是绝对路径: {name!r}")
    if not candidate.parts:
        raise PathUnsafeError(f"清单条目指向目录自身: {name!r}")
    if ".." in candidate.parts:
        raise PathUnsafeError(f"清单条目含上跳分量: {name!r}")
    return candidate


def resolve_within(root: Path, name: str) -> Path:
    """在 ``root`` 下解析清单路径，并复查结果没有逃出 ``root``。

    Args:
        root: 游戏安装目录。
        name: 清单里的相对路径。

    Returns:
        解析后的绝对路径。

    Raises:
        PathUnsafeError: 解析结果逃出 ``root`` 时。
    """
    resolved = (root / safe_relative(name)).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise PathUnsafeError(f"清单条目逃出目标目录: {name!r}")
    return resolved


def _md5_file(path: Path) -> str:
    """算文件 MD5。

    Args:
        path: 文件路径。

    Returns:
        32 位小写十六进制串。
    """
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while block := handle.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


async def _target_is_current(target: Path, asset: PatchAsset) -> bool:
    """目标文件是否已是差分要更新到的内容。

    先比体积（近乎免费），一致才算整文件 MD5；两处都在线程里跑，不占事件循环。

    Args:
        target: 目标文件的绝对路径。
        asset: 待处理明细（带目标体积与 MD5）。

    Returns:
        体积与 MD5 都与目标一致时为真；文件不存在按「不一致」处理。
    """
    if not await asyncio.to_thread(target.is_file):
        return False
    stat = await asyncio.to_thread(target.stat)
    if stat.st_size != asset.target_size:
        return False
    return await asyncio.to_thread(_md5_file, target) == asset.target_md5


def _run_hpatchz(exe: Path, old: Path, diff: Path, out: Path) -> None:
    """调 ``hpatchz`` 把差分打到旧文件上，产出新文件。

    Args:
        exe: ``hpatchz`` 可执行文件。
        old: 旧文件。
        diff: 差分数据。
        out: 产出的新文件。

    Raises:
        UpdaterError: 子进程返回非零或超时时。
    """
    try:
        result = subprocess.run(
            [str(exe), "-f", str(old), str(diff), str(out)],
            capture_output=True,
            timeout=_HPATCHZ_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise UpdaterError(f"hpatchz 超时（{_HPATCHZ_TIMEOUT} 秒）: {out}") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise UpdaterError(f"hpatchz 失败（{result.returncode}）: {detail[-200:]}")


async def _fetch_range(
    client: httpx.AsyncClient, url: str, offset: int, length: int
) -> bytes:
    """按 Range 取一段差分数据。

    差分分片不压缩，取回来的字节就是原样内容：Patch 是 ldiff 数据，CopyOver 是
    完整的新文件。

    Args:
        client: 复用的 HTTP 客户端。
        url: 分片地址。
        offset: 段起点。
        length: 段长度。

    Returns:
        取回的字节。

    Raises:
        UpdaterError: 网络失败或长度不符时。
    """
    headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
    try:
        response = await client.get(url, headers=headers, timeout=_CHUNK_TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise UpdaterError(f"取差分分片失败 {url}: {error}") from error
    data = response.content
    if len(data) != length:
        raise UpdaterError(f"差分分片长度不符 {url}: 期望 {length} 实际 {len(data)}")
    return data


def _journal_path(temp_dir: Path) -> Path:
    """取流水账文件路径。"""
    return temp_dir / _JOURNAL_NAME


def _load_journal(temp_dir: Path, expected_key: str) -> set[str]:
    """读出已完成文件名单；流水账不属于本次计划时整份作废。

    Args:
        temp_dir: 中间产物目录。
        expected_key: 本次计划的标识（基线版本 -> 目标版本）。流水账是
            「哪些文件已到目标内容」的记录，跨了版本就不再可信：比如上次
            中止后用户改用官方启动器把游戏更到了中间版本，旧名单会把没更新
            的文件当成已完成，静默产出混装客户端。

    Returns:
        本次计划下已完成文件的相对路径集合。
    """
    journal = _journal_path(temp_dir)
    try:
        lines = journal.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    if not lines or lines[0].strip() != expected_key:
        # 键不符即整份作废：删掉重记，避免旧名单污染本次进度
        journal.unlink(missing_ok=True)
        return set()
    return {line.strip() for line in lines[1:] if line.strip()}


def _start_journal(temp_dir: Path, key: str) -> None:
    """确保流水账存在且首行是本次计划的标识。"""
    journal = _journal_path(temp_dir)
    if not journal.exists():
        journal.write_text(key + "\n", encoding="utf-8")


def _append_journal(temp_dir: Path, name: str) -> None:
    """把一个已完成文件追加进流水账。"""
    with _journal_path(temp_dir).open("a", encoding="utf-8") as handle:
        handle.write(name + "\n")


async def _apply_asset(
    client: httpx.AsyncClient,
    game_dir: Path,
    temp_dir: Path,
    asset: PatchAsset,
    hpatchz: Path,
    diff_url_prefix: str,
    diff_compressed: bool,
) -> int:
    """处理一个文件：取差分 → 校验 → 同卷原子替换 → 记流水账。

    流水账在 ``os.replace`` 成功后立刻补记：批次里其他文件失败或进程被硬杀时，
    已落盘的文件不能被重试重做——原位补丁（``original_name`` 与 ``name`` 同路径）
    重做会拿已经是新内容的目标文件当旧文件，产物校验必败，更新从此卡死。

    Args:
        client: 复用的 HTTP 客户端。
        game_dir: 游戏安装目录。
        temp_dir: 中间产物目录（存差分数据与流水账）。
        asset: 待处理明细。
        hpatchz: ``hpatchz`` 可执行文件路径。
        diff_url_prefix: 差分分片基址。
        diff_compressed: 差分分片是否 zstd 压缩。

    Returns:
        本次下载的字节数（压缩时按网线上的长度计）。

    Raises:
        UpdaterError: 取回或校验失败时。
        PathUnsafeError: 路径不可信时。
    """
    target = resolve_within(game_dir, asset.name)
    # 自愈检查：目标已是本次要更新到的内容（上次中断前落盘、流水账没来得及记），
    # 直接跳过。不查就重做的话，原位补丁会拿新文件当旧文件打补丁，产物校验必败，
    # 更新从此卡死
    if await _target_is_current(target, asset):
        _append_journal(temp_dir, asset.name)
        return 0

    url = f"{diff_url_prefix.rstrip('/')}/{asset.patch_name}"
    raw = await _fetch_range(client, url, asset.patch_offset, asset.patch_length)
    downloaded = len(raw)
    # 压缩标志跟随接口声明：PatchLength 是网线上的长度，长度校验在解压之前做
    data = _decompress(raw, compressed=True) if diff_compressed else raw

    # 先落到同目录的临时名，校验通过再原子改名——中途失败不会留下半个目标文件
    staging = target.with_name(f"{target.name}.mas-new")
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)

    try:
        if asset.method == METHOD_COPYOVER:
            # 分片本身就是新内容
            actual = await asyncio.to_thread(lambda: hashlib.md5(data).hexdigest())
            if actual != asset.target_md5:
                raise UpdaterError(f"CopyOver 内容校验失败: {asset.name}")
            await asyncio.to_thread(staging.write_bytes, data)
        else:
            diff_path = temp_dir / f"{uuid.uuid4().hex}.diff"
            await asyncio.to_thread(diff_path.write_bytes, data)
            try:
                old = resolve_within(game_dir, asset.original_name)
                if not await asyncio.to_thread(old.is_file):
                    raise UpdaterError(f"打补丁所需的旧文件缺失: {asset.original_name}")
                # 旧文件必须仍是差分基线内容：不符说明它被别的途径改过（比如
                # 上次中断后已经换新），硬打出来的东西过不了校验，只会更难排查
                if (
                    asset.original_md5
                    and await asyncio.to_thread(_md5_file, old) != asset.original_md5
                ):
                    raise UpdaterError(
                        f"补丁源文件不是差分基线内容（可能已被更新过）: "
                        f"{asset.original_name}"
                    )
                await asyncio.to_thread(_run_hpatchz, hpatchz, old, diff_path, staging)
            finally:
                # 用 suppress 包住：取消落在 hpatchz 上时文件可能仍被占用，
                # 让这儿抛 OSError 会把 CancelledError 顶掉、任务被误判成写入失败
                with suppress(OSError):
                    diff_path.unlink(missing_ok=True)
            actual = await asyncio.to_thread(_md5_file, staging)
            if actual != asset.target_md5:
                raise UpdaterError(f"补丁结果校验失败: {asset.name}")

        os.replace(staging, target)
        _append_journal(temp_dir, asset.name)
        return downloaded
    finally:
        # 成功路径里 staging 已被 replace 走，这里只是兜底清掉失败残骸；
        # 同样不能让它抛 OSError 掩盖取消
        with suppress(OSError):
            staging.unlink(missing_ok=True)


def write_local_version(game_dir: Path, version: str) -> None:
    """把新版本号写回 ``config.ini`` 的 ``[General]``。

    只改 ``game_version`` 那一行，其余内容与原文件（含 BOM）、行尾原样保留。
    行解析与读取一致：不用 ``configparser``，该文件含重复键与非常规写法。

    读取与写回都是**严格 UTF-8**：官方写的就是 UTF-8，解不出（或被别的编码改过）
    宁可停手，也不能用替换字符把整份配置写坏。落盘走原子写，断电不会截断配置。

    版本号是「本轮已完成」的标记，只在全部文件落盘、旧文件清理完之后才写。

    Args:
        game_dir: 游戏安装目录。
        version: 目标版本串。

    Raises:
        UpdaterError: ``config.ini`` 不存在、不是 UTF-8、没有 ``game_version``
            或写回失败时。
    """
    ini_path = game_dir / "config.ini"
    try:
        raw_bytes = ini_path.read_bytes()
    except OSError as error:
        raise UpdaterError(f"读取 config.ini 失败: {ini_path} - {error}") from error

    has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise UpdaterError(
            f"config.ini 不是 UTF-8（{error}），拒绝改写以免损坏: {ini_path}"
        ) from error
    lines = text.splitlines(keepends=True)

    in_general = False
    replaced = False
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_general = stripped[1:-1].strip().casefold() == "general"
            continue
        if not in_general or replaced:
            continue
        key, sep, _ = stripped.partition("=")
        if sep and key.strip().casefold() == "game_version":
            ending = "\r\n" if raw.endswith("\r\n") else "\n"
            lines[index] = f"game_version={version}{ending}"
            replaced = True

    if not replaced:
        raise UpdaterError(f"config.ini 的 [General] 里没有 game_version: {ini_path}")

    payload = ("\ufeff" if has_bom else "") + "".join(lines)
    try:
        atomic_write(ini_path, payload.encode("utf-8"))
    except OSError as error:
        raise UpdaterError(f"写回 config.ini 失败: {ini_path} - {error}") from error


async def _remove_unused(game_dir: Path, removals: Iterable[str]) -> int:
    """删除目标清单已不再使用的旧文件。

    只删文件、不删目录：目录留空不影响客户端，误删目录的代价却大得多。

    Args:
        game_dir: 游戏安装目录。
        removals: 待删相对路径。

    Returns:
        实际删掉的个数。
    """
    removed = 0
    for name in removals:
        target = resolve_within(game_dir, name)
        try:
            if target.is_file():
                target.unlink()
                removed += 1
        except OSError as error:
            logger.warning("删除旧文件失败 {}: {}", target, error)
    return removed


def _disk_has_room(game_dir: Path, needed: int) -> bool:
    """目标盘余量是否够本次更新。

    Args:
        game_dir: 游戏安装目录。
        needed: 本轮需要的字节数。

    Returns:
        余量足够时为真；读不出余量时按足够处理，不拦正常更新。
    """
    try:
        free = shutil.disk_usage(game_dir).free
    except OSError:
        return True
    return free >= needed + _DISK_MARGIN_BYTES


async def execute_plan(
    plan: GenshinPlan,
    *,
    hpatchz: Path,
    on_progress: ProgressHook | None = None,
    should_abort: AbortHook | None = None,
    client: httpx.AsyncClient | None = None,
    workers: int = _WORKERS,
    timeout: float = 30.0,
) -> InstallResult:
    """执行一次增量更新。

    只接受 ``kind=PATCH`` 的计划；其余种类一律拒绝，由调用方明确指出并建议改用
    官方启动器。每个文件按「取差分 → 校验 → 同卷原子替换」推进，任何一步不符预期
    即停手，**不写回版本号**——这样客户端仍认为自己是旧版，不会带病启动。

    被中止判定打断时同样不写版本号，已替换的文件保持有效，重新运行会跳过它们
    接着更新（进度记在游戏目录内的中间产物目录里，成功后自动清掉）。

    Args:
        plan: 由 :func:`plan_update` 得出的计划。
        hpatchz: ``hpatchz`` 可执行文件路径（调用方用 ``ensure_hpatchz`` 取得）。
        on_progress: 一行行进度文案的回调。
        should_abort: 中止判定；为真时在文件批次边界收工。
        client: 复用的 HTTP 客户端；``None`` 时临时新建一个。
        workers: 同时处理的文件数。
        timeout: 单次请求超时（秒）。

    Returns:
        :class:`InstallResult`。协议层异常转成失败结论，不抛给调用方。
    """
    if plan.kind is not UpdateKind.PATCH:
        return InstallResult(
            success=False,
            message=f"本次是{KIND_LABELS[plan.kind]}，不自动执行，请用官方启动器更新",
        )
    summary = plan.patch
    if summary is None or not plan.assets:
        return InstallResult(success=False, message="计划里没有可执行的文件明细")

    game_dir = plan.game_dir
    total = len(plan.assets)

    try:
        resolved_targets = [
            (asset, resolve_within(game_dir, asset.name)) for asset in plan.assets
        ]
        for asset, _ in resolved_targets:
            if asset.method == METHOD_PATCH:
                resolve_within(game_dir, asset.original_name)
    except PathUnsafeError as error:
        logger.warning("原神更新路径校验失败: {}", error)
        return InstallResult(success=False, message=f"清单路径不可信：{error}")

    needed = summary.download_size + summary.largest_target
    if not _disk_has_room(game_dir, needed):
        free = summarize_size(shutil.disk_usage(game_dir).free)
        return InstallResult(
            success=False,
            message=(
                f"磁盘剩余 {free}，本次增量需要同时放下差分包与更新后的文件"
                f"（约 {summarize_size(needed)}），请先清理后再试"
            ),
        )

    owned = client is None
    http = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    temp_dir = game_dir / _TEMP_DIR_NAME
    throttle = _Throttle(_PROGRESS_INTERVAL)
    completed = 0
    downloaded = 0

    async with game_dir_lock(game_dir):
        try:
            await asyncio.to_thread(temp_dir.mkdir, parents=True, exist_ok=True)
            journal_key = f"{plan.local_version or '?'}->{plan.remote_version}"
            done = _load_journal(temp_dir, journal_key)
            if done:
                # 流水账只记名字，跳过前复核目标确实已是本次内容：中途被别的
                # 途径改回旧版或删掉时，不能把它当成已完成，否则会漏更却写版本号
                by_name = {asset.name: asset for asset in plan.assets}
                verified: set[str] = set()
                for name in done:
                    asset = by_name.get(name)
                    if asset is None:
                        continue
                    if await _target_is_current(resolve_within(game_dir, name), asset):
                        verified.add(name)
                done = verified
            _start_journal(temp_dir, journal_key)
            pending = [asset for asset in plan.assets if asset.name not in done]
            completed = len(done)

            if on_progress is not None:
                skipped = f"，已跳过 {completed} 个" if completed else ""
                await on_progress(
                    f"开始增量更新：{total} 个文件，待下载 "
                    f"{summarize_size(summary.download_size)}{skipped}"
                )

            size = max(1, workers)
            for start in range(0, len(pending), size):
                if should_abort is not None and should_abort():
                    return InstallResult(
                        success=False,
                        aborted=True,
                        message="更新已中止；已完成的部分保留，重新运行会接着更新",
                        files_done=completed,
                        files_total=total,
                        bytes_downloaded=downloaded,
                    )
                batch = pending[start : start + size]
                outcomes = await asyncio.gather(
                    *(
                        _apply_asset(
                            http,
                            game_dir,
                            temp_dir,
                            asset,
                            hpatchz,
                            plan.diff_url_prefix,
                            plan.diff_compressed,
                        )
                        for asset in batch
                    ),
                    return_exceptions=True,
                )
                failures = [
                    outcome
                    for outcome in outcomes
                    if isinstance(outcome, BaseException)
                ]
                if failures:
                    raise failures[0]
                # 流水账已由 _apply_asset 在替换成功后逐文件补记，这里只计数
                for outcome in outcomes:
                    completed += 1
                    downloaded += int(outcome)  # type: ignore[arg-type]

                if on_progress is not None and throttle.ready():
                    await on_progress(
                        f"更新中 {completed}/{total} · 已下载 {summarize_size(downloaded)}"
                    )

            removed = await _remove_unused(game_dir, summary.removals)
            write_local_version(game_dir, plan.remote_version)
            await asyncio.to_thread(shutil.rmtree, temp_dir, True)

            if on_progress is not None:
                await on_progress(
                    f"原神客户端更新完成 {plan.local_version or '?'} -> "
                    f"{plan.remote_version or '?'}"
                    + (f"，清理旧文件 {removed} 个" if removed else "")
                )
            return InstallResult(
                success=True,
                message="更新完成",
                version=plan.remote_version,
                files_done=completed,
                files_total=total,
                bytes_downloaded=downloaded,
                removed=removed,
            )
        except UpdaterError as error:
            logger.warning("原神更新执行失败: {}", error)
            return InstallResult(
                success=False,
                message=str(error),
                files_done=completed,
                files_total=total,
                bytes_downloaded=downloaded,
            )
        except PathUnsafeError as error:
            logger.warning("原神更新路径校验失败: {}", error)
            return InstallResult(
                success=False,
                message=f"清单路径不可信：{error}",
                files_done=completed,
                files_total=total,
                bytes_downloaded=downloaded,
            )
        except OSError as error:
            logger.warning("原神更新写入失败: {}", error)
            return InstallResult(
                success=False,
                message=f"写入游戏目录失败：{error}",
                files_done=completed,
                files_total=total,
                bytes_downloaded=downloaded,
            )
        except Exception as error:  # noqa: BLE001
            # 兜底：意料之外的异常也不能逃逸成任务崩溃（已替换的文件保持有效，
            # 不写版本号，重开即续传）。CancelledError 属 BaseException，不受影响
            logger.opt(exception=True).warning("原神更新出现异常: {}", error)
            return InstallResult(
                success=False,
                message=f"更新时出现异常：{error}",
                files_done=completed,
                files_total=total,
                bytes_downloaded=downloaded,
            )
        finally:
            if owned:
                await http.aclose()


class _Throttle:
    """把进度行压到固定间隔一条。"""

    def __init__(self, interval: float) -> None:
        """记录间隔并置零起点。

        Args:
            interval: 最小间隔（秒）。
        """
        self._interval = interval
        self._last = 0.0

    def ready(self) -> bool:
        """现在是否该发一条。

        Returns:
            距上次放行已超过间隔时为真。
        """
        now = time.monotonic()
        if now - self._last < self._interval:
            return False
        self._last = now
        return True
