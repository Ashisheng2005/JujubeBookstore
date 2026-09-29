/**
 * 模拟 web/index.html 里前端的完整调用链，验证数据契约。
 * 用法: node scripts/ui_flow_check.mjs [baseUrl]
 */
const BASE = process.argv[2] || 'http://127.0.0.1:8000';

let pass = 0, fail = 0;
const check = (name, cond, extra = '') => {
  if (cond) { pass++; console.log(`  OK   ${name}${extra ? ' — ' + extra : ''}`); }
  else { fail++; console.log(`  FAIL ${name}${extra ? ' — ' + extra : ''}`); }
};

async function get(path, raw = false) {
  const t0 = Date.now();
  const res = await fetch(BASE + path);
  const ms = Date.now() - t0;
  if (!res.ok) throw new Error(`HTTP ${res.status} ${path}`);
  const body = raw ? Buffer.from(await res.arrayBuffer()) : await res.json();
  return { body, ms, type: res.headers.get('content-type') || '' };
}

const imageKind = (buf) => {
  if (buf.subarray(0, 3).equals(Buffer.from([0xff, 0xd8, 0xff]))) return 'jpeg';
  if (buf.subarray(0, 4).toString('latin1') === 'RIFF') return 'webp';
  if (buf.subarray(0, 4).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47]))) return 'png';
  if (buf.subarray(0, 4).toString('latin1') === 'GIF8') return 'gif';
  return '?';
};

const proxyImg = (src, url) => `/api/${src}/image?url=${encodeURIComponent(url)}`;

// --- 前端启动时做的两件事 ---
console.log('\n[启动] 健康检查 + 源清单');
const health = (await get('/healthz')).body;
check('healthz.status', health.status === 'ok');
check('healthz 列出漫画源', Array.isArray(health.sources) && health.sources.length === 3, health.sources.join(','));
check('healthz 列出资源源', Array.isArray(health.resource_sources) && health.resource_sources.includes('dmhy'));
check('healthz 代理开关', typeof health.proxy_enabled === 'boolean', `proxy_enabled=${health.proxy_enabled}`);

const comics = (await get('/api/sources')).body;
const resources = (await get('/api/resources')).body;
check('/api/sources 字段完整', comics.every(s => s.key && s.name && s.kind === 'comic'));
check('/api/resources 字段完整', resources.every(s => s.key && s.name && s.kind === 'resource'));
check('下拉框需要 needs_proxy/prefer_proxy', comics.every(s => 'needs_proxy' in s && 'prefer_proxy' in s));

// --- 漫画模式：搜索 -> 详情 -> 章节 -> 图片（逐个源） ---
const comicCases = [
  ['zaimanhua', '火影', '80808'],
  ['mangabz', '海贼王', '139'],
  ['mangacopy', '火影忍者', 'huoyingrenzhetedian'],
];
for (const [src, keyword, comicId] of comicCases) {
  console.log(`\n[漫画] ${src} · 搜索「${keyword}」`);
  const search = await get(`/api/${src}/search?q=${encodeURIComponent(keyword)}&page=1`);
  check('搜索结果非空', search.body.items.length > 0, `${search.body.count} 条 / ${search.ms}ms`);
  const item = search.body.items[0];
  check('卡片字段 (id/title)', Boolean(item.id && item.title), item.title?.slice(0, 24));

  const detail = await get(`/api/${src}/comic/${encodeURIComponent(comicId)}`);
  const d = detail.body;
  check('详情字段 (title/cover/tags)', Boolean(d.title && d.cover && d.tags), `${d.title?.slice(0, 18)} / ${d.chapters.length} 章`);
  check('章节有 group 字段（前端按组分栏）', d.chapters.every(c => typeof c.group === 'string'));
  const chapter = d.chapters[0];

  const chap = await get(`/api/${src}/comic/${encodeURIComponent(comicId)}/chapter/${encodeURIComponent(chapter.id)}`);
  check('章节图片非空', chap.body.images.length > 0, `${chap.body.count} 页 / ${chap.ms}ms`);

  const img = await get(proxyImg(src, chap.body.images[0]), true);
  const kind = imageKind(img.body);
  check('图片中转返回真图', kind !== '?', `${kind} ${img.body.length}B ${img.type}`);
  if (d.cover) {
    const cover = await get(proxyImg(src, d.cover), true);
    check('封面中转返回真图', imageKind(cover.body) !== '?', `${cover.body.length}B`);
  }
}

// --- 资源模式：搜索（带分类） -> 详情 ---
console.log('\n[资源] dmhy · 搜索「海贼王」category=漫畫');
const rsearch = await get(`/api/resources/dmhy/search?q=${encodeURIComponent('海贼王')}&category=${encodeURIComponent('漫畫')}`);
check('资源结果非空', rsearch.body.items.length > 0, `${rsearch.body.count} 条 / ${rsearch.ms}ms`);
const ritem = rsearch.body.items[0];
check('列表字段 (id/title/magnet)', Boolean(ritem.id && ritem.title && ritem.magnet), `[${ritem.category}] ${ritem.size}`);
check('列表带发布时间/做种数字段', 'published_at' in ritem && 'seeders' in ritem);

const latest = await get('/api/resources/dmhy/latest?page=1');
check('最近更新可用', latest.body.items.length > 0, `${latest.body.count} 条 / ${latest.ms}ms`);

const rdetail = await get(`/api/resources/dmhy/item/${encodeURIComponent(ritem.id)}`);
const rd = rdetail.body;
check('详情字段 (magnet/torrent)', Boolean(rd.magnet && rd.torrent), rd.torrent?.slice(-28));
check('详情含文件列表', Array.isArray(rd.files) && rd.files.length > 0, `${rd.files.length} 个文件`);
check('种子是 https 直链', /^https:\/\/dl\.dmhy\.org\/.+\.torrent$/.test(rd.torrent || ''), rd.torrent);
// 注意：种子文件本身的下载校验在 scripts/smoke.py（走服务端的代理客户端，实测 19,894 字节 bencode）。
// 这里不直接 fetch：dl.dmhy.org 在本机 DNS 被污染，Node 直连会失败，那属于网络环境而非接口问题。
check('磁力是合法 magnet 链接', /^magnet:\?xt=urn:btih:[A-Z2-7]+/.test(rd.magnet || ''));

// --- 错误分支（前端要显示后端返回的 detail） ---
console.log('\n[错误处理]');
const bad = await fetch(BASE + '/api/nope/search?q=x');
check('未知源 404 且带 detail', bad.status === 404 && Boolean((await bad.json()).detail));
const badRes = await fetch(BASE + '/api/resources/dmhy/item/727506');
check('dmhy 纯数字 id 404 且带提示', badRes.status === 404 && /slug/.test((await badRes.json()).detail));

console.log(`\n===== 结果: ${pass} 通过 / ${fail} 失败 =====`);
process.exit(fail ? 1 : 0);
