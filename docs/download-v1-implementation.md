# JujubeBookstore 下载管理 V1 实施文档

## 1. 目标

在现有 FastAPI + 原生单页前端的基础上，增加可持久化的下载管理能力，优先打通以下闭环：

1. 从漫画章节详情页创建单章节下载任务。
2. 后台静默下载章节图片。
3. 实时查看任务进度、速度和状态。
4. 暂停、恢复、取消、重试任务。
5. 使用下载分组管理任务。
6. 下载完成后从浏览器获取文件。
7. 支持直接下载 `.torrent` 文件。

V1 不自行实现 BT 协议，磁力任务接入 aria2/Transmission 延后到 V2。

## 2. 首版范围

### 纳入 V1

- SQLite 任务持久化。
- 单进程异步下载管理器，数据库操作使用 `aiosqlite`。
- 默认 2 个后台 worker，可配置。
- 漫画章节图片下载，统一输出 CBZ。
- `.torrent` 文件下载。
- 自定义下载分组。
- 种子文件 HTTP Range 断点续传；图片按文件断点恢复。
- 失败重试和服务重启恢复。
- SSE 任务进度推送。
- 内置前端下载管理页。
- 本机浏览器文件下载。
- 章节组批量展开：一次确认后创建多个独立章节任务。

### 暂缓到 V2

- 磁力链接实际下载。
- aria2/Transmission RPC 适配。
- 用户登录、权限体系和多用户隔离。
- 公网部署场景下的完整鉴权。
- 多节点下载和分布式队列。
- ZIP、目录输出和目录浏览。
- 自动追更和定时下载。
- 独立桌面客户端。

## 3. 设计原则

- 不修改现有漫画源和资源源的核心接口。
- 下载逻辑放在独立的 `app/downloads/` 模块。
- 站点图片地址只在任务执行时获取，避免签名 URL 过期。
- 所有下载先写入临时文件，完成后原子改名。
- 任务状态写入 SQLite，不能只依赖内存队列；SQLite 访问通过 `aiosqlite` 完成。
- 首版使用 SSE 推送进度，避免引入 WebSocket 依赖。
- 同一章节重复提交时保持幂等；分组变化不产生重复下载。
- 下载目录内的文件名必须经过安全清洗。
- 所有保存路径只能是下载根目录下的相对路径。

## 4. 模块结构

新增目录：

```text
app/downloads/
  __init__.py          # 模块导出
  models.py            # 下载相关 Pydantic 模型
  repository.py        # aiosqlite 初始化和 CRUD
  manager.py           # DownloadManager、队列和 worker
  http_downloader.py   # HTTP 文件/图片下载、断点续传
  comic_downloader.py  # 章节解析、图片下载、CBZ 打包
  torrent_downloader.py# .torrent 文件下载
  events.py            # SSE 事件订阅
  filenames.py         # 文件名清洗和相对路径安全校验
```

`app/main.py` 只负责注册路由和初始化生命周期，不直接实现下载细节。

启动流程：

```text
FastAPI lifespan
  ├─ 初始化 SQLite（WAL + foreign_keys）
  ├─ 将 pending/downloading 状态恢复为 queued
  ├─ 创建 DownloadManager
  ├─ 启动 worker
  └─ 应用退出时停止 worker 并关闭数据库
```

## 5. 数据库设计

首版使用 SQLite，必须开启 WAL 和外键约束。数据库访问使用 `aiosqlite`，避免阻塞 FastAPI 事件循环。数据库路径默认为 `JUJUBE_DOWNLOAD_DB`，未设置时使用下载目录下的 `downloads.db`。

### `download_groups`

```sql
CREATE TABLE download_groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    save_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

### `download_tasks`

```sql
CREATE TABLE download_tasks (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    group_id TEXT REFERENCES download_groups(id) ON DELETE SET NULL,
    type TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    title TEXT NOT NULL,
    output_format TEXT NOT NULL DEFAULT 'cbz',
    status TEXT NOT NULL,
    requested_action TEXT NOT NULL DEFAULT 'none',
    cleanup_state TEXT NOT NULL DEFAULT 'none',
    batch_id TEXT,
    total_bytes INTEGER,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    completed_files INTEGER NOT NULL DEFAULT 0,
    progress REAL NOT NULL DEFAULT 0,
    speed REAL NOT NULL DEFAULT 0,
    eta INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    destination TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);
