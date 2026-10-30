# edgetunnel-proxyip

每 6 小时（北京时间 05:00、11:00、17:00、23:00）用 GitHub Action 经自己的 EdgeTunnel Worker 筛一遍公共 ProxyIP，结果：

- 写进 `proxyip/us.txt`（逗号分隔，可直接粘贴）和 `proxyip/us.json`（测量明细），提交回仓库；
- 同步成 Cloudflare DNS 的 TXT 记录 `us.proxyip.<你的域名>`，EdgeTunnel 的 PROXYIP 填这个域名即可自动跟随。

EdgeTunnel 不能直接读 raw 链接当 ProxyIP（`https://` 开头会被当成 HTTPS 代理），所以要经 TXT 记录中转。Worker 解析域名时先查 TXT，按逗号拆分，支持 `ip:端口`，每个实例缓存至少 5 分钟。

## 运行结构

一个 workflow，matrix 起多个 runner。当前 Worker 放在东京，所以只跑 `apac`（东京、新加坡）、`hk`（香港）和 `res`（住宅）三份；`us`（美西）已停用——从东京测美西每条都是跨太平洋，17–19 Mbps 下行，测不出好坏，停用后 `us.proxyip` 那条 TXT 保持上一份好数据不变。哪天把 Placement 换回美西，把 workflow 里那段注释打开、门槛恢复即可。三个列表同时跑、各自独立测速，互不抢带宽；跑完由单独的 `publish` job 汇总三份结果，一次提交，并按各自变量更新三条 TXT 记录。某个列表这一轮失败时不会覆盖旧列表、也不会动它的 DNS 记录（靠 `<列表>.ok` 标记判断）。

## 筛选方法

候选来自面板「获取更多 PROXYIP」同一数据源 `zip.cm.edu.kg.cmliussss.net/all.json`，按国家 US、CF 机房 SJC/LAX/SEA/PDX 过滤（约 1400 个）。每个候选生成一个 `/proxyip=<ip>` 的 VLESS 节点，共用同一个入口，用 mihomo 经 Worker 访问 CF 托管站点：

1. 全部测两次延迟（并发 6；并发高于 8 会让延迟整体虚高），两次都成功才算活，取最快的 40 个——先便宜地剔掉不稳的；
2. 这 40 个测 4 轮，必须每轮都成功，按 `中位数 + 0.3 × 最大值` 排序；
3. 前 20 个**并发**检查（每个候选独占一个本地端口，不再逐个切换节点）：经 Worker 看到的出口必须是 IPv4、不是 WARP 或 CF 自有 IP（X 对 IPv6 出口一律回 403 `IPv6`）；x.com 首页必须 200，api.x.com 不能 403，ChatGPT 要能连通；
4. 通过的**并发**测带宽（各 4 路）：下载 `registry.npmjs.org` 上约 4 MB 的 tgz，上传 3 MB 到 `cdnjs.cloudflare.com`（CF 收下后丢弃），单个传输 15 秒超时。下行低于 20 Mbps 或上行低于 5 Mbps 的淘汰；按 `√(下行×上行)` 排序，延迟每多 100 ms 扣 10%，留下前 6 个。

5. 决赛单路复测：初筛靠前的 16 个逐个单流复测上下行，用复测结果决定排序和是否达标。

  并行初筛会把 runner 的链路摊给 4 路，数字偏低、彼此拖慢；所以并行只用来淘汰和排序，最终发布的门槛与名次由单路复测决定。

改并发前后对比：原先第 3、4 步一次只测一个候选，4 MB 下载 + 3 MB 上传按最坏超时算，30 个候选能拖到 40 分钟；现在第 3 步 6 路并行、第 4 步 4 路并行，整轮压缩到几分钟。代价是同一时刻有多个传输共享线路，带宽数字比单测略低，用于排序和设门槛足够。

带宽目标必须在 CF 上，否则 Worker 直连、不经过 ProxyIP。`speed.cloudflare.com` 不能用：EdgeTunnel 会在 Worker 内部直接回 204，测出来是假的。数字是 GitHub runner（美国）测到的 Worker → ProxyIP 这一段，不是你本地的网速。门槛可用环境变量 `MIN_DOWN` / `MIN_UP` 调整。

