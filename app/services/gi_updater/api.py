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

"""HYP Connect 协议访问：HTTP 传输、启动器端点编排与响应模型。

**HTTP 客户端**按用途分两类通道：General 通道带 ``x-rpc-device_id``、可选解压；
Resource 通道同上但关闭自动解压（下载二进制流时不能让框架偷偷解压）。用标准库
``urllib.request`` 实现，环境装了 ``requests`` 则自动切换过去；统一重试（默认 5 次，
指数退避）与超时，``x-rpc-device_id`` 首次生成后持久化到用户目录、之后复用同一个
值，并支持 Range 请求（断点续传）。

**启动器端点**并发拉齐更新要用的元数据：Sophon 分支（``getGameBranches``，必须最先，
因为版本以它为准）、资源包（``getGamePackages``）、游戏信息；新闻与 WPF 包跟更新无关，
省略。拿到结果后补一次伪造：当 zip 资源不可用、或版本低于 Sophon 分支时，用 Sophon
的 tag 伪造 ``major`` 与 ``patches``。

**响应模型**字段命名与 JSON 键名逐条对齐，便于对照官方响应：

    getGamePackages   -> HypLauncherGameResourcePackageApi / HypResourcesData / HypPackageInfo
    getGameBranches   -> HypLauncherSophonBranchesApi / HypLauncherSophonBranchesKind
    getBuild          -> SophonManifestBuildBranch / SophonManifestBuildIdentity
    getBuild(patch)   -> SophonManifestPatchBranch / SophonManifestPatchIdentity
"""

from __future__ import annotations

import gzip
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from app.services.gi_updater.common import get_logger
from app.services.gi_updater.presets import PresetConfig

__all__ = [
    "HttpResponse",
    "HttpClient",
    "HttpError",
    "load_or_create_device_id",
    "mask_url_password",
    "LauncherApi",
    "LauncherApiError",
    "HypGameInfoData",
    "HypResourcesData",
    "HypResourcePackageData",
    "HypPackageInfo",
    "HypPackageData",
    "GamePackageResult",
    "HypGameInfoBranchData",
    "HypLauncherSophonBranchesKind",
    "SophonManifestFileInfo",
    "SophonManifestUrlInfo",
    "SophonManifestChunkInfo",
    "SophonManifestBuildIdentity",
    "SophonManifestBuildBranch",
    "SophonManifestPatchIdentity",
    "SophonManifestPatchBranch",
    "parse_game_packages",
    "parse_game_branches",
    "parse_build",
    "parse_patch_build",
]


try:  # pragma: no cover - requests 可选
    import requests  # type: ignore
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


#: 启动器同款 User-Agent 前缀（``HYPContainer`` 版本可随官方升级）
DEFAULT_USER_AGENT = "HYPContainer/1.16.1.361 (windows 11) collapse-python-updater/1.0"

#: 请求地址里的分支密码，写进日志与异常文本前先打码
_PASSWORD_QUERY_RE = re.compile(r"(?i)([?&]password=)[^&]*")


def mask_url_password(url: str) -> str:
    """把请求地址里的 ``password=`` 查询参数替换成 ``***``。

    分支密码是启动器内置的公开常量、不是用户凭据；但同一个地址经宿主日志出去时会被
    打码、经异常文本出去时却是原文，两条日志对不上反而误导排障，所以统一在出口打码。

    Args:
        url: 原始请求地址。

    Returns:
        打码后的地址；不含该参数时原样返回。
    """
    return _PASSWORD_QUERY_RE.sub(r"\1***", url)


def _decode_body(body: bytes, encoding: str) -> bytes:
    """按 ``Content-Encoding`` 解压响应体。

    我们主动声明了 ``Accept-Encoding: gzip, deflate``，所以服务端大概率会压缩；
    urllib 不会自动解压，必须自己按 ``Content-Encoding`` 处理。

    Args:
        body: 原始（可能已压缩的）响应字节。
        encoding: 小写后的 ``Content-Encoding`` 值。

    Returns:
        解压后的字节；无对应编码或解压失败时原样返回 ``body``（不抛异常）。
    """
    if not body:
        return body
    if "gzip" in encoding:
        try:
            return gzip.decompress(body)
        except OSError:  # pragma: no cover - 已解压过就原样返回
            return body
    if "deflate" in encoding:
        try:
            return zlib.decompress(body)
        except zlib.error:
            try:
                return zlib.decompress(body, -zlib.MAX_WBITS)
            except zlib.error:  # pragma: no cover
                return body
    return body


def _wrap_decompressor(stream: Any, encoding: str) -> Any:
    """给流式响应套一层解压器（同样支持 ``read()``）。

    Args:
        stream: 底层二进制流。
        encoding: 小写后的 ``Content-Encoding`` 值。

    Returns:
        包装后的流对象；无对应编码时直接返回原 ``stream``。
    """
    if "gzip" in encoding:
        try:
            return gzip.GzipFile(fileobj=stream)
        except OSError:  # pragma: no cover
            return stream
    if "deflate" in encoding:
        return _ZlibStreamWrapper(stream)
    return stream