```

### `download_files`

```sql
CREATE TABLE download_files (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES download_tasks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    checksum TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(task_id, name)
);
```

图片签名 URL 不写入数据库。漫画图片任务在开始或失败重试时重新调用章节接口，URL 只存在于当前 worker 的内存中。

`payload_json` 保存任务执行所需的源参数，例如 `comic_id`、`chapter_id`、`torrent_url` 和输出选项。它是任务恢复的输入，不保存长期有效的图片签名地址。

初始化数据库时执行：

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
```

## 6. 状态机

```text
pending → queued → downloading → completed
                    ├─ requested_action=pause  → paused → queued
                    ├─ requested_action=cancel → canceled
                    └────────────────────────→ failed → queued
```

状态约束：

- `pending`：已创建但尚未进入队列。
- `queued`：等待 worker 执行。
- `downloading`：正在传输或打包。
- `requested_action=pause`：运行中的任务收到暂停请求，等待 worker 在安全检查点确认。
- `paused`：worker 已确认暂停，保留 `.part` 文件。
- `completed`：所有文件完成并已生成最终文件。
- `failed`：达到重试上限或发生不可恢复错误。
- `canceled`：用户取消，保留任务记录但不再执行。
- 服务重启时，`pending` 和 `downloading` 自动转为 `queued`；`paused` 保持暂停。
- API 不能直接把 `downloading` 写成 `completed`、`paused` 或 `canceled`，最终状态由 worker 使用条件更新确认。

## 7. API 设计

### 分组

```text
GET    /api/download-groups
POST   /api/download-groups
PATCH  /api/download-groups/{group_id}
DELETE /api/download-groups/{group_id}
```

创建请求：

```json
{
  "name": "海贼王",
  "description": "待整理章节",
  "save_path": null
}
```

删除分组只解除任务关联，任务本身和文件不删除；关联任务转为“未分组”。

### 任务

```text
GET    /api/downloads
POST   /api/downloads
POST   /api/downloads/batch
GET    /api/downloads/{task_id}
GET    /api/downloads/{task_id}/children
DELETE /api/downloads/{task_id}
POST   /api/downloads/{task_id}/pause
POST   /api/downloads/{task_id}/resume
POST   /api/downloads/{task_id}/cancel
POST   /api/downloads/{task_id}/retry
GET    /api/download-batches/preview
GET    /api/download-batches/{batch_id}
```

创建漫画章节任务：

```json
{
  "type": "comic_chapter",
  "source": "mangabz",
  "source_id": "comic-139/chapter-29397",
  "comic_id": "139",
  "chapter_id": "29397",
  "title": "海贼王 第29397话",
  "group_id": "group-id",
  "output_format": "cbz"
}
```

创建章节组任务：

```json
{
  "source": "mangabz",
  "source_id": "139",
  "comic_id": "139",
  "chapter_group": "连载",
  "title": "海贼王 连载章节",
  "group_id": "group-id",
  "output_format": "cbz"
}
```

`POST /api/downloads/batch` 只调用一次详情接口，在请求中完成章节筛选和任务批量写入；它不创建父任务，也不启动批量 worker。返回 `batch_id` 和子任务 ID 列表，后台只执行普通 `comic_chapter` 任务。

`GET /api/download-batches/preview` 只读取详情并返回章节数量、筛选结果和已存在任务数量，不创建任务；前端确认后再调用批量创建接口。批量创建超过 `JUJUBE_DOWNLOAD_BATCH_MAX_ITEMS` 时返回 `422`。

创建种子文件任务：

```json
{
  "type": "torrent",
  "source": "dmhy",
  "source_id": "727928_xxx.html",
  "title": "xxx.torrent",
  "torrent_url": "https://example.com/a.torrent",
  "group_id": "group-id",
  "output_format": "file"
}
```

