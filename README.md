# JujubeBookstore

国漫 / 汉化漫画抓取服务。后端提供两类源：

- **漫画源（图片阅读型）**：搜索、章节列表、图片直链，外加最近更新与防盗链图片中转
- **资源索引源（BT / 磁力）**：搜索、最新发布、磁力 / 种子 / 文件列表

- 技术栈：Python 3.12 + FastAPI + httpx
- 解析逻辑与 HTTP 层、站点元数据分离：新增站点 = 加一个源类 + 注册一行
- 每个源都**真实联网验证**过，不是纸面接口
- 77 项离线单测（fixtures 是真实抓取的响应片段）+ 真实联网联调脚本

## 已实现的源

### 漫画源 `ComicSource`

| key | 站点 | 代理 | 搜索 | 章节列表 | 图片直链 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| `zaimanhua` | 再漫画 | 直连 | ✅ 20 条/页 | ✅ 264 章实测 | ✅ | App v4 API；部分章节需登录/VIP（`canRead=false`） |
| `mangabz` | Mangabz | 有代理优先 | ✅ 12 条 | ✅ 975 章实测 | ✅ 20/96 页实测 | HTML + 打包 JS 图片接口，全免费 |
| `mangacopy` | 拷贝漫画 | **必须** | ✅ 21 条/页（total 247） | ✅ | ✅ 6 页 webp 实测 | 官方 App v3 API，需 `JUJUBE_PROXY` |

### 资源索引源 `ResourceSource`

| key | 站点 | 代理 | 搜索 | 磁力/种子 | 备注 |
| --- | --- | --- | --- | --- | --- |
| `dmhy` | 动漫花园 share.dmhy.org | 有代理优先（无代理走 IPv4） | ✅ 80 条/页 | ✅ 实测下载到 19,894 字节 bencode 种子 | 免费公开 BT 索引，匿名可搜 |

实测样例：三源各跑一遍 `搜索 → 详情 → 章节图片 → 图片中转`，均返回 200 且图片格式校验通过
（再漫画 JPEG 192KB、Mangabz JPEG 96KB、拷贝漫画 WebP 353KB）；dmhy 跑 `搜索 → 详情 → 下载 .torrent 校验 bencode`。

## 快速开始

```bash
pip install -r requirements.txt

# 真实联网联调：搜索 -> 详情 -> 章节 -> 下载首图校验格式
python scripts/smoke.py 海贼王 --source mangabz
python scripts/smoke.py 火影   --source zaimanhua

# 资源索引源（BT/磁力）：搜索 -> 详情 -> 下载种子校验 bencode
python scripts/smoke.py 海贼王 --kind resource --source dmhy --category 漫畫

# 拷贝漫画需要代理
export JUJUBE_PROXY=http://127.0.0.1:7897
python scripts/smoke.py 火影忍者 --source mangacopy

# 启动服务
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

交互式文档：<http://127.0.0.1:8000/docs>

## API

### 漫画源

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查：两类源清单、代理开关 |
| GET | `/api/sources` | 可用漫画源（`kind=comic`） |
| GET | `/api/{source}/search?q=&page=1` | 搜索 |
| GET | `/api/{source}/latest?page=1` | 最近更新（探索页） |
| GET | `/api/{source}/comic/{comic_id}` | 详情 + 章节列表 |
| GET | `/api/{source}/comic/{comic_id}/chapter/{chapter_id}` | 章节图片直链 |
| GET | `/api/{source}/image?url=` | 图片中转（白名单校验，防 SSRF） |

### 资源索引源（BT / 磁力）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/resources` | 可用资源源（`kind=resource`） |
| GET | `/api/resources/{source}/search?q=&page=&category=` | 搜索资源，`category` 支持分类名或 `sort_id` |
| GET | `/api/resources/{source}/latest?page=&category=` | 最新发布 |
| GET | `/api/resources/{source}/item/{item_id}` | 磁力 / 种子 / 文件列表 |