class _ZlibStreamWrapper:
    """把 zlib 解压器包装成 ``read()`` 接口（供 deflate 流式响应使用）。"""

    def __init__(self, stream: Any) -> None:
        """包装一个底层流，供 deflate 流式响应逐块解压。

        Args:
            stream: 底层二进制响应流。
        """
        self._stream = stream
        self._decompressor = zlib.decompressobj()
        self._buffer = b""
        self._eof = False

    def read(self, size: int = -1) -> bytes:
        """增量解压并读取指定字节数。

        Args:
            size: 期望读取的字节数；``-1``/``None`` 表示读到流末尾。

        Returns:
            解压后的字节块；流耗尽后返回剩余缓冲（可能为空）。
        """
        if size is None or size < 0:
            chunks = [self._buffer]
            self._buffer = b""
            while not self._eof:
                raw = self._stream.read(65536)
                if not raw:
                    self._eof = True
                    break
                chunks.append(self._decompressor.decompress(raw))
            chunks.append(self._decompressor.flush())
            return b"".join(chunks)

        while len(self._buffer) < size and not self._eof:
            raw = self._stream.read(65536)
            if not raw:
                self._eof = True
                chunks = [self._buffer, self._decompressor.flush()]
                self._buffer = b"".join(chunks)
                break
            self._buffer += self._decompressor.decompress(raw)

        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def close(self) -> None:  # pragma: no cover
        """关闭底层流（忽略关闭异常）。"""
        try:
            self._stream.close()
        except Exception:  # noqa: BLE001
            pass


class HttpError(RuntimeError):
    """HTTP 请求失败（含重试耗尽）。"""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        """构造异常；附带可选的 HTTP 状态码。

        Args:
            message: 错误描述。
            status: 关联的 HTTP 状态码；``None`` 表示非 HTTP 层错误。
        """
        super().__init__(message)
        self.status = status


@dataclass
class HttpResponse:
    """统一的响应视图。"""

    status: int
    headers: Dict[str, str]
    _body: bytes = b""
    _stream: Optional[Any] = None

    @property
    def content(self) -> bytes:
        """响应体字节。

        首次访问会消费底层流（``_stream.read()``）并缓存到 ``_body``，
        之后重复访问都返回同一份缓存；无流也无缓冲时返回 ``b""``。
        """
        if self._body:
            return self._body
        if self._stream is not None:
            self._body = self._stream.read()
            return self._body
        return b""

    def json(self) -> Any:
        """把响应体按 UTF-8 解析为 JSON。

        Returns:
            解析后的 JSON 对象（``dict``/``list``/标量）。

        Raises:
            json.JSONDecodeError: 响应体不是合法 JSON 时。
        """
        return json.loads(self.content.decode("utf-8"))

    def iter_content(self, chunk_size: int = 65536) -> Iterator[bytes]:
        """流式读取；已缓冲则直接切块返回。

        Args:
            chunk_size: 每块字节数，默认 65536。

        Yields:
            分块字节；已缓冲时直接对 ``_body`` 切块，否则逐块读流。
        """
        if self._body:
            for offset in range(0, len(self._body), chunk_size):
                yield self._body[offset : offset + chunk_size]
            return
        if self._stream is None:
            return
        while True:
            chunk = self._stream.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        """关闭底层流（若存在）；忽略异常。"""
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:  # pragma: no cover
                pass