V1 只允许 `comic_chapter=cbz` 和 `torrent=file` 两种组合。`directory`、`zip` 和任意自定义保存格式暂不支持。

### 查询和过滤

`GET /api/downloads` 支持：

```text
group_id
status
type
page
page_size
```

默认按 `created_at DESC` 返回。

### 进度事件

```text
GET /api/downloads/events
```

事件类型：

```text
download.created
download.updated
download.completed
download.failed
download.deleted
```

事件示例：

```json
{
  "type": "download.updated",
  "task_id": "task-id",
  "status": "downloading",
  "downloaded_bytes": 52428800,
  "total_bytes": 104857600,
  "progress": 50.0,
  "speed": 1834210,
  "eta": 28,
  "completed_files": 5,
  "file_count": 10
}
```

SSE 实现要求：每 15 秒发送一次 heartbeat；订阅者断开时立即移除；每个订阅者队列限制长度，进度事件只保留最新值，不能因为慢客户端阻塞 worker。客户端重连后必须重新请求任务列表，事件只用于刷新，不作为唯一状态来源。

### 文件获取

```text
GET /api/downloads/{task_id}/files
GET /api/downloads/{task_id}/content
```

只有 `completed` 任务允许访问 `content`。漫画任务返回已生成的 `.cbz` 文件，种子任务返回 `.torrent` 文件。

## 8. 下载执行策略

### 漫画章节

1. worker 调用现有章节接口获取最新图片列表。
2. 为任务创建 `download_files` 记录。
3. 逐文件或有限并发下载图片。
4. 每完成一个文件就更新数据库和 SSE 事件。
5. 根据 `output_format=cbz` 生成 CBZ。
6. 所有文件完成后更新为 `completed`。

图片请求继续复用现有源的 HTTP 客户端、代理和 Referer 配置。

### `.torrent`

1. 通过资源源客户端获取种子文件。
2. 使用 HTTP Range 续传。
3. 写入临时文件。
4. 完成后改名为最终文件。

### 断点续传

- 临时文件后缀统一使用 `.part`。
- 服务重启时根据临时文件大小发送 `Range` 请求。
- 上游不支持 Range 时删除临时文件并重新下载。
- 不覆盖已完成的同名文件，使用幂等任务 ID 或安全重命名。

### 8.1 任务触发矩阵

| 触发入口 | 任务类型 | 创建方式 | 实际执行者 | 完成条件 |
| --- | --- | --- | --- | --- |
| 章节详情页“下载本章” | `comic_chapter` | 创建一个任务 | 漫画章节 worker | 本章全部图片完成并完成打包 |
| 章节详情页“下载章节组” | 多个 `comic_chapter` 任务 | 请求中获取一次详情并批量创建任务，共享 `batch_id` | 漫画章节 worker | 所有任务分别完成，批次接口汇总结果 |
| 资源详情页“下载 .torrent” | `torrent` | 创建一个任务 | 种子文件 worker | 种子文件写入并校验完成 |
| 下载列表“重试” | 原任务类型 | 复用原任务记录 | 原类型 worker | 按原任务完成条件结束 |
| 服务启动恢复 | 原任务类型 | 从数据库恢复 `downloading` | DownloadManager | 任务重新进入正常状态 |

V1 不创建 `comic_batch` 父任务。`batch_id` 只是批次标识，任务之间相互独立；批次查询接口按 `batch_id` 汇总完成、失败和进行中的任务。

### 8.2 通用创建流程

所有创建入口必须经过同一套流程，不能由前端直接操作队列：

1. API 校验任务类型和必填参数。
2. 校验 `group_id` 是否存在；为空时使用“未分组”。
3. 根据任务内容生成 `idempotency_key`。
4. 查询同一幂等键的已有任务：已有未取消任务直接返回，避免重复下载。
5. 生成任务目录，但不创建最终文件。
6. 在事务中直接写入 `queued` 任务；批量请求为每个子任务写入相同的 `batch_id`。
7. 提交事务后发布 `download.created` 事件。
8. 调用 `DownloadManager.enqueue(task_id)`；如果进程在入队前退出，启动恢复会再次扫描 `queued` 任务。
9. 返回完整任务对象，前端立即进入下载列表。

