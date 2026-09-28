# JujubeBookstore

国漫 / 汉化漫画抓取服务。后端只做三件事：**搜索**、**章节列表**、**图片直链**（另附最近更新与防盗链图片中转）。

- 技术栈：Python 3.12 + FastAPI + httpx
- 解析逻辑与 HTTP 层、站点元数据分离：新增站点 = 加一个源类 + 注册一行
- 所有源都已**真实联网验证**，不是纸面接口

## 已实现的源

| key | 站点 | 搜索 | 章节列表 | 图片直链 | 备注 |
| --- | --- | --- | --- | --- | --- |
| `zaimanhua` | 再漫画 | ✅ 20 条/页 | ✅ | ✅ | App v4 API，匿名可用；**部分章节需登录/VIP**（`canRead=false`） |
| `mangabz` | Mangabz | ✅ | ✅ 975 章（海贼王实测） | ✅ 20 页/章 | HTML + 打包 JS 图片接口，全免费 |

实测样例（`海贼王` / Mangabz）：搜索 12 条 → 详情 975 章 → 单章 20 页 → 图片中转返回 `image/jpeg` 95987 字节。

## 快速开始

```bash
pip install -r requirements.txt

# 真实联网联调：搜索 -> 详情 -> 章节 -> 下载首图校验格式
python scripts/smoke.py 海贼王 --source mangabz
python scripts/smoke.py 火影 --source zaimanhua

# 启动服务
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

交互式文档：<http://127.0.0.1:8000/docs>

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查：已注册源、代理开关 |
| GET | `/api/sources` | 可用漫画源 |
| GET | `/api/{source}/search?q=&page=1` | 搜索 |
| GET | `/api/{source}/latest?page=1` | 最近更新（探索页） |
| GET | `/api/{source}/comic/{comic_id}` | 详情 + 章节列表 |
| GET | `/api/{source}/comic/{comic_id}/chapter/{chapter_id}` | 章节图片直链 |
| GET | `/api/{source}/image?url=` | 图片中转（仅限该源允许域名，防 SSRF） |

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
    {"id": "139", "title": "海贼王", "cover": "https://cover.mangabz.com/…", "authors": [], "tags": [], "status": null, "description": null}
  ]
}
```

错误约定：`404` 资源不存在/未知源，`502` 上游异常，`503` 该源需要代理但未配置 `JUJUBE_PROXY`。

> 图片直链普遍带签名与过期时间（如再漫画的 `?sign=…&t=…`），客户端应及时消费；服务端章节缓存默认 300s，避免下发失效链接。

## 代理配置

部分站点（拷贝漫画等）国内不可直连，这类源声明 `needs_proxy = True` 后自动走 `JUJUBE_PROXY`：

```bash
export JUJUBE_PROXY=http://127.0.0.1:7897   # Clash 混合端口
```

其余配置见 `.env.example`（超时、缓存 TTL、图片中转开关与大小上限）。

## 项目结构

```
app/
  main.py            FastAPI 路由、错误翻译、缓存接入
  config.py          JUJUBE_* 环境变量配置
  http_client.py     httpx 客户端池（直连 / 走代理两套）
  cache.py           极简 TTL 缓存
  schemas.py         对外统一数据结构
  errors.py          SourceError / NotFoundError / ProxyRequiredError
  sources/
    base.py          ComicSource 抽象：search / detail / chapter
    zaimanhua.py     再漫画（App v4 API）
    mangabz.py       Mangabz（HTML + chapterimage.ashx）
    packer.py        Dean Edwards 打包 JS 解包器（纯标准库）
scripts/smoke.py     真实联网联调脚本
tests/               全离线单测（fixtures 为真实抓取的响应片段）
```

## 新增一个源

1. 继承 `ComicSource`，实现 `search` / `detail` / `chapter`，返回 `schemas` 里的统一结构；
2. 声明 `key` / `name` / `needs_proxy` / `image_hosts` / `image_referer`；
3. 在 `app/sources/__init__.py` 的 `SOURCE_CLASSES` 注册；
4. 解析错误统一抛 `SourceError` / `NotFoundError`，API 层会自动翻译成 502 / 404。

## 测试

```bash
python -m pytest        # 32 项，全离线
python scripts/smoke.py 海贼王 --source mangabz   # 真实联网
```

## 站点调研与踩坑记录

### 本机网络实测（无代理时）

| 站点 | 直连 | 结论 |
| --- | --- | --- |
| 再漫画 | 200 | 已实现 |
| Mangabz | 200 | 已实现 |
| 极速漫画 1kkk / 快看漫画 / 风车漫画 / 漫画屋 | 200 | 待实现 |
| 拷贝漫画 | 不通，走代理可用 | API 域名是 `api.mangacopy.com`，**`copymanga.org` 已失效** |
| 动漫之家 / 看漫画 manhuagui | 不通 | 需代理 |

### 关键坑位

- **再漫画详情接口**：必须带 `?_v=2.2.5`。用 `?channel=android` 时大量漫画会返回 `errno=2 漫画不存在或已被删除`（实为 android 渠道的版权过滤），实测 0/6 与 6/6 的差异就来自这个参数。
- **再漫画 id 映射**：搜索结果的 `comic_id` 恒为 0、最近更新的 `id` 恒为 0，必须取非 0 的那个（与 keiyoushi DTO 的 `comicId.takeIf { it != 0 } ?: id` 一致）。
- **Mangabz 图片接口**：`/m{cid}/chapterimage.ashx?cid=&page=` 返回 Dean Edwards 打包 JS，解包后取 `pix` 前缀 + 路径数组；一次返回 2 页（服务端有缓存时 15 页），翻页要按实际返回数量步进，不能按 1 递增。
- **Mangabz 页面**：需要 `Referer` 与 `mangabz_lang=2` cookie；桌面 UA 才有完整模板。章节页数写在章节名里（如 `第1话 （20P）`）。

### 参考实现

- [keiyoushi/extensions-source](https://github.com/keiyoushi/extensions-source)：Tachiyomi/Mihon 扩展（Kotlin），`zaimanhua`、`mangabz`、`kuaikanmanhua`、`manhuagui` 等，本项目的接口参数以它为准
- [YHQY-Dev/venera-sources](https://github.com/YHQY-Dev/venera-sources)：Venera 书源（JS），结构与本项目最接近

## 路线图

1. 拷贝漫画（`api.mangacopy.com`）：接口结构已确认，但搜索在代理下返回 `total=0`，待排查（可能需要 `region`/`version` 请求头或登录态）
2. 再漫画登录态：`account-api.zaimanhua.com/v1/login/passwd`（MD5 密码）拿 Bearer token，解锁 VIP 章节
3. 快看漫画（搜索接口直接返回 JSON）、极速漫画 1kkk
4. 下载队列与本地落盘（当前只提供直链与中转）