def _default_device_id_path() -> str:
    """设备 ID 的落盘位置。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "AUTO-MAS", "genshin_updater_device_id")


def load_or_create_device_id(path: Optional[str] = None) -> str:
    """读取或生成 ``x-rpc-device_id``。


    Args:
        path: 设备 ID 文件位置；``None`` 时用默认 ``_default_device_id_path()``。

    Returns:
        已存在的 ID，或新生成的 ID（``{MAC 12 位十六进制}{毫秒时间戳}``）。

    Note:
        首次运行会**写入**设备 ID 文件；写文件失败（只读环境）时静默降级为
        仅内存值，不会抛出 ``OSError``。
    """
    target = path or _default_device_id_path()
    try:
        if os.path.isfile(target):
            with open(target, "r", encoding="utf-8") as handle:
                existing = handle.read().strip()
            if existing:
                return existing
    except OSError:  # pragma: no cover
        pass

    # MachineGuid 在 Windows 注册表，Python 侧用 uuid.getnode() 近似
    node = f"{uuid.getnode():012x}"
    device_id = f"{node}{int(time.time() * 1000)}"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(device_id)
    except OSError:  # pragma: no cover - 只读环境下退化为内存值
        pass
    return device_id


@dataclass
class HttpClient:
    """带重试 / 超时 / 默认头的 HTTP 客户端。"""

    timeout: float = 30.0
    max_retries: int = 5
    retry_backoff: float = 0.5
    user_agent: str = DEFAULT_USER_AGENT
    device_id: str = ""
    verify_ssl: bool = True
    extra_headers: Dict[str, str] = field(default_factory=dict)
    #: 离线自测时置 True，禁止一切真实网络访问
    offline: bool = False
    logger: Any = None
    #: 复用的 requests 会话（惰性建，只为复用 TCP/TLS 连接；重试仍由 `request` 负责）
    _session: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """惰性初始化 logger，并在 ``device_id`` 为空时生成/读取持久设备 ID。"""
        if self.logger is None:
            self.logger = get_logger()
        if not self.device_id:
            self.device_id = load_or_create_device_id()

    def get_session(self) -> Any:
        """取（必要时新建）复用的 ``requests.Session``；没装 requests 时返回 ``None``。

        Returns:
            带连接池的 Session，头是空的——每次请求仍由 :meth:`build_headers`
            现算，避免会话默认头混进请求里改变线上行为。

        Note:
            差分更新平均一段才 1–2 MiB，原来每条请求都新建连接、重做 TLS 握手；
            实测复用连接后同样一批分片吞吐提升约七成。适配器上的 ``max_retries=0``
            是故意的：重试只允许由 :meth:`request` 统一做，否则两处叠加会成倍重试。
        """
        if requests is None:
            return None
        if self._session is None:
            session = requests.Session()
            session.headers.clear()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=8,
                pool_maxsize=32,
                max_retries=0,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._session = session
        return self._session

    # ---------------------------------------------------------------- 请求头

    def build_headers(
        self, extra: Optional[Dict[str, Optional[str]]] = None
    ) -> Dict[str, str]:
        """组装请求头（注入 User-Agent / device_id 等默认头 + 调用方附加头）。

        Args:
            extra: 仅本次请求附加的额外头；会覆盖同名默认头。值为 ``None`` 时
                **删除**该头 —— 用于向非米哈游的服务端发请求时不带回米哈游专用头。

        Returns:
            完整请求头字典（含 ``extra_headers`` 与 ``extra`` 的合并结果）。
        """
        headers: Dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
            "x-rpc-device_id": self.device_id,
        }
        headers.update(self.extra_headers)
        if extra:
            for key, value in extra.items():
                if value is None:
                    headers.pop(key, None)
                else:
                    headers[key] = value
        return headers

    # ---------------------------------------------------------------- 核心

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, Optional[str]]] = None,
        stream: bool = False,
        timeout: Optional[float] = None,
        body: Optional[bytes] = None,
    ) -> HttpResponse:
        """发请求；``offline=True`` 时抛错（防止自测意外联网）。

        Args:
            method: HTTP 方法（GET/HEAD/POST/...）。
            url: 完整请求地址。
            headers: 本次请求附加头；会并入 ``build_headers`` 的结果。
            stream: ``True`` 时返回流式响应（调用方负责 ``close``）。
            timeout: 覆盖客户端默认超时的秒数；``None`` 时用 ``self.timeout``。
            body: 请求体原始字节；``None`` 表示不带体。

        Returns:
            统一封装的 ``HttpResponse``。

        Raises:
            HttpError: ``offline`` 模式直接抛；4xx（除 408/429）立即失败不重试；
                其余错误重试耗尽（默认 5 次指数退避）后仍失败则抛。
        """
        if self.offline:
            raise HttpError(f"离线模式下禁止网络请求: {mask_url_password(url)}")

        final_headers = self.build_headers(headers)
        effective_timeout = timeout if timeout is not None else self.timeout
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                if requests is not None:
                    return self._request_with_requests(
                        method, url, final_headers, stream, effective_timeout, body
                    )
                return self._request_with_urllib(
                    method, url, final_headers, stream, effective_timeout, body
                )
            except Exception as error:  # noqa: BLE001 - 统一重试
                last_error = error
                status = getattr(error, "status", None)
                # 4xx（除 408/429）不重试
                if (
                    isinstance(status, int)
                    and 400 <= status < 500
                    and status not in (408, 429)
                ):
                    raise HttpError(
                        f"HTTP {status} 请求失败: {mask_url_password(url)}", status
                    ) from error
                if attempt >= self.max_retries:
                    break
                delay = self.retry_backoff * (2 ** (attempt - 1)) + random.uniform(
                    0, 0.2
                )
                self.logger.warning(
                    "请求失败（第 %d/%d 次）：%s，%.1fs 后重试",
                    attempt,
                    self.max_retries,
                    error,
                    delay,
                )
                time.sleep(delay)

        raise HttpError(
            f"请求失败（已重试 {self.max_retries} 次）: "
            f"{mask_url_password(url)} -> {last_error}"
        )

    def _request_with_urllib(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        stream: bool,
        timeout: float,
        body: Optional[bytes] = None,
    ) -> HttpResponse:
        """用标准库 ``urllib`` 发请求并包装成 ``HttpResponse``。

        Args:
            method: HTTP 方法。
            url: 请求地址。
            headers: 已合并的最终请求头。
            stream: 是否返回流式响应。
            timeout: 本次超时秒数。
            body: 请求体原始字节；``None`` 表示不带体。

        Returns:
            非流式时直接解压并缓存 ``_body``；流式时挂上解压后的 ``_stream``（调用方须 close）。

        Note:
            流式响应不入 ``with``，因为 ``urlopen`` 上下文在退出时会关闭底层 socket。
        """
        request = urllib.request.Request(
            url, data=body, method=method.upper(), headers=headers
        )
        opener_kwargs: Dict[str, Any] = {}
        if not self.verify_ssl:  # pragma: no cover
            import ssl

            opener_kwargs["context"] = ssl._create_unverified_context()
        # 注意：流式响应不能放进 ``with`` —— ``urlopen`` 的上下文管理器在
        # __exit__ 时会关闭底层 socket，调用方就拿不到数据了。
        response = urllib.request.urlopen(request, timeout=timeout, **opener_kwargs)
        try:
            response_headers = {
                key.lower(): value for key, value in response.headers.items()
            }
            encoding = response_headers.get("content-encoding", "").lower()
            if stream:
                # 调用方负责 close（HttpResponse.close 会关掉 _stream）
                return HttpResponse(
                    status=response.status,
                    headers=response_headers,
                    _stream=_wrap_decompressor(response, encoding),
                )
            body = _decode_body(response.read(), encoding)
            return HttpResponse(
                status=response.status, headers=response_headers, _body=body
            )
        finally:
            if not stream:
                response.close()

    def _request_with_requests(  # pragma: no cover - 仅在装了 requests 时走到
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        stream: bool,
        timeout: float,
        body: Optional[bytes] = None,
    ) -> HttpResponse:
        """用 ``requests`` 发请求（装了 ``requests`` 时优先走此路径）。

        Args:
            method: HTTP 方法。
            url: 请求地址。
            headers: 已合并的最终请求头。
            stream: 是否流式响应。
            timeout: 本次超时秒数。
            body: 请求体原始字节；``None`` 表示不带体。

        Returns:
            包装后的 ``HttpResponse``。

        Raises:
            HttpError: 状态码 >= 400 时（直接关闭响应并抛出）。
        """
        session = self.get_session()
        send = session.request if session is not None else requests.request
        response = send(
            method,
            url,
            headers=headers,
            data=body,
            timeout=timeout,
            stream=stream,
            verify=self.verify_ssl,
        )
        if response.status_code >= 400:
            error = HttpError(
                f"HTTP {response.status_code}: {mask_url_password(url)}",
                response.status_code,
            )
            response.close()
            raise error
        response_headers = {
            key.lower(): value for key, value in response.headers.items()
        }
        if stream:
            response.raw.decode_content = True
            return HttpResponse(
                status=response.status_code,
                headers=response_headers,
                _stream=response.raw,
            )
        return HttpResponse(
            status=response.status_code,
            headers=response_headers,
            _body=response.content,
        )

    # ---------------------------------------------------------------- 便捷方法

    def get(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        stream: bool = False,
        timeout: Optional[float] = None,
    ) -> HttpResponse:
        """发起 GET 请求（便捷封装，等价于 ``request("GET", ...)``）。

        Args:
            url: 请求地址。
            headers: 附加请求头。
            stream: 是否流式。
            timeout: 覆盖默认超时；``None`` 用默认值。

        Returns:
            统一封装的 ``HttpResponse``。
        """
        return self.request("GET", url, headers=headers, stream=stream, timeout=timeout)

    def get_json(self, url: str, timeout: Optional[float] = None) -> Any:
        """GET 并解析 JSON 响应（读取后自动关闭响应）。

        Args:
            url: 完整请求地址。
            timeout: 覆盖客户端默认超时的秒数；``None`` 时用默认值。

        Returns:
            解析后的 JSON 对象（通常是 ``dict``）。

        Raises:
            HttpError: 请求失败时（含重试耗尽）。
            json.JSONDecodeError: 响应体不是合法 JSON 时。
        """
        response = self.get(url, timeout=timeout)
        try:
            return response.json()
        finally:
            response.close()

    def range_get(
        self, url: str, start: int, end: Optional[int] = None
    ) -> HttpResponse:
        """带 ``Range`` 头的 GET（多会话分片下载用，恒为流式）。

        Args:
            url: 请求地址。
            start: 起始字节偏移（含）。
            end: 结束字节偏移（含）；``None`` 表示 ``bytes=start-``，
                即请求从 start 到文件末尾（**不是**「不设置范围」）。

        Returns:
            流式 ``HttpResponse``（调用方须 ``close``）。
        """
        if end is None:
            value = f"bytes={start}-"
        else:
            value = f"bytes={start}-{end}"
        return self.get(url, headers={"Range": value}, stream=True)


class LauncherApiError(RuntimeError):
    pass


@dataclass
class LauncherApi:
    """一次「拉取远程元数据」的结果快照。

     暴露的那几个属性：`LauncherGameResourcePackage`、
    `LauncherGameSophonBranches`、`LauncherGameResourcePlugin`、`LauncherGameResourceSdk`。
    """

    preset: PresetConfig
    client: HttpClient

    #: game_packages[] 里匹配到本游戏的条目
    resource_package: Optional[HypResourcesData] = None
    #: game_branches[] 里匹配到本游戏的条目
    sophon_branches: Optional[HypLauncherSophonBranchesKind] = None

    #:：zip 缺失时必须走 Sophon
    is_force_redirect_to_sophon: bool = False
    logger: Any = None

    def __post_init__(self) -> None:
        """惰性初始化 logger（未传入时取默认）。"""
        if self.logger is None:
            self.logger = get_logger()

    # ------------------------------------------------------------------ 加载

    @classmethod
    def load(
        cls,
        preset: PresetConfig,
        client: Optional[HttpClient] = None,
        *,
        logger: Any = None,
    ) -> "LauncherApi":
        """并发拉取所需接口并做归一化。

        用线程池并发请求 ``getGamePackages``/``getGameBranches``，
        任一失败仅记日志不中断；最后执行 Sophon 版本伪造。

        Args:
            preset: 目标游戏区服预设。
            client: 复用的 ``HttpClient``；``None`` 时新建一个（非离线）。
            logger: 覆盖默认 logger。

        Returns:
            填充好 ``resource_package``/``sophon_branches`` 的 ``LauncherApi`` 快照。

        Raises:
            LauncherApiError: 当 ``packages`` 与 ``branches`` **两者都**拉取失败时。
        """
        client = client or HttpClient()
        api = cls(preset=preset, client=client, logger=logger or get_logger())

        tasks: Dict[str, str] = {
            "packages": preset.game_packages_url,
            "branches": preset.game_branches_url,
        }

        results: Dict[str, Any] = {}
        failures: Dict[str, str] = {}
        # I/O 密集，用线程池并发拉取
        with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as pool:
            futures = {
                pool.submit(client.get_json, url): name for name, url in tasks.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as error:  # noqa: BLE001
                    failures[name] = str(error)
                    api.logger.warning("拉取 %s 失败：%s", name, error)

        if "packages" not in results and "branches" not in results:
            raise LauncherApiError(
                "资源包与 Sophon 分支接口均拉取失败：" + "; ".join(failures.values())
            )

        if "packages" in results:
            api.resource_package = api._find_resource(results["packages"])
        if "branches" in results:
            api.sophon_branches = api._find_branch(results["branches"])

        api._initialize_fake_version_info()
        return api

    # ------------------------------------------------------------------ 查找

    def _find_resource(self, payload: Dict[str, Any]) -> Optional[HypResourcesData]:
        """在 ``game_packages[]`` 里按 biz / game_id 定位本游戏。

        Args:
            payload: 原始 ``getGamePackages`` 响应。

        Returns:
            匹配到的 ``HypResourcesData``；未命中返回 ``None``。
        """
        for item in parse_game_packages(payload):
            if self._match(item.game_info.game_biz, item.game_info.game_id):
                return item
        return None

    def _find_branch(
        self, payload: Dict[str, Any]
    ) -> Optional[HypLauncherSophonBranchesKind]:
        """在 ``game_branches[]`` 里按 biz / game_id 定位本游戏。

        Args:
            payload: 原始 ``getGameBranches`` 响应。

        Returns:
            匹配到的 ``HypLauncherSophonBranchesKind``；未命中返回 ``None``。
        """
        for item in parse_game_branches(payload):
            if self._match(item.game_info.game_biz, item.game_info.game_id):
                return item
        return None

    def _match(self, biz: str, game_id: str) -> bool:
        """判断某条目的 biz / game_id 是否命中本预设。

        Args:
            biz: 待比对业务名（对应 ``launcher_biz_name``）。
            game_id: 待比对游戏 ID（对应 ``game_id``）。

        Returns:
            ``True`` 当 biz 或 game_id 任一相等（且双方均非空）。
        """
        want_biz = self.preset.launcher_biz_name
        want_id = self.preset.game_id
        if want_biz and biz and biz == want_biz:
            return True
        if want_id and game_id and game_id == want_id:
            return True
        return False

    # --------------------------------------------------- InitializeFakeVersionInfo

    def _initialize_fake_version_info(self) -> None:
        """的等价实现（有副作用）。

        直接改写 ``self.resource_package`` 并在需要时置 ``is_force_redirect_to_sophon``。

        触发逻辑：
          * 无 Sophon 分支（``self.sophon_branches`` 为 ``None``）时直接返回，不改动。
          * 完全没有 zip 资源（``resource`` 为 ``None``，如原神 5.6+）时，置强制 Sophon 并返回。
          * Sophon 的 ``tag`` 与 zip 的 ``major.version`` **不相等**时，用 Sophon tag 伪造
            ``main``/``pre_download`` 的 ``current_version`` 并把 ``diff_tags`` 补成 ``patches``，
            同时置 ``is_force_redirect_to_sophon = True``。

        Note:
            本方法会就地修改 ``self.resource_package``，调用方不应假设其不可变。
        """
        resource = self.resource_package
        branch = self.sophon_branches
        if branch is None:
            return

        main_branch = branch.main
        preload_branch = branch.pre_download

        if resource is None:
            # 完全没有 zip 资源（原神 5.6 之后就是这样）-> 全靠 Sophon
            self.is_force_redirect_to_sophon = True
            self.logger.debug("无 zip 资源包，强制走 Sophon")
            return

        from app.services.gi_updater.common.version import GameVersion

        zip_version = (
            GameVersion.parse(resource.main_package.current_version.version)
            if resource.main_package and resource.main_package.current_version
            else None
        )
        sophon_version = GameVersion.parse(main_branch.tag) if main_branch else None

        if (
            sophon_version is not None
            and zip_version is not None
            and sophon_version == zip_version
        ):
            return

        # 伪造 pre_download
        if preload_branch is not None and resource.pre_download is None:
            resource.pre_download = _make_resource_package(
                preload_branch.tag, preload_branch.diff_tags
            )
        elif preload_branch is not None and resource.pre_download is not None:
            _add_fake_version(
                resource.pre_download, preload_branch.tag, preload_branch.diff_tags
            )

        if main_branch is None:
            return

        if resource.main_package is None:
            resource.main_package = _make_resource_package(
                main_branch.tag, main_branch.diff_tags
            )
        else:
            _add_fake_version(
                resource.main_package, main_branch.tag, main_branch.diff_tags
            )
        self.is_force_redirect_to_sophon = True


def _make_resource_package(tag: str, diff_tags: Optional[List[str]]):
    """构造一个「伪造」的 ``HypResourcePackageData``（仅含 Sophon 提供的版本信息）。

    Args:
        tag: Sophon 分支的版本 tag，用作 ``current_version``。
        diff_tags: 差分 tag 列表，逐一补成 ``patches``。

    Returns:
        ``HypResourcePackageData``：``current_version`` 取 ``tag``，``patches`` 由 ``diff_tags`` 补成。
    """
    from app.services.gi_updater.api.models import HypResourcePackageData

    package = HypResourcePackageData()
    _add_fake_version(package, tag, diff_tags)
    return package


def _add_fake_version(package, tag: str, diff_tags: Optional[List[str]]) -> None:
    """的等价实现（就地修改 ``package``）。

    Args:
        package: 目标 ``HypResourcePackageData``（被就地修改，无返回值）。
        tag: Sophon 版本 tag，缺失 ``current_version`` 时用来填充。
        diff_tags: 差分 tag 列表，去重后追加为 ``patches``。

    Note:
        本函数就地修改 ``package``（填充 ``current_version``、追加 ``patches``），不返回新对象。
    """
    from app.services.gi_updater.api.models import HypPackageInfo

    if package.current_version is None or not package.current_version.version:
        package.current_version = HypPackageInfo(version=tag or "")

    existing = {patch.version for patch in package.patches}
    for diff_tag in diff_tags or []:
        if diff_tag not in existing:
            package.patches.append(HypPackageInfo(version=diff_tag))


# --------------------------------------------------------------------------- #
# 工具：宽松取值（服务端同一字段可能给数字，也可能给字符串）
# --------------------------------------------------------------------------- #


def _int(value: Any, default: int = 0) -> int:
    """把任意值宽松转成 ``int``（宽松取值助手）。

    用于解析服务端可能以字符串/缺省形式返回的整数字段。

    Args:
        value: 原始值；``None`` 直接返回 default。
        default: 解析失败或值为 ``None`` 时的回退值。

    Returns:
        转换后的整数；任何异常（类型不符、空串）都会被吞掉返回 ``default``。
    """
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _str(value: Any, default: str = "") -> str:
    """把任意值宽松转成 ``str``（宽松取值助手）。

    Args:
        value: 原始值；``None`` 直接返回 default。
        default: 值为 ``None`` 时的回退值。

    Returns:
        转换后的字符串；``None`` 时返回 ``default``（不会抛异常）。
    """
    if value is None:
        return default
    return str(value)


def _bool(value: Any, default: bool = False) -> bool:
    """把任意值宽松转成 ``bool``（宽松取值助手）。

    除显式布尔外，仅 ``"1"/"true"/"yes"/"on"``（大小写不敏感）视为真。

    Args:
        value: 原始值；``None`` 直接返回 default。
        default: 非真值字符串或 ``None`` 时的回退值。

    Returns:
        判定结果；类型不符或空串等异常均被吞掉返回 ``default``。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# getGamePackages