队列只接收数据库中已存在的任务 ID；worker 每次从数据库重新读取任务，避免内存对象和数据库状态不一致。

### 8.3 worker 通用执行循环

`DownloadManager` 的运行时对象：

- `asyncio.Queue`：保存已持久化的 `task_id`，默认按创建顺序执行。
- `asyncio.Semaphore`：限制同时执行的任务数。
- `task_controls`：按任务 ID 保存 `pause_event` 和 `cancel_event`，只用于控制当前进程中的 worker。
- `event_bus`：保存 SSE 订阅者队列；数据库是最终状态来源，事件丢失后可通过列表接口恢复。

暂停或取消请求先写入 `requested_action`，再设置对应的运行时事件。排队任务可以立即改为 `paused` 或 `canceled`；运行中任务由 worker 在安全检查点确认最终状态。服务重启后只依赖数据库状态恢复。

```text
worker_loop
  ├─ 从内存队列获取 task_id
  ├─ 原子抢占：queued → downloading
  ├─ 检查 requested_action
  ├─ 根据 task.type 选择 handler
  ├─ handler 执行并周期性上报进度
  ├─ 成功：写 completed 并发布 completed 事件
  ├─ 可重试错误：递增 retry_count，延迟后重新 queued
  ├─ 不可重试错误：写 failed 并发布 failed 事件
  └─ finally：释放并发槽位，继续处理下一个任务
```

抢占和最终状态写入必须使用条件更新，防止同一任务被两个 worker 同时执行，或用户操作被 worker 覆盖：

```sql
UPDATE download_tasks
SET status = 'downloading', started_at = COALESCE(started_at, :now), updated_at = :now
WHERE id = :task_id AND status = 'queued';
```

更新影响行数为 0 时，worker 放弃该任务，不重复执行。worker 完成时使用 `WHERE status = 'downloading' AND requested_action = 'none'`；暂停和取消确认时使用相同的状态条件。

进度更新不应每个数据块都写数据库。建议按“至少 250ms 或累计 256KB”触发一次，更新数据库并发布 `download.updated`。

### 8.4 单章节任务执行逻辑

任务类型：`comic_chapter`。

1. 根据 `source` 创建现有 `ComicSource`。
2. 调用 `source.chapter(comic_id, chapter_id)` 获取最新图片列表。
3. 如果章节不存在、图片列表为空或源返回不可读错误，任务直接 `failed`。
4. 按图片顺序生成安全文件名：`001.jpg`、`002.webp` 等；扩展名从响应类型或 URL 推断。
5. 在事务中插入或补齐 `download_files`，已存在且最终文件校验通过的文件标记为 `completed`。
6. 对剩余文件执行有限并发下载：
   - 使用源的 HTTP 客户端和 Referer。
   - 图片 URL 只保存在当前 worker 内存中，不写入数据库。
   - 目标文件写入 `page-001.ext.part`。
   - 支持 Range 续传。
   - 单文件完成后改名并更新文件状态。
7. 每个文件完成后更新任务的 `completed_files`、`downloaded_bytes`、`progress`。
8. 全部图片完成后，使用 `asyncio.to_thread()` 执行 CBZ 打包，写入临时文件并原子改名。
9. 更新 `destination`、`completed_at`，任务进入 `completed`。

如果执行中收到暂停或取消信号，当前 HTTP 请求结束后立即停止提交新文件；已完成文件和 `.part` 文件保留。图片文件不做字节级 Range 续传，单张失败时重新下载该文件。

### 8.5 章节组批量展开逻辑

章节组下载不创建父任务，也不在后台运行独立协调器：

