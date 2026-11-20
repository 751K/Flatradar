# Canvas（原 OurCampus）侦察记录

侦察日期：2026-11-20。以下全部是当天的观察，不是对站点长期行为的承诺。

## 1. 结论

- OurCampus 已并入 Greystar 的全球学生 / 年轻专业人士品牌 **Canvas**
  （canvas-world.com）。ourcampus.nl 302 到
  `canvas-world.com/en/locations/netherlands/amsterdam/diemen`；ourcampus.com /
  .eu 已连不上。
- Canvas 在荷兰登记了 5 栋楼。4 栋的预订后台仍是 RentCafe（Yardi），与现有
  `scrapers/ourdomain.py` / `scrapers/ourcampus.py` 同一模板；第 5 栋
  （Rotterdam Brainpark）用 Presendoo，是另一套系统。
- 用现有爬虫只换 base / slug / property_id，**Diemen 和 Rotterdam Blaak 当天
  就能完整抓到单元表**，解析器不用改，GET 和 POST 两种请求形状都行。
- 主要障碍不是抓取，而是**状态判定**：Diemen 的 31 套全部挂着 `Contact` 按钮、
  租金写 `Call for Details`，但下一步的租约页写明“提交申请即进入该单元的抽签，
  不保留房源”。现有逻辑会把 `Contact` 当作没见过的按钮文字放行成
  `Available to book`，上线会误报。
- OurDomain（ourdomain.nl）没有改名，页面里没有 Canvas 字样。

## 2. 荷兰楼盘

| 楼盘 | 官网路径（/en/locations/netherlands/…） | 定位 | 预订后台 | property | 当天结果 |
|---|---|---|---|---|---|
| Canvas Student Amsterdam Diemen（原 OurCampus） | `amsterdam/diemen` | 学生 | RentCafe `canvas-student-amsterdam-diemen-3p-rentcafewebsiteuk.securerc.co.uk/onlineleasing/shell-ourcampus-amsterdam-diemen-3p` | 186607（旧 186609） | 31 套，全部 `Contact` / `Call for Details`，实为抽签 |
| Canvas Living Rotterdam Blaak | `rotterdam/blaak` | 专业人士 | RentCafe `canvas-world.securerc.co.uk/onlineleasing/andre-van-der-louwbrug-4-rotterdam-3011-wm` | 186511 | 6 套可订，€1.711–1.733 |
| Canvas Student Utrecht | `utrecht/canvas-utrecht` | 学生 | RentCafe `canvas-student.securerc.co.uk/onlineleasing/canvas-utrecht` | 182857 | 0 套，页面写明只提前约一个月放房 |
| Canvas Living Amsterdam Bullewijk（Ensemble） | `amsterdam/amsterdam-bullewijk` | 专业人士 | RentCafe `ensemble-rentcafewebsiteuk.securerc.co.uk/onlineleasing/ensemble` | 186101 | 抓不到，见 §4.3 |
| Canvas Living Rotterdam Brainpark | `rotterdam/brainpark` | 专业人士 | Presendoo `canvas-living-at-rotterdam-brainpark.presendoo.app` | — | 未接入，纯前端 SPA |

官网每个楼盘页里的 property ID 与 RentCafe 页面里的不完全一致：Bullewijk 官网
186103 对 RentCafe 186101，Utrecht 官网 182855 对 RentCafe 182857。以 RentCafe
页面为准。

## 3. 官网（canvas-world.com）

- Next.js + Sitecore，房型数据在 RSC flight payload（`self.__next_f.push`）里。
  每个房型有 `unitsAvailable`、`earliestUnitAvailableDate`、`price`（形如
  `"000=932"`）、`floorPlanArea`、`floorPlanInclusions`、`showSoldOut`、
  `waitlistCTA` 等字段。
- 这些数字是官网内容层的缓存，与 RentCafe 实时数据不一致：Bullewijk 官网房型
  标着几十套可用，但 RentCafe 那边拿不到任何单元；Diemen 官网标价 0。**只适合
  拿来发现楼盘和 RentCafe 地址，不适合当房源来源。**
- `sitemap.xml` 可以列出全部楼盘页，新楼盘出现在这里。

## 4. RentCafe 各楼观察

### 4.1 Diemen：Contact = 抽签

- `floorplans.aspx` 能发现两个 floorplan：1112902（Furnished Student Apartment -
  1 Person，30 套）和 1112903（2 Person，1 套）。代码里写死的旧 floorplan ID
  不再适用。