# --------------------------------------------------------------------------- #


@dataclass
class HypGameInfoData:
    """``game_packages[].game``。"""

    game_id: str = ""
    game_biz: str = ""
    name: str = ""

    @classmethod
    def from_json(cls, data: Optional[Dict[str, Any]]) -> "HypGameInfoData":
        """从 ``game_packages[].game`` 节点构造。

        Args:
            data: 原始 JSON 对象；``None`` 时按空对象处理，字段全部回退默认。

        Returns:
            解析后的 ``HypGameInfoData``；``display`` 缺失时 ``name`` 为空串。
        """
        data = data or {}
        display = data.get("display") or {}
        return cls(
            game_id=_str(data.get("id")),
            game_biz=_str(data.get("biz")),
            name=_str(display.get("name")),
        )


@dataclass
class HypPackageData:
    """单个下载包。"""

    url: str = ""
    path: str = ""
    md5: str = ""
    size: int = 0
    language: str = ""
    version: str = ""

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "HypPackageData":
        """从单个下载包节点构造。

        Args:
            data: 原始 JSON 对象。

        Returns:
            解析后的 ``HypPackageData``。
        """
        return cls(
            url=_str(data.get("url")),
            path=_str(data.get("path")),
            md5=_str(data.get("md5")),
            size=_int(data.get("size")),
            language=_str(data.get("language")),
            version=_str(data.get("version")),
        )


