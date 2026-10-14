# edgetunnel-proxyip

每 6 小时用 GitHub Action 经自己的 EdgeTunnel Worker 筛一遍美西公共 ProxyIP，结果：

- 写进 `proxyip/us.txt`（逗号分隔，可直接粘贴）和 `proxyip/us.json`（测量明细），提交回仓库；
- 同步成 Cloudflare DNS 的 TXT 记录 `us.proxyip.yieti.top`，EdgeTunnel 的 PROXYIP 填这个域名即可自动跟随。

EdgeTunnel 不能直接读 raw 链接当 ProxyIP（`https://` 开头会被当成 HTTPS 代理），所以要经 TXT 记录中转。Worker 解析域名时先查 TXT，按逗号拆分，支持 `ip:端口`，每个实例缓存至少 5 分钟。

## 筛选方法

候选来自面板「获取更多 PROXYIP」同一数据源 `zip.cm.edu.kg.cmliussss.net/all.json`，按国家 US、CF 机房 SJC/LAX/SEA/PDX 过滤（约 1400 个）。每个候选生成一个 `/proxyip=<ip>` 的 VLESS 节点，共用同一个入口，用 mihomo 经 Worker 访问 CF 托管站点：

1. 全部测一次延迟（并发 6；并发高于 8 会让延迟整体虚高），取最快的 40 个；
2. 这 40 个测 6 轮，必须每轮都成功，按 `中位数 + 0.3 × 最大值` 排序；
3. 前 12 个逐个真实请求 ChatGPT 接口、api.x.com、x.com，全部通过的留下前 6 个。

通过的少于 3 个时脚本报错退出，不覆盖旧列表、不改 DNS。

## 设置

1. Cloudflare 建 API Token：模板「Edit zone DNS」，Zone Resources 只选 `yieti.top`。
2. 本仓库 Settings → Secrets and variables → Actions：
   - Secrets：`SUB_URL` = 订阅链接（含 token）；`CF_API_TOKEN` = 上一步的 token
   - Variables：`CF_ZONE` = `yieti.top`；`CF_RECORD` = `us.proxyip.yieti.top`；可选 `CF_ZONE_ID`（token 看不到 zone 列表时填）、`ENTRY_IP`
3. Actions 页手动 Run workflow 一次，确认 `proxyip/us.txt` 已生成、DNS 里出现 TXT 记录。
4. EdgeTunnel 面板「反代」→ PROXYIP 改成 `us.proxyip.yieti.top`，保存，客户端更新订阅。

验证：浏览器打开 `https://claude.ai/cdn-cgi/trace`，`ip=` 应是 `us.txt` 里的某个。

## 注意

- 只配合 Worker Placement 在美西（如 AWS us-west-1，响应头 `cf-placement: remote-SJC`）使用。切回东京时要换日本列表。
- Runner 在美国，入口段和你本地不同，但对所有候选相同；排序比较的是 Worker → ProxyIP 这一段。
- 每次运行约 2000 次请求打到 Worker，每天约 8000 次，在免费额度（每天 10 万次）内。
- 仓库建议公开：公开仓库 Action 分钟数不限；订阅链接和 CF token 都在 Secrets 里，不会出现在日志或文件里。
- 本地运行：`SUB_URL=... MIHOMO=./mihomo python3 screen.py`（需关闭 TUN，mihomo 用 v1.19.32）。