1. 前端先请求预览接口或详情接口，显示章节数量和已存在任务数量。
2. 用户确认后，API 调用一次 `source.detail(comic_id)`。
3. 按 `ChapterInfo.group` 筛选章节，最多允许 `JUJUBE_DOWNLOAD_BATCH_MAX_ITEMS` 条。
4. 为每个章节生成幂等键：

   ```text
   source + comic_id + chapter_id + output_format
   ```

5. 在一个事务中创建不存在的 `comic_chapter` 任务，写入相同的 `batch_id`。
6. 事务提交后逐个放入普通任务队列。
7. `GET /api/download-batches/{batch_id}` 按批次统计 `queued`、`downloading`、`paused`、`completed`、`failed` 和 `canceled` 数量。
8. 批次中部分失败时保留已完成任务，用户可以只重试失败任务。

### 8.6 `.torrent` 任务执行逻辑

任务类型：`torrent`。

1. 如果请求只提供 `source` 和 `source_id`，worker 先调用资源源 `detail(source_id)` 获取种子地址。
2. 如果请求提供 `torrent_url`，仍必须通过对应资源源的 HTTP 客户端访问，禁止使用任意新建的直连客户端。
3. 校验 URL 的协议、资源源域名和最终跳转域名；不符合白名单直接失败。
4. 生成安全文件名并写入 `<task-dir>/<name>.torrent.part`。
5. 使用 Range 续传，按字节数上报进度；服务器无 `Content-Length` 时允许总大小为空。
6. 下载完成后检查内容非空，并执行通用 bencode 解析校验，不假设必须存在 `announce` 字段。
7. 校验通过后原子改名，写入文件记录，任务进入 `completed`。
8. 种子下载失败不影响漫画任务；重试沿用通用重试策略。

### 8.7 暂停、恢复、取消、重试

**暂停**：

- `queued` 任务直接改为 `paused`，并从待执行队列中跳过。
- `downloading` 任务只写入 `requested_action=pause`；worker 在文件边界或 250ms 检查点停止，并使用条件更新确认 `paused`。
- 不删除 `.part` 文件，不增加失败次数。

**恢复**：

- 仅允许 `paused` 任务恢复。
- 清除 `requested_action`，状态改为 `queued`，发布 `download.updated`，重新入队。
- 继续使用已有 `.part` 文件。

**取消**：

- `queued`、`paused` 任务立即改为 `canceled`。
- `downloading` 任务只写入 `requested_action=cancel`，worker 在当前请求结束后停止并确认 `canceled`。
- 默认保留 `.part` 文件，便于用户之后手动重试；取消任务不再自动执行。

**重试**：

- 允许 `failed` 或 `canceled` 任务重试。
- 清空 `error_message` 和 `requested_action`，保留已完成文件和 `.part` 文件。
- 将任务改为 `queued` 并重新入队。
- 自动重试使用指数退避：`2s、5s、15s`，达到 `JUJUBE_DOWNLOAD_RETRIES` 后转为 `failed`。

**删除**：

- `downloading`、`queued`、`paused` 任务不能直接删除，API 返回 `409`，要求先暂停或取消。
- `completed`、`failed`、`canceled` 任务允许删除。
- 删除时先将任务的 `cleanup_state` 标记为 `pending`，再删除任务目录和临时文件，最后在事务中删除数据库记录。
- 文件删除失败时保留数据库记录和清理标记，服务启动时继续清理，不产生“数据库已删但文件残留”的不可追踪状态。
- 删除成功后发布 `download.deleted` 事件。

### 8.8 错误分类

| 错误 | 处理 |
| --- | --- |
| 连接超时、连接重置、HTTP 408、429、5xx | 自动重试 |
| 章节地址签名过期 | 重新调用章节接口获取地址后重试 |
| HTTP 404、源明确返回不存在 | 直接失败 |
| 代理未配置 | 直接失败，提示配置 `JUJUBE_PROXY` |
| 磁盘空间不足、无权限、路径非法 | 直接失败，不自动重试 |
| 用户暂停或取消 | 不计为失败 |
| CBZ 打包失败 | 任务失败，保留已下载图片 |

### 8.9 服务重启恢复

服务启动时按以下顺序恢复：

