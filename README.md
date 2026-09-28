# JujubeBookstore

国漫 / 汉化漫画抓取服务。后端只做三件事：**搜索**、**章节列表**、**图片直链**（另附最近更新与防盗链图片中转）。

- 技术栈：Python 3.12 + FastAPI + httpx
- 解析逻辑与 HTTP 层、站点元数据分离：新增站点 = 加一个源类 + 注册一行
- 三个源都已**真实联网验证**，不是纸面接口
- 48 项离线单测（fixtures 是真实抓取的响应片段）+ 真实联网联调脚本

## 已实现的源

| key | 站点 | 代理 | 搜索 | 章节列表 | 图片直链 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| `zaimanhua` | 再漫画 | 直连 | ✅ 20 条/页 | ✅ 264 章实测 | ✅ | App v4 API；部分章节需登录/VIP（`canRead=false`） |
| `mangabz` | Mangabz | 有代理优先 | ✅ 12 条 | ✅ 975 章实测 | ✅ 20/96 页实测 | HTML + 打包 JS 图片接口，全免费 |
| `mangacopy` | 拷贝漫画 | **必须** | ✅ 21 条/页（total 247） | ✅ | ✅ 6 页 webp 实测 | 官方 App v3 API，需 `JUJUBE_PROXY` |

实测样例：三源各跑一遍 `搜索 → 详情 → 章节图片 → 图片中转`，均返回 200 且图片格式校验通过
（再漫画 JPEG 192KB、Mangabz JPEG 96KB、拷贝漫画 WebP 353KB）。

## 快速开始

```bash
pip install -r requirements.txt

# 真实联网联调：搜索 -> 详情 -> 章节 -> 下载首图校验格式
python scripts/smoke.py 海贼王 --source mangabz
python scripts/smoke.py 火影   --source zaimanhua

# 拷贝漫画需要代理
export JUJUBE_PROXY=http://127.0.0.1:7897
python scripts/smoke.py 火影忍者 --source mangacopy

# 启动服务
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

交互式文档：<http://127.0.0.1:8000/docs>

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查：已注册源、代理开关 |
| GET | `/api/sources` | 可用漫画源（含 `needs_proxy` / `prefer_proxy`） |
| GET | `/api/{source}/search?q=&page=1` | 搜索 |
| GET | `/api/{source}/latest?page=1` | 最近更新（探索页） |
| GET | `/api/{source}/comic/{comic_id}` | 详情 + 章节列表 |
| GET | `/api/{source}/comic/{comic_id}/chapter/{chapter_id}` | 章节图片直链 |
| GET | `/api/{source}/image?url=` | 图片中转（白名单校验，防 SSRF） |

```bash
curl "http://127.0.0.1:8000/api/mangabz/search?q=%E6%B5%B7%E8%B4%BC%E7%8E%8B"
curl "http://127.0.0.1:8000/api/mangabz/comic/139"
curl "http://127.0.0.1:8000/api/mangabz/comic/139/chapter/29397"
```

响应用统一结构，站点原始字段不外泄：

```json
{
  "source": "mangabz",
  "keyword": "海贼王",
  "page": 1,
  "count": 12,
  "items": [
    {"id": "139", "title": "海贼王", "cover": "https://cover.mangabz.com/…",
     "authors": [], "tags": [], "status": null, "description": null}
  ]
}
```

错误约定：`404` 资源不存在/未知源，`502` 上游异常，`503` 该源需要代理但未配置 `JUJUBE_PROXY`。

> 图片直链普遍带签名与过期时间（如再漫画的 `?sign=…&t=…`），客户端应及时消费；服务端章节缓存默认 300s，避免下发失效链接。

## 代理配置

代理统一走 `JUJUBE_PROXY`，源用两个标记声明自己的需求：

| 标记 | 语义 |
| --- | --- |
| `needs_proxy = True` | 必须走代理（拷贝漫画）。未配置代理时返回 **503** 并提示设置 `JUJUBE_PROXY` |
| `prefer_proxy = True` | 直连可用但不稳（Mangabz）。配了代理就走代理，没配就直连 |

其余配置见 `.env.example`（超时、重试次数、缓存 TTL、图片中转开关与大小上限）。

## 项目结构

```
app/
  main.py            FastAPI 路由、错误翻译、缓存接入、图片中转
  config.py          JUJUBE_* 环境变量配置
  http_client.py     httpx 客户端池：直连/代理两套 + 幂等 GET 重试 + SSL 上下文复用
  cache.py           极简 TTL 缓存
  schemas.py         对外统一数据结构
  errors.py          SourceError / NotFoundError / ProxyRequiredError
  sources/
    base.py          ComicSource 抽象：search / detail / chapter
    zaimanhua.py     再漫画（App v4 API）
    mangabz.py       Mangabz（HTML + chapterimage.ashx，章节并发抓取）
    mangacopy.py     拷贝漫画（App v3 API，节点轮换）
    packer.py        Dean Edwards 打包 JS 解包器（纯标准库）
