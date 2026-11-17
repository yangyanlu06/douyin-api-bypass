---
name: douyin-resolve
description: 用 lapian_bridge.py 把抖音分享链接解析成本地 mp4，给 agent 调用。走 headless Edge（playwright, channel=msedge），从页面自身 /aweme/v1/web/ 的 JSON 取 video.play_addr；失败即停、不重试、不换第三方接口。
---

# douyin-resolve

给 agent 用的抖音取流工具。一条命令，一个 JSON 结果。

## 调用

```
python lapian_bridge.py "<整段分享口令或链接>" --out <目录> [--timeout 25]
```

- 输入可以是一整段分享口令（含【作者的作品】、emoji、尾部 `:8pm daN:/` 之类），脚本自己取第一个 `http(s)` 链接；
- 首次运行会启动 headless Edge，约 8–15 秒；给命令 180–240 秒超时；
- 依赖：本目录的 `douyin_lapian.py`（同方法的上游实现，被 import 复用取流逻辑）。

## 读结果

只认最后一行以 `__DWP_JSON__` 开头的 JSON，前面是进度输出：

```json
{"ok": true, "mp4": "/abs/out/dy_7708061779860224933.mp4",
 "meta": {"aweme_id": "...", "title": "...", "duration": 13.77, ...}}
```

```json
{"ok": false, "reason": "..."}
```

`ok=false` 是最终结论。**不要**重试，不要改投第三方解析接口，不要换 UA 刷——按「失败即停」处理，把 `reason` 原样报给用户。

## 什么时候会失败（都要如实告知）

- 图文作品（`/note/`）：没有视频。需要正片图时，用 `Baiduspider` UA 直接 GET `www.douyin.com/note/<id>`，HTML 里有 `~tplv-dy-aweme-images` 地址。
- 作品被删 / 短链过期：页面会跳到 `douyin.com/jingxuan?previous_page=web_video_404_link`。
- 搜索页会触发验证码；视频页、图文页不会。
- `/slides/<id>` 是混合轮播（一屏多个视频），会被重定向到 `/note/<id>`，能取到第一条视频。

## 硬约定

- 不逆向签名、不去水印、不批量爬取、不并发。
- 不把 `state.json`（登录态 cookie）提交进仓库或发给别人。
- 媒体只落本机，不上传第三方。