1. 打开数据库并启用 WAL。
2. 查询所有 `pending`、`downloading` 任务，批量改为 `queued` 并清除 `requested_action`。
3. 查询 `queued` 任务，按 `created_at ASC` 放入内存队列。
4. 清理 `cleanup_state=pending` 的任务目录；删除失败则保留记录等待下一次启动。
5. 检查每个任务目录中的 `.part` 文件，文件记录不存在时补齐记录。
6. 删除上一次打包残留的 `.cbz.tmp` 文件。
7. 启动 worker。
8. 发布一次 `download.updated` 快照事件，供前端重连后刷新。

恢复过程不自动重置 `retry_count`，避免服务频繁重启绕过重试上限。

### 8.10 文件完成与客户端获取

文件完成后才允许 `GET /api/downloads/{task_id}/content`：

1. API 查询任务状态必须为 `completed`。
2. 使用数据库中的 `destination`，解析后必须仍位于 `JUJUBE_DOWNLOAD_DIR` 下。
3. 单文件任务使用 `FileResponse` 支持 Range。
4. 漫画任务只返回已生成的 CBZ 文件，不在请求期间动态打包。
5. 设置 `Content-Disposition`，文件名使用安全清洗后的标题。
6. 任务未完成、路径不存在或路径越界时返回 404/409，不返回部分文件。

## 9. 前端 V1

继续使用 `web/index.html`，不引入构建工具。

新增“下载管理”导航页：

- 左栏：分组列表和未分组任务。
- 主区：任务表格。
- 详情区：文件列表、错误信息和操作按钮。
- 顶部显示运行中、排队中、已完成、失败数量。

任务行显示：

```text
标题 | 分组 | 状态 | 进度条 | 速度 | ETA | 操作
```

现有页面增加入口：

- 章节详情：`下载本章`、`下载章节组`。
- 资源详情：`下载 .torrent`。
- 下载完成：`下载 CBZ/种子文件`。

前端使用 `EventSource('/api/downloads/events')` 监听进度；服务端每 15 秒发送 heartbeat，断线后自动重连，并在重连后重新请求任务列表。

## 10. 配置项

```env
JUJUBE_DOWNLOAD_DIR=./downloads
JUJUBE_DOWNLOAD_DB=./downloads/downloads.db
JUJUBE_DOWNLOAD_WORKERS=2
JUJUBE_DOWNLOAD_IMAGE_CONCURRENCY=4
JUJUBE_DOWNLOAD_RETRIES=3
JUJUBE_DOWNLOAD_RESUME=1
JUJUBE_DOWNLOAD_BATCH_MAX_ITEMS=100
```

默认配置面向本机使用。下载目录应限制在明确的根目录内，分组路径只能是下载根目录下的相对目录，禁止客户端提交任意绝对路径。

## 11. 开发顺序

### 里程碑 M1：任务库和基础模型

- 创建 `app/downloads/`。
- 完成 SQLite 初始化、迁移和 Repository。
- 完成 Pydantic 模型与状态机。
- 完成分组和任务基础 API。
- 增加任务幂等校验。

验收：创建、查询、修改、删除分组和任务，服务重启后数据仍存在。

### 里程碑 M2：HTTP 下载 worker

- 实现 DownloadManager。
- 实现 worker 并发控制。
- 实现漫画章节图片下载。
- 实现 `.torrent` 下载。
- 实现暂停、恢复、取消、重试。
- 实现临时文件和断点续传。

验收：能够从现有漫画源下载完整章节，并在中断后继续下载。

### 里程碑 M3：进度和文件获取

- 实现 SSE 事件中心。
- 完善任务进度和速度计算。
- 增加 CBZ 打包。
- 增加完成文件接口。

验收：浏览器可观察实时进度并下载完成文件。

### 里程碑 M4：前端管理

- 增加下载管理页。
- 增加分组操作。
- 在章节和资源详情页增加创建任务按钮。
- 增加章节组预览、确认和批量展开。
- 增加批量暂停、恢复、取消。

验收：不打开 API 文档即可完成完整下载流程。

