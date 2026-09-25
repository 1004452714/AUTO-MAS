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
#   不包含其源文件副本。Sophon 协议定义与流程来自下列 MIT 项目，许可原文见本目录
#   的 LICENSE.Sophon.md：
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

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
import zstandard
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from app.utils import get_logger

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
# 上游原始定义（MIT，见 LICENSE.Sophon.md）：
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
    """``getGamePackages`` 里一个分支的包信息。"""

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


@dataclass(frozen=True)
class PatchSummary:
    """一次增量更新的体量统计。"""

    file_count: int
    download_size: int
    patch_count: int
    copyover_count: int
    removals: tuple[str, ...] = ()


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

    retcode = payload.get("retcode")
    if retcode not in (0, None):
        raise UpdaterError(
            f"接口返回 retcode={retcode} message={payload.get('message')!r}: {url}"
        )
    return payload


async def _request_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    """下载一份清单原始字节。

    Args:
        client: 复用的 HTTP 客户端。
        url: 清单地址。

    Returns:
        原始字节。

    Raises:
        UpdaterError: 网络失败或非 200 时。
    """
    try:
        response = await client.get(url)
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
    return zstandard.ZstdDecompressor().decompress(data, max_output_size=0)


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
    entries = (payload.get("data") or {}).get("game_branches") or []
    if not entries:
        raise UpdaterError(f"{preset.label}：getGameBranches 没有返回分支")
    entry = entries[0]
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
    for entry in payload.get("data", {}).get("manifests", []):
        if str(entry.get("matching_field") or "").casefold() == matching_field:
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
    raw = await _request_bytes(client, url)
    return _decompress(raw, compressed=ref.manifest_compressed)


def parse_manifest_assets(data: bytes) -> Any:
    """解析主清单，返回 protobuf 消息对象。

    Args:
        data: 解压后的主清单字节。

    Returns:
        ``SophonManifestProto`` 消息。
    """
    message = _SophonManifestProto()
    message.ParseFromString(data)
    return message


def parse_patch_manifest(data: bytes) -> Any:
    """解析差分清单，返回 protobuf 消息对象。

    Args:
        data: 解压后的差分清单字节。

    Returns:
        ``SophonPatchProto`` 消息。
    """
    message = _SophonPatchProto()
    message.ParseFromString(data)
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


def summarize_patch(
    patch_proto: Any, target_assets: Any, baseline: str
) -> PatchSummary:
    """统计一次增量的体量与动作构成。

    以差分清单点名的文件为准：``original_file_name`` 为空的是 CopyOver（分片本身
    即新内容），非空的是 Patch（用 hpatchz 打到旧文件上）。目标清单里未被点名的
    文件本机已有且内容一致，不参与本次更新。

    Args:
        patch_proto: ``SophonPatchProto`` 消息。
        target_assets: 目标版本主清单消息。
        baseline: 本机基线版本。

    Returns:
        体量统计。
    """
    in_target = {str(asset.AssetName) for asset in target_assets.Assets}
    file_count = 0
    download_size = 0
    patch_count = 0
    copyover_count = 0

    for asset_property in patch_proto.PatchAssets:
        name = str(asset_property.AssetName)
        if name not in in_target:
            # 差分清单点名但目标清单里没有：文件被移除，交给 unused_assets 处理
            continue
        info = _pick_asset_info(asset_property, baseline)
        if info is None:
            continue
        chunk = info.Chunk
        # 要下的长度是 PatchLength：PatchSize 是整个差分 blob 的大小，多个文件
        # 共用同一 blob，按文件累加会重复计数（实测会把 10 GB 报成 85 GB）。
        # CopyOver 时 PatchLength 就等于新文件的完整大小。
        size = int(chunk.PatchLength or 0)
        file_count += 1
        download_size += size
        if str(chunk.OriginalFileName):
            patch_count += 1
        else:
            copyover_count += 1

    removals = collect_removals(patch_proto, in_target)
    return PatchSummary(
        file_count=file_count,
        download_size=download_size,
        patch_count=patch_count,
        copyover_count=copyover_count,
        removals=removals,
    )


def collect_removals(patch_proto: Any, in_target: set[str]) -> tuple[str, ...]:
    """收集需要删除的旧文件。

    ``unused_assets`` 是被淘汰的文件，但目标清单里仍在用的必须留下——判据以目标
    清单为准，不以差分清单的声明为准。

    Args:
        patch_proto: ``SophonPatchProto`` 消息。
        in_target: 目标清单里的文件名集合。

    Returns:
        待删除的相对路径元组。
    """
    removed: list[str] = []
    for unused in patch_proto.UnusedAssets:
        for info in unused.AssetInfos:
            for asset in info.Assets:
                name = str(asset.FileName)
                if name and name not in in_target:
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
        summary = summarize_patch(patch_proto, target_assets, local_version)
        return GenshinPlan(
            region=region,
            game_dir=game_dir,
            state=InstallState.NEEDS_UPDATE,
            kind=UpdateKind.PATCH,
            local_version=local_version,
            remote_version=remote_version,
            patch=summary,
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
    finally:
        if owned:
            await http.aclose()