@dataclass
class HypPackageInfo:
    """一个版本（major 或某个 patch）的全部包。"""

    version: str = ""
    game_packages: List[HypPackageData] = field(default_factory=list)
    audio_packages: List[HypPackageData] = field(default_factory=list)
    resource_list_url: str = ""

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "HypPackageInfo":
        """从单个版本节点构造（对应一个 major 或 patch）。

        Args:
            data: 原始 JSON 对象。

        Returns:
            解析后的 ``HypPackageInfo``；``game_pkgs``/``audio_pkgs`` 缺失时为空列表。
        """
        return cls(
            version=_str(data.get("version")),
            game_packages=[
                HypPackageData.from_json(x) for x in (data.get("game_pkgs") or [])
            ],
            audio_packages=[
                HypPackageData.from_json(x) for x in (data.get("audio_pkgs") or [])
            ],
            resource_list_url=_str(data.get("res_list_url")),
        )


@dataclass
class HypResourcePackageData:
    """``major`` / ``pre_download`` 容器。"""

    current_version: Optional[HypPackageInfo] = None
    patches: List[HypPackageInfo] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: Optional[Dict[str, Any]]) -> "HypResourcePackageData":
        """从 ``major``/``pre_download`` 容器节点构造。

        Args:
            data: 原始 JSON 对象；``None`` 时按空对象处理。

        Returns:
            解析后的 ``HypResourcePackageData``；``major`` 缺失则 ``current_version`` 为 ``None``。
        """
        data = data or {}
        return cls(
            current_version=(
                HypPackageInfo.from_json(data["major"]) if data.get("major") else None
            ),
            patches=[HypPackageInfo.from_json(x) for x in (data.get("patches") or [])],
        )