```bash
curl "http://127.0.0.1:8000/api/mangabz/search?q=%E6%B5%B7%E8%B4%BC%E7%8E%8B"
curl "http://127.0.0.1:8000/api/mangabz/comic/139"
curl "http://127.0.0.1:8000/api/mangabz/comic/139/chapter/29397"

# 资源索引：搜索 -> 详情（磁力/种子）
curl "http://127.0.0.1:8000/api/resources/dmhy/search?q=%E6%B5%B7%E8%B4%BC%E7%8E%8B&category=%E6%BC%AB%E7%95%AB"
curl "http://127.0.0.1:8000/api/resources/dmhy/item/727928_SweetSub_Seihantai_na_Kimi_to_Boku_24_WebRip_1080P_HEVC_10bit.html"
```

> 资源源的 `id` 是**带 slug 的规范路径片段**（形如 `727928_SweetSub_....html`），不是纯数字：
> dmhy 对 `/topics/view/{纯数字}` 会返回 HTTP 200 的 9KB 空壳页，拿不到任何内容，
> 所以传纯数字会直接返回 404 并提示改用列表里的 `id`/`detail_url`。

漫画源响应用统一结构，站点原始字段不外泄：

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
| `prefer_proxy = True` | 直连可用但不稳（Mangabz、dmhy）。配了代理就走代理，没配就直连 |
| `prefer_ipv4 = True` | 无代理时强制 IPv4：站点 AAAA 是伪地址、httpcore 会卡在 IPv6 超时（dmhy） |

其余配置见 `.env.example`（超时、重试次数、缓存 TTL、图片中转开关与大小上限）。

## 项目结构

```
app/
  main.py            FastAPI 路由（漫画 + 资源两套命名空间）、错误翻译、缓存接入、图片中转
  config.py          JUJUBE_* 环境变量配置
  http_client.py     httpx 客户端池：直连/代理/IPv4 三态 + 幂等 GET 重试 + SSL 上下文复用
  transport.py       源共用传输层：代理与客户端选择（漫画源/资源源都继承）
  cache.py           极简 TTL 缓存
  schemas.py         对外统一数据结构
  errors.py          SourceError / NotFoundError / ProxyRequiredError
  sources/           漫画源（ComicSource）
    base.py          search / detail / chapter 抽象
    zaimanhua.py     再漫画（App v4 API）
    mangabz.py       Mangabz（HTML + chapterimage.ashx，章节并发抓取）
    mangacopy.py     拷贝漫画（App v3 API，节点轮换）
    packer.py        Dean Edwards 打包 JS 解包器（纯标准库）
  resources/         资源索引源（ResourceSource，BT/磁力）
    base.py          search / latest / detail 抽象
    dmhy.py          动漫花园（HTML 解析 + 分类映射）
scripts/smoke.py     真实联网联调脚本（--kind comic|resource）
tests/               77 项离线单测（fixtures 为真实抓取的响应片段）
```

## 新增一个源

**漫画源**：

1. 继承 `app/sources/base.py` 的 `ComicSource`，实现 `search` / `detail` / `chapter`；
2. 声明 `key` / `name` / `needs_proxy` / `prefer_proxy` / `prefer_ipv4` / `image_hosts`（或 `image_host_patterns`）/ `image_referer`；
3. 在 `app/sources/__init__.py` 的 `SOURCE_CLASSES` 注册。

**资源索引源**：

1. 继承 `app/resources/base.py` 的 `ResourceSource`，实现 `search` / `detail`（`latest` 可选）；
2. 在 `app/resources/__init__.py` 的 `RESOURCE_CLASSES` 注册。

两者错误统一抛 `SourceError` / `NotFoundError`，API 层会自动翻译成 502 / 404。

## 测试

```bash
python -m pytest                                       # 77 项，全离线，约 9s
python scripts/smoke.py 海贼王 --source mangabz         # 漫画源真实联网
python scripts/smoke.py 海贼王 --kind resource --source dmhy            # 资源源真实联网
python scripts/smoke.py 海贼王 --kind resource --source dmhy --category 漫畫
```

## 站点调研与踩坑记录

### 本机网络实测（无代理时）

