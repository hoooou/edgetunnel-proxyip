# edgetunnel-proxyip

每 6 小时（北京时间 05:00、11:00、17:00、23:00）用 GitHub Action 经自己的 EdgeTunnel Worker 筛一遍美西公共 ProxyIP，结果：

- 写进 `proxyip/us.txt`（逗号分隔，可直接粘贴）和 `proxyip/us.json`（测量明细），提交回仓库；
- 同步成 Cloudflare DNS 的 TXT 记录 `us.proxyip.<你的域名>`，EdgeTunnel 的 PROXYIP 填这个域名即可自动跟随。

EdgeTunnel 不能直接读 raw 链接当 ProxyIP（`https://` 开头会被当成 HTTPS 代理），所以要经 TXT 记录中转。Worker 解析域名时先查 TXT，按逗号拆分，支持 `ip:端口`，每个实例缓存至少 5 分钟。

## 筛选方法

候选来自面板「获取更多 PROXYIP」同一数据源 `zip.cm.edu.kg.cmliussss.net/all.json`，按国家 US、CF 机房 SJC/LAX/SEA/PDX 过滤（约 1400 个）。每个候选生成一个 `/proxyip=<ip>` 的 VLESS 节点，共用同一个入口，用 mihomo 经 Worker 访问 CF 托管站点：

1. 全部测一次延迟（并发 6；并发高于 8 会让延迟整体虚高），取最快的 40 个；
2. 这 40 个测 6 轮，必须每轮都成功，按 `中位数 + 0.3 × 最大值` 排序；
3. 前 20 个逐个检查：经 Worker 看到的出口必须是 IPv4、不是 WARP 或 CF 自有 IP（X 对 IPv6 出口一律回 403 `IPv6`）；x.com 首页必须 200，api.x.com 不能 403，ChatGPT 要能连通；
4. 通过的逐个测带宽（不并发）：下载 `registry.npmjs.org` 上约 20 MB 的 tgz，上传 8 MB 到 `cdnjs.cloudflare.com`（CF 收下后丢弃）。各测 2 次取较差的一次，下行低于 20 Mbps 或上行低于 5 Mbps 的淘汰；按 `√(下行×上行)` 排序，延迟每多 100 ms 扣 10%，留下前 6 个。

带宽目标必须在 CF 上，否则 Worker 直连、不经过 ProxyIP。`speed.cloudflare.com` 不能用：EdgeTunnel 会在 Worker 内部直接回 204，测出来是假的。数字是 GitHub runner（美国）测到的 Worker → ProxyIP 这一段，不是你本地的网速。门槛可用环境变量 `MIN_DOWN` / `MIN_UP` 调整。

通过的少于 3 个时脚本报错退出，不覆盖旧列表、不改 DNS。

## 设置

1. Cloudflare 建 API Token：模板「Edit zone DNS」，Zone Resources 只选 你的域名（例如 `example.com`）。
2. 本仓库 Settings → Secrets and variables → Actions：
   - Secrets：`SUB_URL` = 订阅链接（含 token）；`CF_API_TOKEN` = 上一步的 token
   - Variables：`CF_ZONE` = 你的域名（例如 `example.com`）；`CF_RECORD` = `us.proxyip.<你的域名>`；可选 `CF_ZONE_ID`（token 看不到 zone 列表时填）、`ENTRY_IP`
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

同一个 workflow 的第二个 job，跑在美国筛选之后。与美国列表同样的筛选步骤（连通、多轮延迟、出口必须是该 IP 本身的 IPv4、x.com 200 / api.x.com 非 403、上下行带宽），候选是数据源里国家为 JP 或 SG、CF 机房为 NRT / KIX / SIN 的 IP。

最终 8 个，按配额 **东京 4 + 新加坡 4** 各取本国最好的；某一边不够 4 个时，用另一边的补足。写 `proxyip/apac.txt`，发布到 Variable `CF_RECORD_APAC` 指定的 TXT 记录。

注意：测速是经你当前的 Worker 做的，结果取决于 Placement 在哪。Worker 在美西时，Worker → 东京/新加坡这一段是跨太平洋的单条流，实测上行被压在 4.2–4.8 Mbps（十几个不同 ProxyIP 几乎一致，说明瓶颈在这一段而不是 IP 本身），8 MB 上传也常超过 20 秒。因此 apac job 用了更宽的上行门槛（3 Mbps）和更长的超时（40 秒），避免把真实可用的 IP 当成不合格。把 Placement 改到东京后重新跑一轮，门槛应恢复成上行 5 Mbps。

另外，出口检查会在 x.com、claude.ai、www.cloudflare.com 三个 trace 端点间重试，单个端点偶发空响应不再被判成「出口不对」。

## 住宅 ProxyIP（res.proxyip.<你的域名>）

同一个 workflow 的第二个 job，跑在美国筛选之后（不同时压 Worker）。国家：五眼（US/CA/GB/AU/NZ）+ 西欧（IE/DE/NL/FR/BE/LU/CH/AT/IT/ES/PT/DK/SE/NO/FI/IS）。

1. 候选：同一数据源，国家在上述列表内、记录的出口是 IPv4（约 3500 个）；
2. ip-api.com（免费、无需 key）去掉 hosting / proxy（剩约 900 个）；
3. 经 Worker 并发做便宜的实测：能连通，且 CF 站点看到的出口就是这个 IP 本身（IPv4、不是 WARP / CF / 转发出去的别的地址）。挂掉或出口不对的不会消耗 ipapi.is 额度；
4. **最后才做住宅与纯净度识别**：只对第 3 步通过的 IP 调 ipapi.is（Secret `IPAPI_IS_KEY`），要求 `is_datacenter=false` 且 `company.type=isp`，不是 VPN / Tor / 滥用者，纯净度 `100×(1−company.abuser_score)` ≥ 90。结果缓存 7 天（`proxyip/res-cache.json`），每轮最多新查 200 个，每天 4 轮仍在免费额度（每天 1000 个）内；
5. 只对住宅 IP 做耗时的检查：x.com 200、api.x.com 非 403，带宽下行 ≥5、上行 ≥2 Mbps；
6. 按纯净度、再按带宽和延迟排序，最多 8 个，写 `proxyip/res.txt` 并发布到 `CF_RECORD_RES`。少于 2 个不覆盖。

「住宅」只代表 ipapi.is 判定所属运营主体类型是 ISP、网段不是机房；纯净度是网段层面的，不是单个 IP 的风险分。这类 IP 多是别人家里开放的设备，寿命短、速度不稳，适合做「需要干净 IP 的少数网站」专用节点，不适合当主力。

用法：面板里的 PROXYIP 是全局的，所以住宅要单独做一个节点，path 写 `/proxyip=res.proxyip.<你的域名>`，再在规则里把需要的网站分到这个节点。