scripts/smoke.py     真实联网联调脚本
tests/               48 项离线单测（fixtures 为真实抓取的响应片段）
```

## 新增一个源

1. 继承 `ComicSource`，实现 `search` / `detail` / `chapter`，返回 `schemas` 里的统一结构；
2. 声明 `key` / `name` / `needs_proxy` / `prefer_proxy` / `image_hosts`（或 `image_host_patterns`）/ `image_referer`；
3. 在 `app/sources/__init__.py` 的 `SOURCE_CLASSES` 注册；
4. 解析错误统一抛 `SourceError` / `NotFoundError`，API 层会自动翻译成 502 / 404。

## 测试

```bash
python -m pytest                                  # 48 项，全离线，约 3s
python scripts/smoke.py 海贼王 --source mangabz    # 真实联网
```

## 站点调研与踩坑记录

### 本机网络实测（无代理时）

| 站点 | 直连 | 结论 |
| --- | --- | --- |
| 再漫画 | 200，0.1s | 已实现 |
| Mangabz | 200，但 8~15s 且偶发超时 | 已实现（配代理优先） |
| 拷贝漫画 | 不通 | 已实现（必须走代理） |
| 极速漫画 1kkk / 快看漫画 / 风车漫画 / 漫画屋 | 200 | 待实现 |
| 动漫之家 / 看漫画 manhuagui | 不通 | 需代理 |

### 拷贝漫画（踩坑最多，值得单独记）

1. **`platform` 请求头是开关**：缺了它搜索恒返回 `total=0`（不是被墙、不是关键词问题，加头后 21 条/页）。
2. **域名换过**：`api.mangacopy.com` 现在对第三方返回 `code=210 請升級到最新的APP(3.0.0)`；
   可用的是 `api.2024manga.com`（搜索 total 247，索引最全）与 `www.mangacopy.com`（total 27），已做节点轮换。
3. **详情与章节是两套接口**：`comic2/{path_word}` 返回的 `groups` 里章节常常是空的，
   真正章节要再打 `comic/{pw}/group/{group}/chapters`（每页上限 100，需翻页）。
4. **图片端点与文档不符**：文档写 `chapter{version}`（如 `chapter2/{uuid}`），实测在 `api.2024manga.com` 上是 404，
   要用 `comic/{pw}/chapter/{uuid}`。
5. **最近更新**：文档的 `update/newest` 已 404，改用 `comics?ordering=-datetime_updated`。
6. 图片 CDN 是泛域名（`sh.mangafunb.fun` 之类），所以白名单用**正则**而不是固定域名。

### 再漫画

1. **详情必须带 `?_v=2.2.5`**：用 `?channel=android` 时大量漫画返回 `errno=2 漫画不存在或已被删除`
   （实为 android 渠道的版权过滤），同一批 id 实测 0/6 与 6/6 的差异就来自这一个参数。
2. **id 映射**：搜索结果的 `comic_id` 恒为 0、最近更新的 `id` 恒为 0，必须取非 0 的那个。
3. 大量章节是 VIP/需登录（`canRead=false`），代码会明确区分并报错，而不是假装成功。

### Mangabz

1. 图片接口返回 Dean Edwards 打包 JS：解包后取 `pix` 前缀 + 路径数组；`packer.py` 为纯标准库实现。
2. **一次请求返回 2 页（服务端有缓存时 15 页）**，所以下一批起点要按返回数量步进（1→3→5），不能 +1；
   剩余页并发抓取（并发 4），实测 20 页章节 35.3s → 11.0s。
3. 需要 `Referer` 与 `mangabz_lang=2` cookie；桌面 UA 才有完整模板；章节页数写在章节名里（如 `第1话 （20P）`）。
4. 该域名 **A 记录被污染**：`socket.getaddrinfo` 给出的 IPv4 连不上（强制 IPv4 反而 30s 超时），
   走系统默认解析要 8~15s，走代理约 6.5s —— 这就是 `prefer_proxy` 的由来。

### 网络层工程细节（通用）

- **幂等 GET 重试**：漫画站连接超时/对端断连很常见，搜索/详情/章节都是幂等请求，默认重试 2 次（`JUJUBE_HTTP_RETRIES`）。
- **SSL 上下文进程级复用**：Windows 上 `create_default_context(certifi.where())` 单次要 1s+，
  每个客户端构造一遍会拖慢启动与测试；现在缓存后客户端构造约 0.1s。
- **`trust_env=False`**：代理由 `JUJUBE_PROXY` 显式管理，避免 httpx 去读系统代理/注册表。

### 参考实现

- [keiyoushi/extensions-source](https://github.com/keiyoushi/extensions-source)：Tachiyomi/Mihon 扩展（Kotlin），`zaimanhua`、`mangabz` 等
- [YHQY-Dev/venera-sources](https://github.com/YHQY-Dev/venera-sources)：Venera 书源（JS），结构与本项目最接近
- [拷贝漫画移动端 API 文档](https://naer.ink/2026-2/04-24%E6%8B%B7%E8%B4%9D%E6%BC%AB%E7%94%BBAPI.html)：基于 [fumiama/copymanga](https://github.com/fumiama/copymanga) 逆向整理

## 路线图

1. 再漫画登录态：`account-api.zaimanhua.com/v1/login/passwd`（MD5 密码）拿 Bearer token，解锁 VIP 章节
2. 快看漫画（搜索接口直接返回 JSON）、极速漫画 1kkk
3. 拷贝漫画登录态（`authorization: Token {token}`）与 VIP 章节
4. 下载队列与本地落盘（当前只提供直链与中转）