- 单元行结构不变（`AvailUnitRow` / `unitrow_<id>`），按钮是
  `<input value='Contact' data-function='contactUsLink' onclick="showDialog('Contact Us', …)">`，
  租金列是 `Call for Details`。
- 官网的 “Check Availability” 直接链到
  `oleapplication.aspx?stepname=RentalOptions&myOlePropertyId=186607&FloorPlanID=…&UnitID=…`。
  这一页给出每个单元的实际费用和说明。以 #1202 为例：
  - Base Rent €823,02
  - SC Other €144,98，SC Electricity €53,98，SC Heating €39,27
  - Administration fee €6,25，Water €10,00
  - 可入住 30-11-2026，租期最少 12 个月，在读期间自动续约
  - 原话：“submitting this application enters you into the lottery for this unit;
    it does not reserve the apartment.”
- 结论：Diemen 当前是**按单元抽签**，与 2026-09-15 见过的 `Join Lottery` 是同一
  语义，只是按钮文字换成了 `Contact`。

### 4.2 Rotterdam Blaak：正常可订

- 8 个 floorplan（Executive / Standard / Short Stay 各档 Studio 和 1 Bedroom），
  当天只有 “1 Bedroom Executive” 有 6 套，租金 €1.711–1.733。
- 页面说明：每周三 12:00 放新房；没有房的房型可以订阅通知；2026-02 起申请条件有
  变化。

### 4.3 Bullewijk（Ensemble）：入口是租约类型落地页

- `floorplans.aspx` 不直接列房型，而是一个 “Lease Option Page”，四个按钮调用
  `GetNextStepContent('Student' | 'Corporate' | 'ShortTerm' | 'Market')`。
  它写 cookie `ApplicationLeaseType`，再请求 `rcformsave.ashx?...formName2=FlexLandingPage`。
  这个请求不带 AJAX 头会 403，带上后返回一段跳转脚本：
  - Market → `floorplans.aspx?stepname=Floorplan&…&myLeaseCafeType=0&myStuApplicantType=Market`
  - Student → 同上，`myLeaseCafeType=2`
  - ShortTerm → `shorttermapartmentavailability.aspx?…`
  - Corporate → `contactus.aspx?…`
- 跟随跳转后，Market 又回到落地页，Student 返回 500。在真实浏览器里点 Market
  也一样回到落地页。**当天这栋楼在 RentCafe 上没有可浏览的房源**，原因不明，
  可能是新楼尚未开放网上申请。

### 4.4 Utrecht：当天无房

`floorplans.aspx` 列出 14 个房型名称，但没有可发现的 floorplan ID。页面原话是
房源有了才会出现，且只提前约一个月知道空房、不能预订更远的日期。现有爬虫在这里
找不到 floorplan，会把扫描标为不完整，而不是“0 套”。

## 5. 接入时要改的地方

> v2.3.0 已完成 Diemen 的第 1–3 项和第 6 项的后端部分（平台 key 改为 `canvas`），
> 第 4 项在部署时用 `SHADOW_SOURCES` 做。Blaak、Utrecht 和 App 端平台表尚未处理。

1. **地址**：`OurCampusScraper.BASE` / slug / property_id 改为 Diemen 的新值。
   floorplan 改为每轮从 `floorplans.aspx` 发现，不写死。
2. **按钮语义**：`scrapers/rentcafe_parse` 把 `Contact` 识别为一个明确状态。
   Diemen 的证据指向抽签，可以映射到现有的 `Available in lottery`。不能再走
   “没见过的文字按可订放行”。
3. **租金**：`Call for Details` 不能当租金。要么留空；要么每个单元多请求一次
   RentalOptions 页取 Base Rent 和服务费（Diemen 一轮约 31 个请求）。
4. **首轮静默**：新地址的单元 ID 与旧库里的 `oc_` 记录不同，首轮会把全部单元
   当新房源。上线时要先静默入库，不要推送（2026-11-20 Codomo 扩城时已出现过
   一次批量误推）。
5. **其他楼**：
   - Blaak 可以直接按 OurDomain 形状接入。它是专业人士楼，租客类型要单独登记。
   - Utrecht 要处理“无 floorplan ID 不等于结构变了”的情况，否则长期显示不完整。
   - Bullewijk 和 Brainpark 暂不接。
6. **命名**：用户可见的平台名是否改成 Canvas，要与 iOS / Android 的平台表一起改。