通过的少于 3 个时脚本报错退出，不覆盖旧列表、不改 DNS。

## 设置

1. Cloudflare 建 API Token：模板「Edit zone DNS」，Zone Resources 只选 你的域名（例如 `example.com`）。
2. 本仓库 Settings → Secrets and variables → Actions：
   - Secrets：`SUB_URL` = 订阅链接（含 token）；`CF_API_TOKEN` = 上一步的 token
   - Variables：`CF_ZONE` = 你的域名（例如 `example.com`）；`CF_RECORD` = `us.proxyip.<你的域名>`；可选 `CF_ZONE_ID`（token 看不到 zone 列表时填）、`ENTRY_IP`
   - 每份列表各有一条记录变量，没设的那份只提交 `proxyip/*.txt`、不发 DNS：`CF_RECORD`（us）、`CF_RECORD_APAC`（apac）、`CF_RECORD_HK`（hk）、`CF_RECORD_RES`（res-住宅）
3. Actions 页手动 Run workflow 一次，确认 `proxyip/us.txt` 已生成、DNS 里出现 TXT 记录。
4. EdgeTunnel 面板「反代」→ PROXYIP 改成 `us.proxyip.<你的域名>`，保存，客户端更新订阅。

验证：浏览器打开 `https://claude.ai/cdn-cgi/trace`，`ip=` 应是 `us.txt` 里的某个。

## 注意

- 只配合 Worker Placement 在美西（如 AWS us-west-1，响应头 `cf-placement: remote-SJC`）使用。切回东京时要换日本列表。
- Runner 在美国，入口段和你本地不同，但对所有候选相同；排序比较的是 Worker → ProxyIP 这一段。
- 每次运行约 3000 次请求打到 Worker（美国 + 住宅），每天 4 次约 1.2 万次，在免费额度（每天 10 万次）内。带宽测试每次约传 1.3 GB，Workers 不按流量计费。
- 仓库建议公开：公开仓库 Action 分钟数不限；订阅链接和 CF token 都在 Secrets 里，不会出现在日志或文件里。
- 本地运行：`SUB_URL=... MIHOMO=./mihomo python3 screen.py`（需关闭 TUN，mihomo 用 v1.19.32）。

## 东京 + 新加坡 ProxyIP（apac.proxyip.<你的域名>）

与美西列表同一个 workflow，作为 matrix 的另一个 runner 同时跑（互不共享网络路径）。与美国列表同样的筛选步骤（连通、多轮延迟、出口必须是该 IP 本身的 IPv4、x.com 200 / api.x.com 非 403、上下行带宽），候选是数据源里国家为 JP 或 SG、CF 机房为 NRT / KIX / SIN 的 IP。

最终 8 个，按配额 **东京 4 + 新加坡 4** 各取本国最好的；某一边不够 4 个时，用另一边的补足。写 `proxyip/apac.txt`，发布到 Variable `CF_RECORD_APAC` 指定的 TXT 记录。

注意：测速是经你当前的 Worker 做的，结果取决于 Placement 在哪。**Worker 在东京时**，这一份是本地段，用正常门槛（上行 ≥5 Mbps）；**美西那一份**因此隔着太平洋，上行会被压在 4.5 Mbps 上下，所以它的门槛放低到 3 Mbps，作为备份列表。哪天把 Placement 换回美西，两边要对调。

视频检查同样受益：`video.twimg.com` 在日本解析到 Cloudflare，Worker 在东京时视频也走本地区，不再跨太平洋。

另外，出口检查会在 x.com、claude.ai、www.cloudflare.com 三个 trace 端点间重试，单个端点偶发空响应不再被判成「出口不对」。

## 香港 ProxyIP（hk.proxyip.<你的域名>）

matrix 里的 `hk` 任务，同一个 workflow 同时跑。候选是数据源里国家为 HK、CF 机房为 HKG 的 IP（192 个），筛选步骤与其它列表一致：连通、多轮延迟、出口必须是 IPv4 且不是 WARP / CF 自有、x.com 200 / api.x.com 非 403、上下行带宽、决赛单路复测。

