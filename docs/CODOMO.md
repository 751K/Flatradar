# Codomo（原 Holland2Stay）平台维护手册

## 1. 平台概况

2026-11 Holland2Stay 改名 Codomo：`www.holland2stay.com` 全站 301 到
`https://codomo.eu`。旧站的加密 GraphQL 接口、operation 白名单、REST 快速通道和
GraphQL 下单流程随之全部失效，本项目对应的代码已经删除。

平台 key 随改名从 `holland2stay` 改为 `codomo`。升级后第一次启动时，
`mstorage/_codomo_rename.py` 会一次性改写库里的旧数据（见第 5 节）。

Codomo 覆盖荷兰、奥地利、西班牙、葡萄牙，本项目只抓荷兰
（`/homes/nl/en`）。

## 2. 抓取链路

| 项 | 说明 |
|---|---|
| 入口 | `scrapers/codomo.py` 的 `CodomoScraper`，纯 HTTP（curl_cffi），不需要浏览器 |
| 数据来源 | `https://codomo.eu/homes/nl/en?sort=newest&r=<轮次时间戳>&page=N`，每页 25 条，服务端渲染的 HTML。必须固定排序：默认排序在同日入住的房源之间不稳定，翻页会重复一套、漏一套。`r` 让同一轮的各页绕开 Cloudflare 的逐页缓存、取同一时刻的数据，否则各页是不同时刻的快照，边界同样会重复和遗漏 |
| 解析 | `scrapers/codomo_parse.py`，只依赖标准库 |
| 批次 | 与 Magis / Plaza 相同：每批次抓一遍全国列表，各城市 task 从同一份结果里切分 |
| 城市表 | `mcore/registry.py` 的 `KNOWN_CODOMO_CITIES`，订阅键 `CODOMO_CITIES` |

新站没有可用的 JSON 接口：前端只调 `/api/units/count` 这类计数接口；城市子页
（`/homes/nl/en/eindhoven`）只有楼盘介绍，没有房源卡片。所以只能解析全国列表页。

### 2.1 流式渲染

Next.js 用 React Suspense 流式输出，部分卡片的规格和价格一开始是
`<template id="P:N">` 占位符，真正的内容在文档末尾的 `<div hidden id="S:N">`
里，由内联脚本换进去。curl 拿到的是替换之前的原文，`codomo_parse` 会自己完成
这次替换。漏掉这一步时，约四分之一的卡片会缺价格和规格。

### 2.2 完整性

页面自报总数（`142 residences`）。只有当所有页都取到，且去重后的卡片数等于这个
总数时，本轮才标记为完整扫描。不完整的结果不参与「消失即下架」的状态收敛。

### 2.3 城市归属

城市按卡片 URL 里的城市段（`/homes/nl/en/{slug}/…`）归类，不按标题里的城市名。
The Hague 的 slug 是 `den-haag`。如果站点上了注册表里没有的城市，日志会出现
`Codomo 出现未登记的城市` 的 WARNING，这些房源不会分给任何城市。

订阅 key 沿用旧 H2S 的数字城市 ID（Eindhoven `29`），旧配置迁过来不用改值。

## 3. 字段与状态

| 卡片内容 | Listing |
|---|---|
| URL 末段 slug | `id`（与旧站 `url_key` 同形，新旧房源能接上） |
| 标题「地址, 城市」 | `name` 取地址（地图按 name + city 定位） |
| `Book directly` | `status = "Available to book"` |
| `Show interest` + 「N people have shown interest」 | `status = "Available in lottery"`，`Interested: N` |
| `€1,522 /month*` + `*Excl. €252/month …` | `price_raw = "€1774"`（含服务费，与旧站 price_range 同口径） |
| 日期 `Nov 18, 2026` | `available_from` |
| `Studio` / `1 bedroom` | `Type: Studio` / `Type: 1` |
| 面积、人数、楼层、装修、楼盘 | `Area` / `Occupancy` / `Floor` / `Finishing` / `Building` |
| `Student only` 徽标 | `Tenant: student only` |
| 其他徽标（`Parking included`、`Premium inventory` 等） | `Offer: …` |
| `Min 12 months` | `Min stay: 12 months` |

列表只给「可订」和「登记意向」两类房源，不再有 Reserved / Occupied，所以这个 feed
不覆盖「已预留」状态，状态收敛按「只列可订」的平台处理。

过滤维度登记了 floor / occupancy / type / offer / finishing。tenant 没有登记：只有
部分卡片带 `Student only` 徽标，没有徽标不能说明「不限租客」。能耗、合同类型、
片区只在详情页上，目前不抓。

## 4. 错误识别

| 响应 | 处理 |
|---|---|
| 403 | `BlockedError`，交给平台熔断 |
| 429 | 按 `RATE_LIMIT_BACKOFF` 重试，仍然 429 就抛 `RateLimitError` |
| 维护页，或被重定向离开 `codomo.eu`（全站维护时跳到占位域名） | `UpstreamMaintenanceError` |
| 其他非 200、传输异常 | `ScrapeNetworkError`（保留异常链，代理故障才能被识别） |
| 卡片数与自报总数对不上 | 不抛异常，本轮标记为不完整 |

## 5. 升级迁移

`mstorage/_codomo_rename.py` 在第一次启动时执行一次，执行状态记在 meta 键
`codomo_rename_v1`：

- `listings.source`、`round_stats.source` 的 `holland2stay`（以及多源之前留下的空值）
  改为 `codomo`
- 用户筛选和自动预订筛选里的 `allowed_sources`
- `SOURCES` / `SHADOW_SOURCES` / `SHARD_SIZES` / `SOURCE_MIN_INTERVALS` /
  `SOURCE_PEAK_MIN_INTERVALS` 里的平台名
- 设置 `CITIES` 改名为 `CODOMO_CITIES`；`AVAILABILITY_FILTERS`、`H2S_REST_*`、
  `WARM_BROWSER_LANE` 删除
- meta 里以平台名为键的运行状态，以及注册表快照里的 `CITIES`

`.env` 里还写着 `CITIES` 时，`CODOMO_CITIES` 缺省会读它（见 `config._load_targets`）。

## 6. 自动预订

暂不支持。旧站的 GraphQL 下单流程已经失效。新站的预订（`Book directly` /
`Show interest`）要登录账号才能看到，目前还没有侦察。`codomo` 不在
`AUTO_BOOK_SOURCES` 里，命中的房源只发通知。
