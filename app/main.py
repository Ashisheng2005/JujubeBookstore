"""FastAPI 入口。

漫画源（图片阅读型）只做三件事（外加一个最新更新列表）：

* ``GET /api/{source}/search``                     搜索
* ``GET /api/{source}/comic/{comic_id}``           章节列表
* ``GET /api/{source}/comic/{id}/chapter/{cid}``   图片直链
* ``GET /api/{source}/latest``                     最近更新（探索页用）
* ``GET /api/{source}/image``                      图片中转（绕开防盗链/CORS）

资源索引源（BT/磁力）是另一套模型，走独立命名空间：

* ``GET /api/resources``                                    可用资源源
* ``GET /api/resources/{source}/search``                    搜索资源
* ``GET /api/resources/{source}/latest``                    最新发布
* ``GET /api/resources/{source}/item/{item_id}``            磁力 / 种子 / 文件列表
"""

from __future__ import annotations

import contextlib
from pathlib import Path as FilePath
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from . import __version__
from .cache import TTLCache
from .config import Settings, load_settings
from .errors import NotFoundError, ProxyRequiredError, SourceError
from .http_client import HttpClientPool
from .downloads.api import router as download_router
from .downloads.manager import DownloadManager
from .downloads.repository import DownloadRepository
from .resources import RESOURCE_CLASSES, create_resource
from .resources import available_keys as resource_keys
from .schemas import (
    ChapterImages,
    ComicDetail,
    ComicList,
    ResourceDetail,
    ResourceList,
    ResourceSearchResult,
    SearchResult,
    SourceInfo,
)
from .sources import SOURCE_CLASSES, available_keys, create_source


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    _init_state(app)
    settings = app.state.settings
    root = FilePath(settings.download_dir).resolve()
    repository = DownloadRepository(FilePath(settings.download_db) if settings.download_db else root / "downloads.db")
    try:
        await repository.open()
        app.state.downloads = DownloadManager(repository, app.state.pool, settings)
        await app.state.downloads.start()
        yield
    finally:
        if getattr(app.state, "downloads", None) is not None:
            await app.state.downloads.stop()
            app.state.downloads = None
        await repository.close()
        with contextlib.suppress(Exception):
            await app.state.pool.aclose()
        app.state.pool = None


app = FastAPI(
    title="JujubeBookstore Manga API",
    version=__version__,
    description="国漫/汉化漫画抓取服务：搜索、章节列表、图片直链",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(download_router)


def _init_state(app: FastAPI) -> None:
    """延迟初始化运行时状态，测试里直接调用 API 也不会炸。"""
    state = app.state
    if getattr(state, "pool", None) is not None:
        return
    settings = load_settings()
    state.settings = settings
    state.pool = HttpClientPool(settings)
    state.detail_cache = TTLCache[ComicDetail](settings.detail_ttl)
    state.chapter_cache = TTLCache[ChapterImages](settings.chapter_ttl)


def _state(request: Request) -> tuple[Settings, HttpClientPool, TTLCache, TTLCache]:
    _init_state(request.app)
    state = request.app.state
    return state.settings, state.pool, state.detail_cache, state.chapter_cache


def _source_or_404(request: Request, source: str):
    _, pool, _, _ = _state(request)
    try:
        return create_source(source, pool)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"未知的漫画源 {source!r}，可用: {', '.join(available_keys())}",
        ) from None


def _translate(exc: SourceError) -> HTTPException:
    if isinstance(exc, ProxyRequiredError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


#: 内置前端（单文件、零依赖，和后端同一个进程）
WEB_DIR = FilePath(__file__).resolve().parent.parent / "web"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def console() -> HTMLResponse:
    """控制台页面：搜索、章节、图片、磁力资源。"""
    index = WEB_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="前端文件缺失：web/index.html")
    return HTMLResponse(index.read_text(encoding="utf-8"))


def _resource_or_404(request: Request, source: str):
    _, pool, _, _ = _state(request)
    try:
        return create_resource(source, pool)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"未知的资源源 {source!r}，可用: {', '.join(resource_keys())}",
        ) from None


@app.get("/healthz", summary="健康检查")
async def healthz(request: Request) -> dict[str, Any]:
    settings, _, _, _ = _state(request)
    return {
        "status": "ok",
        "version": __version__,
        "sources": available_keys(),
        "resource_sources": resource_keys(),
        "proxy_enabled": settings.proxy_enabled,
    }


@app.get("/api/sources", response_model=list[SourceInfo], summary="可用漫画源")
async def list_sources(request: Request) -> list[SourceInfo]:
    _, pool, _, _ = _state(request)
    return [SourceInfo(**create_source(key, pool).info()) for key in available_keys()]


@app.get("/api/resources", response_model=list[SourceInfo], summary="可用资源索引源（BT/磁力）")
async def list_resource_sources(request: Request) -> list[SourceInfo]:
    _, pool, _, _ = _state(request)
    return [SourceInfo(**create_resource(key, pool).info()) for key in resource_keys()]


@app.get("/api/{source}/search", response_model=SearchResult, summary="搜索漫画")
async def search(
    request: Request,
    source: str = Path(description="漫画源标识，见 /api/sources"),
    q: str = Query(min_length=1, description="关键词"),
    page: int = Query(1, ge=1),
) -> SearchResult:
    comic_source = _source_or_404(request, source)
    try:
        items = await comic_source.search(q, page=page)
    except SourceError as exc:
        raise _translate(exc) from exc
    return SearchResult(source=source, keyword=q, page=page, count=len(items), items=items)