| 站点 | 直连 | 结论 |
| --- | --- | --- |
| 再漫画 | 200，0.1s | 已实现 |
| Mangabz | 200，但 8~15s 且偶发超时 | 已实现（配代理优先） |
| 动漫花园 share.dmhy.org | 直连超时；强制 IPv4 通但 23~25s | 已实现（配代理优先，约 10s） |
| 拷贝漫画 | 不通 | 已实现（必须走代理） |
| 极速漫画 1kkk / 快看漫画 / 风车漫画 / 漫画屋 | 200 | 待实现 |
| 动漫之家 / 看漫画 manhuagui | 不通 | 需代理 |
| koz.moe（Kmoe） | 200 | 未实现，见下 |

### 动漫花园 dmhy（BT 资源索引）

1. **免费公开**：浏览与搜索都不需要账号，匿名实测 80 条/页，页面头部只有「登入/註冊」链接。
2. 交付物是 **magnet + .torrent**（`//dl.dmhy.org/{日期}/{hash}.torrent`），没有章节/图片，所以单独一类源。
3. **详情必须带 slug**：`/topics/view/{id}_{slug}.html` 才有效；任何非规范地址（含纯数字 id）
   都返回 **HTTP 200 + 9183 字节空壳页**，没有任何内容 —— 这类「200 但空」最难查，
   所以列表项的 `id` 直接就是规范 slug。
4. 分类用 `sort_id`（動畫=2、漫畫=3、音樂=4、日劇=6、ＲＡＷ=7、遊戲=9、特攝=12、季度全集=31、
   港台原版=41、日文原版=42…），支持繁简别名与数字；注意它是**分类树**，
   例如 `sort_id=3` 会把「港台原版」这类子分类的结果也带出来。
5. 列表列序固定为：日期 | 分类 | 标题+发布组 | 磁力 | 大小 | 做種 | 下載 | 完成 | 發布人。
6. 标题里内嵌 `<span class="keyword">` 高亮标签，取文本时既不能加空格分隔符
   （`海賊王 ][1179]`），也不能 `strip=True`（`One Piece海賊王`），只能取原文本再压缩空白。

### koz.moe / Kmoe（Kindle 漫画电子书，未实现）

- 标题「Kmoe [Kindle|epub漫畫]」，与 **`mox.moe`、`kox.moe` 是同一家**的镜像（来自油猴脚本的 `@match`）。
- 交付物是 **epub / mobi / azw3 文件**，不是图片序列，所以也不属于 `ComicSource`。
- **不是纯免费**：作品页真实路径是 `/c/{id}.htm`（`/comic/{id}.htm` 是 404），匿名访问会命中
  「[隱藏] 此書未公開」，页面里 `VIP` 出现 15 次、`额度` 2 次 —— 有每日下载额度与 VIP 分层。
- 结论：要做需要配账号 cookie（甚至 VIP），且要新增「电子书源」类型，暂缓。

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
- **三种传输形态**：直连 / 走代理 / 强制 IPv4。同一个站点在不同网络下最优解不同
  （Mangabz 强制 IPv4 反而 30s 超时，dmhy 不强制 IPv4 则直接连不上），所以选择权交给源自己声明。

### 参考实现

- [keiyoushi/extensions-source](https://github.com/keiyoushi/extensions-source)：Tachiyomi/Mihon 扩展（Kotlin），`zaimanhua`、`mangabz` 等
- [YHQY-Dev/venera-sources](https://github.com/YHQY-Dev/venera-sources)：Venera 书源（JS），结构与本项目最接近
- [拷贝漫画移动端 API 文档](https://naer.ink/2026-2/04-24%E6%8B%B7%E8%B4%9D%E6%BC%AB%E7%94%BBAPI.html)：基于 [fumiama/copymanga](https://github.com/fumiama/copymanga) 逆向整理

## 路线图

1. koz.moe / Kmoe 电子书源（epub/mobi）：需要账号 cookie，且要新增「电子书源」类型
2. 再漫画登录态：`account-api.zaimanhua.com/v1/login/passwd`（MD5 密码）拿 Bearer token，解锁 VIP 章节
3. 快看漫画（搜索接口直接返回 JSON）、极速漫画 1kkk
4. 拷贝漫画登录态（`authorization: Token {token}`）与 VIP 章节
5. 下载队列与本地落盘：BT 资源可以衔接 `aria2`/`transmission`（当前只给 magnet/种子）