@dataclass
class HypResourcesData:
    """``game_packages[]`` 的元素。"""

    game_info: HypGameInfoData = field(default_factory=HypGameInfoData)
    main_package: Optional[HypResourcePackageData] = None
    pre_download: Optional[HypResourcePackageData] = None

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "HypResourcesData":
        """从 ``game_packages[]`` 元素构造。

        Args:
            data: 原始 JSON 对象。

        Returns:
            解析后的 ``HypResourcesData``；``main``/``pre_download`` 缺失时为 ``None``。
        """
        return cls(
            game_info=HypGameInfoData.from_json(data.get("game")),
            main_package=(
                HypResourcePackageData.from_json(data["main"])
                if data.get("main")
                else None
            ),
            pre_download=(
                HypResourcePackageData.from_json(data["pre_download"])
                if data.get("pre_download")
                else None
            ),
        )


@dataclass
class GamePackageResult:
    """归一化后的「本次要下的包」。"""

    main_package: List[HypPackageData] = field(default_factory=list)
    audio_package: List[HypPackageData] = field(default_factory=list)
    uncompressed_url: str = ""
    version: str = ""


def parse_game_packages(payload: Dict[str, Any]) -> List[HypResourcesData]:
    """解析 ``getGamePackages`` 响应，返回 ``game_packages[]``。

    Args:
        payload: 接口原始 JSON 响应。

    Returns:
        每个元素的 ``HypResourcesData`` 列表；``data.game_packages`` 缺失时为空列表。
    """
    data = (payload or {}).get("data") or {}
    return [
        HypResourcesData.from_json(item) for item in (data.get("game_packages") or [])
    ]


# --------------------------------------------------------------------------- #
# getGameBranches
# --------------------------------------------------------------------------- #