与美西/日新不同的有三处，都是第一次跑出来的结论：

- **关掉候选预筛**：这 192 个候选只有 26 个的 `clientIp` 记录是 IPv4，其余记的是 IPv6 出口。那份记录是别人测的，不代表现在不能用，所以用 `PRE_V4=0` 让 192 个全部进第 1 步；出口判定仍在第 3 步，口径没变（必须 IPv4、不是 WARP、不是 CF 自有），只是把判断从「抄记录」改成「实测」。
- **先测记录过 IPv4 出口的那批**：第 3 步只取前 `SITE_POOL` 个，而香港按延迟排最快的 30 个里 28 个是 IPv6 出口、只有 2 个 IPv4——能过的还没被测就被名次截掉了。现在排序改为「记录是 IPv4 出口的优先，其次按延迟」，`SITE_POOL=40` 足以覆盖全部 26 个。这只影响谁被测，不影响发布名次（名次仍由第 4、5 步的带宽决定）。
- **门槛下调**：香港池子偏弱，实测两个 IPv4 出口分别是 19.7/4.3 和下载失败/4.3 Mbps，所以下行门槛 15、上行 4 Mbps（apac 是 20/5）。要收紧就改 workflow 里那一行的 `MIN_DOWN` / `MIN_UP`。

名额按「能过多少留多少」：`TOP_N=12` 只是上限，最多留 12 个（决赛单路复测池 16 个）；`MIN_KEEP=2` 是覆写门槛，不足 2 个通过才整轮失败、不覆盖 `hk.txt`、不动 TXT 记录。

写 `proxyip/hk.txt`（+ `hk.json` 明细），发布到 Variable `CF_RECORD_HK` 指定的 TXT 记录，没设就只提交文件不发 DNS。香港这份适合给需要香港出口的网站单独做一个节点（path 写 `/proxyip=hk.proxyip.<你的域名>`），不要直接把它当全局 PROXYIP 用——它只有香港一个地区。

## 住宅 ProxyIP（res.proxyip.<你的域名>）

与其它列表同一个 workflow，作为 matrix 的另一个 runner 同时跑。国家：五眼（US/CA/GB/AU/NZ）+ 西欧（IE/DE/NL/FR/BE/LU/CH/AT/IT/ES/PT/DK/SE/NO/FI/IS）。

1. 候选：同一数据源，国家在上述列表内、记录的出口是 IPv4（约 3500 个）；
2. ip-api.com（免费、无需 key）去掉 hosting / proxy（剩约 900 个）；
3. 经 Worker 并发做便宜的实测：能连通，且 CF 站点看到的出口就是这个 IP 本身（IPv4、不是 WARP / CF / 转发出去的别的地址）。挂掉或出口不对的不会消耗 ipapi.is 额度；
4. **最后才做住宅与纯净度识别**：只对第 3 步通过的 IP 调 ipapi.is（Secret `IPAPI_IS_KEY`），要求 `is_datacenter=false` 且 `company.type=isp`，不是 VPN / Tor / 滥用者，纯净度 `100×(1−company.abuser_score)` ≥ 90。结果缓存 7 天（`proxyip/res-cache.json`），每轮最多新查 200 个，每天 4 轮仍在免费额度（每天 1000 个）内；
5. 只对住宅 IP 做耗时的检查（同样并发，各 4 路）：x.com 200、api.x.com 非 403，带宽下行 ≥5、上行 ≥2 Mbps；
6. 按纯净度、再按带宽和延迟排序，最多 8 个，写 `proxyip/res.txt` 并发布到 `CF_RECORD_RES`。少于 2 个不覆盖。

「住宅」只代表 ipapi.is 判定所属运营主体类型是 ISP、网段不是机房；纯净度是网段层面的，不是单个 IP 的风险分。这类 IP 多是别人家里开放的设备，寿命短、速度不稳，适合做「需要干净 IP 的少数网站」专用节点，不适合当主力。

用法：面板里的 PROXYIP 是全局的，所以住宅要单独做一个节点，path 写 `/proxyip=res.proxyip.<你的域名>`，再在规则里把需要的网站分到这个节点。