@app.get("/api/{source}/latest", response_model=ComicList, summary="最近更新")
async def latest(
    request: Request,
    source: str = Path(description="漫画源标识"),
    page: int = Query(1, ge=1),
) -> ComicList:
    comic_source = _source_or_404(request, source)
    loader = getattr(comic_source, "latest", None)
    if loader is None:
        raise HTTPException(status_code=501, detail=f"源 {source!r} 未实现最近更新列表")
    try:
        items = await loader(page=page)
    except SourceError as exc:
        raise _translate(exc) from exc
    return ComicList(source=source, page=page, count=len(items), items=items)


@app.get("/api/{source}/comic/{comic_id}", response_model=ComicDetail, summary="漫画详情与章节列表")
async def comic_detail(
    request: Request,
    source: str = Path(description="漫画源标识"),
    comic_id: str = Path(description="漫画 id，来自搜索/最近更新的 items[].id"),
) -> ComicDetail:
    comic_source = _source_or_404(request, source)
    _, _, detail_cache, _ = _state(request)
    cache_key = f"{source}:{comic_id}"
    cached = detail_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        detail = await comic_source.detail(comic_id)
    except SourceError as exc:
        raise _translate(exc) from exc
    detail_cache.set(cache_key, detail)
    return detail


@app.get(
    "/api/{source}/comic/{comic_id}/chapter/{chapter_id}",
    response_model=ChapterImages,
    summary="章节图片直链",
)
async def chapter_images(
    request: Request,
    source: str = Path(description="漫画源标识"),
    comic_id: str = Path(description="漫画 id"),
    chapter_id: str = Path(description="章节 id，来自详情 chapters[].id"),
) -> ChapterImages:
    comic_source = _source_or_404(request, source)
    _, _, _, chapter_cache = _state(request)
    cache_key = f"{source}:{comic_id}:{chapter_id}"
    cached = chapter_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        images = await comic_source.chapter(comic_id, chapter_id)
    except SourceError as exc:
        raise _translate(exc) from exc
    chapter_cache.set(cache_key, images)
    return images


@app.get("/api/{source}/image", summary="图片中转（可选）")
async def image_proxy(
    request: Request,
    source: str = Path(description="漫画源标识"),
    url: str = Query(description="图片直链，必须属于该源允许的域名"),
) -> Response:
    comic_source = _source_or_404(request, source)
    settings, _, _, _ = _state(request)
    if not settings.image_proxy_enabled:
        raise HTTPException(status_code=403, detail="图片中转已通过 JUJUBE_IMAGE_PROXY=0 关闭")

    host = (urlparse(url).hostname or "").lower()
    if urlparse(url).scheme not in {"http", "https"} or not host:
        raise HTTPException(status_code=400, detail="图片地址非法")
    if not comic_source.allows_image_host(host):
        raise HTTPException(status_code=403, detail=f"域名 {host} 不在该源允许列表内")

    headers = {"Referer": comic_source.image_referer} if comic_source.image_referer else {}
    try:
        upstream = await comic_source.client.get(url, headers=headers)
        upstream.raise_for_status()
    except Exception as exc:  # httpx 异常种类多，统一按上游失败处理
        raise HTTPException(status_code=502, detail=f"拉取图片失败: {exc}") from exc

    content = upstream.content
    if len(content) > settings.image_proxy_max_bytes:
        raise HTTPException(status_code=413, detail="图片超过中转大小上限")
    return Response(
        content=content,
        media_type=upstream.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=3600"},
    )


# --- 资源索引源（BT / 磁力） ------------------------------------------------
@app.get(
    "/api/resources/{source}/search",
    response_model=ResourceSearchResult,
    summary="搜索资源（BT/磁力）",
)
async def resource_search(
    request: Request,
    source: str = Path(description="资源源标识，见 /api/resources"),
    q: str = Query(min_length=1, description="关键词；留空请用 /latest"),
    page: int = Query(1, ge=1),
    category: str | None = Query(None, description="分类名或 sort_id，例如 動畫/漫畫/游戏/3"),
) -> ResourceSearchResult:
    resource_source = _resource_or_404(request, source)
    try:
        items = await resource_source.search(q, page=page, category=category)
    except SourceError as exc:
        raise _translate(exc) from exc
    return ResourceSearchResult(
        source=source, keyword=q, page=page, category=category, count=len(items), items=items
    )


@app.get(
    "/api/resources/{source}/latest",
    response_model=ResourceList,
    summary="最新发布",
)
async def resource_latest(
    request: Request,
    source: str = Path(description="资源源标识"),
    page: int = Query(1, ge=1),
    category: str | None = Query(None, description="分类名或 sort_id"),
) -> ResourceList:
    resource_source = _resource_or_404(request, source)
    try:
        items = await resource_source.latest(page=page, category=category)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except SourceError as exc:
        raise _translate(exc) from exc
    return ResourceList(source=source, page=page, count=len(items), items=items)


@app.get(
    "/api/resources/{source}/item/{item_id}",
    response_model=ResourceDetail,
    summary="资源详情：磁力 / 种子 / 文件列表",
)
async def resource_detail(
    request: Request,
    source: str = Path(description="资源源标识"),
    item_id: str = Path(description="资源 id（列表里的 id 或 detail_url 的路径）"),
) -> ResourceDetail:
    resource_source = _resource_or_404(request, source)
    _, _, detail_cache, _ = _state(request)
    cache_key = f"resource:{source}:{item_id}"
    cached = detail_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        detail = await resource_source.detail(item_id)
    except SourceError as exc:
        raise _translate(exc) from exc
    detail_cache.set(cache_key, detail)
    return detail


__all__ = ["app", "RESOURCE_CLASSES", "SOURCE_CLASSES"]