## 12. 测试要求

### 单元测试

- 状态转换合法性。
- `downloading + requested_action` 的最终状态竞争。
- 幂等键重复提交。
- 文件名清洗和路径逃逸拦截。
- Range 请求计算。
- 重试次数和失败状态。
- `pending/downloading` 服务重启恢复。
- 进度、速度和 ETA 计算。
- SQLite Repository CRUD。

### API 测试

- 分组 CRUD。
- 任务创建和过滤。
- 暂停、恢复、取消、重试。
- 完成任务文件下载。
- 未完成任务拒绝文件下载。
- SSE 事件格式。
- 章节组预览、确认、批量展开和批次汇总。
- 分组删除只解除关联，不删除任务文件。

### 集成测试

- 使用本地 HTTP 测试服务器模拟图片源。
- 模拟连接中断后恢复下载。
- 模拟签名 URL 失效后重新获取章节地址。
- 服务重启后恢复排队任务。

现有搜索、详情、章节、资源 API 测试必须继续通过。

## 13. V1 验收标准

- 可以创建下载分组。
- 可以从章节详情创建下载任务。
- 服务在后台持续下载，不阻塞搜索和阅读接口。
- 页面可显示实时进度、速度、ETA 和文件计数。
- 可以暂停、恢复、取消和重试。
- 下载任务在服务重启后可以恢复。
- 图片章节可以保存为 CBZ。
- `.torrent` 文件可以保存并通过浏览器获取。
- 章节组可以预览数量后批量创建独立任务。
- 同一章节重复提交不会产生重复任务。
- 下载路径不会被文件名或请求参数逃逸。
- 现有功能和测试不回归。

## 14. 明确不在 V1 解决的问题

- 直接实现 BitTorrent 协议。
- 磁力任务的种子解析和做种。
- 多用户权限和账号系统。
- 跨机器共享下载队列。
- 公网访问的完整安全体系。
- 自动追更和定时下载。

完成 V1 后，再根据实际使用情况决定是否接入 aria2/Transmission、桌面客户端和自动追更功能。

## 15. 实施记录（2026-09-29）

- M1 已实现：下载配置、Pydantic 创建模型、SQLite WAL 初始化、Repository、分组和任务 API、幂等创建。
- M2 已实现：后台 worker、图片有限并发、种子 Range 续传、暂停/恢复/取消/重试、重启恢复和失败清理恢复。
- M3 已实现：有界且合并进度的 SSE 订阅、15 秒 heartbeat、速度/ETA、CBZ 打包、完成文件与 Range 获取。
- M4 已实现：下载管理页、分组操作、任务筛选和详情、单章/种子入口、章节组预览确认和批量操作。
- 验证：118 项离线测试通过，其中 77 项为原有测试；本地 HTTP 图片源集成测试通过。
- 浏览器验证：Playwright 检查分组 CRUD、单章创建、批次确认、种子入口、文件获取和批量控制；桌面与手机截图均已检查。
- 尚未执行真实上游站点的完整下载验收；浏览器检查通过 fixtures 模拟下载接口，不产生真实下载任务。

实现细节与约定：

- 图片按已完成文件恢复，未完成的单张图片重新传输；种子文件按 `.part` 大小续传。
- 分组保存路径在任务创建时写入任务参数，后续修改分组路径不移动已有文件。
- 新增批次关联表，让重复章节可关联多个批次，旧批次仍可查询完整成员；后台执行对象仍只有独立章节任务。
- 重复提交已取消任务时复用原任务记录并重新排队；对失败任务使用显式重试操作。
- 删除暂停任务前需先取消；删除失败保留 `cleanup_state=pending`，启动时继续清理。
- 使用单个 uvicorn 应用进程，worker 数量通过 `JUJUBE_DOWNLOAD_WORKERS` 配置。
- 前端图标使用内嵌 Lucide 图标数据，不依赖 CDN；许可保存在 `web/LUCIDE-LICENSE`。

验证命令：

```bash
python -m pytest
python scripts/download_ui_check.py --executable "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
```