@dataclass
class HypGameInfoBranchData:
    """一个分支（main / pre_download）。"""

    package_id: str = ""
    branch: str = ""
    password: str = ""
    tag: str = ""
    diff_tags: List[str] = field(default_factory=list)

    @classmethod
    def from_json(
        cls, data: Optional[Dict[str, Any]]
    ) -> Optional["HypGameInfoBranchData"]:
        """从单个分支节点构造（对应 main/pre_download）。

        Args:
            data: 原始 JSON 对象；``None``/空时返回 ``None``。

        Returns:
            解析后的分支信息；输入为空时返回 ``None``。
        """
        if not data:
            return None
        return cls(
            package_id=_str(data.get("package_id")),
            branch=_str(data.get("branch")),
            password=_str(data.get("password")),
            tag=_str(data.get("tag")),
            diff_tags=[_str(x) for x in (data.get("diff_tags") or [])],
        )


@dataclass
class HypLauncherSophonBranchesKind:
    """``game_branches[]`` 的元素。"""

    game_info: HypGameInfoData = field(default_factory=HypGameInfoData)
    main: Optional[HypGameInfoBranchData] = None
    pre_download: Optional[HypGameInfoBranchData] = None

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "HypLauncherSophonBranchesKind":
        """从 ``game_branches[]`` 元素构造。

        Args:
            data: 原始 JSON 对象。

        Returns:
            解析后的分支容器；``main``/``pre_download`` 缺失时为 ``None``。
        """
        return cls(
            game_info=HypGameInfoData.from_json(data.get("game")),
            main=HypGameInfoBranchData.from_json(data.get("main")),
            pre_download=HypGameInfoBranchData.from_json(data.get("pre_download")),
        )


def parse_game_branches(payload: Dict[str, Any]) -> List[HypLauncherSophonBranchesKind]:
    """解析 ``getGameBranches`` 响应，返回 ``game_branches[]``。

    Args:
        payload: 接口原始 JSON 响应。

    Returns:
        每个元素的 ``HypLauncherSophonBranchesKind`` 列表；``game_branches`` 缺失时为空列表。
    """
    data = (payload or {}).get("data") or {}
    return [
        HypLauncherSophonBranchesKind.from_json(item)
        for item in (data.get("game_branches") or [])
    ]


# --------------------------------------------------------------------------- #
# getBuild
# --------------------------------------------------------------------------- #


@dataclass
class SophonManifestFileInfo:
    """``manifest``。"""

    id: str = ""
    checksum: str = ""
    compressed_size: int = 0
    uncompressed_size: int = 0

    @classmethod
    def from_json(cls, data: Optional[Dict[str, Any]]) -> "SophonManifestFileInfo":
        """从 ``manifest`` 节点构造。

        Args:
            data: 原始 JSON 对象；``None`` 时按空对象处理。

        Returns:
            解析后的清单文件信息。
        """
        data = data or {}
        return cls(
            id=_str(data.get("id")),
            checksum=_str(data.get("checksum")),
            compressed_size=_int(data.get("compressed_size")),
            uncompressed_size=_int(data.get("uncompressed_size")),
        )


@dataclass
class SophonManifestUrlInfo:
    """``manifest_download`` / ``chunk_download``。"""

    url_prefix: str = ""
    url_suffix: str = ""
    password: str = ""
    is_encrypted: bool = False
    is_compressed: bool = False

    @classmethod
    def from_json(cls, data: Optional[Dict[str, Any]]) -> "SophonManifestUrlInfo":
        """从下载端点节点构造（manifest/chunk 下载 URL 组）。

        Args:
            data: 原始 JSON 对象；``None`` 时按空对象处理。

        Returns:
            解析后的 URL 信息。

        Note:
            字段名是有意重映射：JSON 的 ``encryption`` -> ``is_encrypted``、
            ``compression`` -> ``is_compressed``，易写反，对照时务必留意。
        """
        data = data or {}
        return cls(
            url_prefix=_str(data.get("url_prefix")),
            url_suffix=_str(data.get("url_suffix")),
            password=_str(data.get("password")),
            is_encrypted=_bool(data.get("encryption")),
            is_compressed=_bool(data.get("compression")),
        )


@dataclass
class SophonManifestChunkInfo:
    """``stats`` / ``deduplicated_stats``。"""

    compressed_size: int = 0
    uncompressed_size: int = 0
    file_count: int = 0
    chunk_count: int = 0

    @classmethod
    def from_json(cls, data: Optional[Dict[str, Any]]) -> "SophonManifestChunkInfo":
        """从 ``stats``/``deduplicated_stats`` 节点构造。

        Args:
            data: 原始 JSON 对象；``None`` 时按空对象处理。

        Returns:
            解析后的分块统计信息。
        """
        data = data or {}
        return cls(
            compressed_size=_int(data.get("compressed_size")),
            uncompressed_size=_int(data.get("uncompressed_size")),
            file_count=_int(data.get("file_count")),
            chunk_count=_int(data.get("chunk_count")),
        )


@dataclass
class SophonManifestBuildIdentity:
    """—— ``data.manifests[]`` 的元素。

    额外保留 ``matching_field`` / ``category_id`` / ``category_name``
    （这三个字段定义在 ``SophonIdentifiableProperty`` 里）。
    """

    matching_field: str = ""
    category_id: int = 0
    category_name: str = ""
    manifest: SophonManifestFileInfo = field(default_factory=SophonManifestFileInfo)
    manifest_download: SophonManifestUrlInfo = field(
        default_factory=SophonManifestUrlInfo
    )
    chunk_download: SophonManifestUrlInfo = field(default_factory=SophonManifestUrlInfo)
    stats: SophonManifestChunkInfo = field(default_factory=SophonManifestChunkInfo)
    deduplicated_stats: SophonManifestChunkInfo = field(
        default_factory=SophonManifestChunkInfo
    )

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "SophonManifestBuildIdentity":
        """从 ``data.manifests[]`` 元素构造。

        Args:
            data: 原始 JSON 对象。

        Returns:
            解析后的清单条目；``matching_field`` 等来自 ``SophonIdentifiableProperty``。
        """
        return cls(
            matching_field=_str(data.get("matching_field")),
            category_id=_int(data.get("category_id")),
            category_name=_str(data.get("category_name")),
            manifest=SophonManifestFileInfo.from_json(data.get("manifest")),
            manifest_download=SophonManifestUrlInfo.from_json(
                data.get("manifest_download")
            ),
            chunk_download=SophonManifestUrlInfo.from_json(data.get("chunk_download")),
            stats=SophonManifestChunkInfo.from_json(data.get("stats")),
            deduplicated_stats=SophonManifestChunkInfo.from_json(
                data.get("deduplicated_stats")
            ),
        )


@dataclass
class SophonManifestBuildBranch:
    """``getBuild`` 响应整体。"""

    retcode: int = 0
    message: str = ""
    build_id: str = ""
    tag: str = ""
    manifests: List[SophonManifestBuildIdentity] = field(default_factory=list)

    @property
    def is_success(self) -> bool:
        """成功标志：``retcode == 0`` 且 ``manifests`` 非空（服务端正常返回清单）。"""
        return self.retcode == 0 and bool(self.manifests)

    def find(self, matching_field: str) -> Optional[SophonManifestBuildIdentity]:
        """按 ``matching_field`` 定位首个命中的清单条目。

        Args:
            matching_field: 要匹配的标识串（如 ``"game"``）。

        Returns:
            首个命中条目；未命中返回 ``None``。
        """
        for identity in self.manifests:
            if identity.matching_field == matching_field:
                return identity
        return None


def parse_build(payload: Dict[str, Any]) -> SophonManifestBuildBranch:
    """解析 ``getBuild`` 响应。

    Args:
        payload: 接口原始 JSON 响应。

    Returns:
        整体构建分支信息（含 ``retcode``/``tag``/``manifests``）。
    """
    payload = payload or {}
    data = payload.get("data") or {}
    return SophonManifestBuildBranch(
        retcode=_int(payload.get("retcode")),
        message=_str(payload.get("message")),
        build_id=_str(data.get("build_id")),
        tag=_str(data.get("tag")),
        manifests=[
            SophonManifestBuildIdentity.from_json(item)
            for item in (data.get("manifests") or [])
        ],
    )


# --------------------------------------------------------------------------- #
# getBuild (patch / 差分)
# --------------------------------------------------------------------------- #


@dataclass
class SophonManifestPatchIdentity:
    """差分清单条目。"""

    matching_field: str = ""
    category_id: int = 0
    category_name: str = ""
    manifest: SophonManifestFileInfo = field(default_factory=SophonManifestFileInfo)
    manifest_download: SophonManifestUrlInfo = field(
        default_factory=SophonManifestUrlInfo
    )
    diff_download: SophonManifestUrlInfo = field(default_factory=SophonManifestUrlInfo)
    #: version tag -> stats
    diff_tagged_info: Dict[str, SophonManifestChunkInfo] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "SophonManifestPatchIdentity":
        """从差分清单条目构造。

        Args:
            data: 原始 JSON 对象。

        Returns:
            解析后的差分条目；``stats`` 被重建成 ``version tag -> 分块统计`` 的映射。
        """
        tagged = data.get("stats") or {}
        return cls(
            matching_field=_str(data.get("matching_field")),
            category_id=_int(data.get("category_id")),
            category_name=_str(data.get("category_name")),
            manifest=SophonManifestFileInfo.from_json(data.get("manifest")),
            manifest_download=SophonManifestUrlInfo.from_json(
                data.get("manifest_download")
            ),
            diff_download=SophonManifestUrlInfo.from_json(data.get("diff_download")),
            diff_tagged_info={
                _str(key): SophonManifestChunkInfo.from_json(value)
                for key, value in tagged.items()
            }
            if isinstance(tagged, dict)
            else {},
        )


@dataclass
class SophonManifestPatchBranch:
    """差分 ``getBuild`` 响应。"""

    retcode: int = 0
    message: str = ""
    build_id: str = ""
    tag: str = ""
    patch_id: str = ""
    manifests: List[SophonManifestPatchIdentity] = field(default_factory=list)

    @property
    def is_success(self) -> bool:
        """成功标志：``retcode == 0`` 且 ``manifests`` 非空。"""
        return self.retcode == 0 and bool(self.manifests)

    def find(self, matching_field: str) -> Optional[SophonManifestPatchIdentity]:
        """按 ``matching_field`` 定位差分清单条目。

        Args:
            matching_field: 要匹配的标识串。

        Returns:
            首个命中条目；未命中返回 ``None``。
        """
        for identity in self.manifests:
            if identity.matching_field == matching_field:
                return identity
        return None


def parse_patch_build(payload: Dict[str, Any]) -> SophonManifestPatchBranch:
    """解析差分 ``getBuild`` 响应。

    Args:
        payload: 接口原始 JSON 响应。

    Returns:
        差分构建分支信息（含 ``patch_id``/``manifests``）。
    """
    payload = payload or {}
    data = payload.get("data") or {}
    return SophonManifestPatchBranch(
        retcode=_int(payload.get("retcode")),
        message=_str(payload.get("message")),
        build_id=_str(data.get("build_id")),
        tag=_str(data.get("tag")),
        patch_id=_str(data.get("patch_id")),
        manifests=[
            SophonManifestPatchIdentity.from_json(item)
            for item in (data.get("manifests") or [])
        ],
    )
