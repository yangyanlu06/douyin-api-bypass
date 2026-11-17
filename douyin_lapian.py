#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音链接 -> 拉片（无水印 mp4 + 逐镜分镜表 + 每镜首/中/尾帧）
          -> 逐帧分析（全帧定量指标 CSV + 全片采样帧联系表 + 运动峰值/定格帧定位）

用法:
  python douyin_lapian.py "整段分享口令或链接" --out ./lapian_out
  python douyin_lapian.py "https://www.douyin.com/video/<id>" --step 0.25
  python douyin_lapian.py --batch candidate_pool.json --out ./audit --step 0.5 --limit 12

要点:
  * 播放直链优先从页面 /aweme/v1/web/ 接口 JSON 的 video.play_addr / bit_rate 里取，
    不再依赖 <video>.currentSrc（MSE 流常为 blob:，requests 无法下载）。
  * 逐帧分析对视频**每一帧**计算亮度/光比/帧差/背景帧差/上下半区运动，
    CSV 一帧一行；联系表按 --step 秒采样全片，用于逐张目检。
  * 约定：失败即停、不重试；不逆向签名、不去水印、不批量爬取。
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import cv2
import numpy as np
import requests
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
URL_RE = re.compile(
    r"https?://(?:v\.douyin\.com|www\.douyin\.com|www\.iesdouyin\.com|m\.douyin\.com)"
    r"[^\s'\"<>\\]*"
)
CDN_KEYS = ("douyinvod", "aweme.snssdk", "bytecdn", "snssdk", "video.toutiao", "ixigua", "v-")


# ---------------------------------------------------------------- 链接解析
def extract_url(text: str) -> str:
    m = URL_RE.search(text)
    if not m:
        raise SystemExit("未在输入中找到抖音链接（支持整段分享口令 / 短链 / 长链）")
    return m.group(0).rstrip("，。、,;)】]")


def iter_url_lists(node):
    if isinstance(node, dict):
        if isinstance(node.get("url_list"), list):
            yield node["url_list"]
        for v in node.values():
            yield from iter_url_lists(v)
    elif isinstance(node, list):
        for v in node:
            yield from iter_url_lists(v)


def play_urls_from_blobs(blobs):
    """从接口 JSON 递归挑出视频 CDN 播放直链（排除封面图域名）。"""
    out = []
    for blob in blobs:
        for lst in iter_url_lists(blob):
            for u in lst:
                if not isinstance(u, str) or not u.startswith("http"):
                    continue
                host = urlparse(u).netloc
                if not any(k in host for k in CDN_KEYS):
                    continue
                if any(k in host for k in ("douyinpic", "image", "p-statp")):
                    continue
                if u not in out:
                    out.append(u)
    return out


def pick_best_src(urls):
    """抖音多档播放地址：qs=0 为最高档，qs=1 次之，qs=11 为低清；优先 CDN 直链。"""
    cdn = [u for u in urls if isinstance(u, str) and u.startswith("http")
           and any(k in urlparse(u).netloc for k in ("douyinvod", "aweme.snssdk", "bytecdn"))]
    if not cdn:
        return urls[0] if urls else ""
    def rank(u):
        try:
            return int(parse_qs(urlparse(u).query).get("qs", ["99"])[0])
        except (ValueError, TypeError):
            return 99
    return min(cdn, key=rank)


def resolve_and_download(page_url: str, out: Path, direct_src: str = "") -> tuple:
    """单次打开页面，取播放直链并下载；失败即停、不重试。"""
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    mp4 = None
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=True,
            args=["--disable-blink-features=AutomationControlled", "--mute-audio"],
        )
        kw = {"user_agent": UA, "viewport": {"width": 1280, "height": 720}, "locale": "zh-CN"}
        if STATE.exists():
            kw["storage_state"] = str(STATE)
        ctx = browser.new_context(**kw)
        page = ctx.new_page()
        blobs = []

        def on_response(r):
            if "/aweme/v1/web/" in r.url or "/aweme/v1/aweme/" in r.url:
                try:
                    blobs.append(r.json())
                except Exception:
                    pass

        media_urls = []

        def on_request(r):
            # blob: MSE 流下，真实视频请求仍会出现在 network 里
            try:
                if r.resource_type in ("media", "fetch", "other"):
                    u = r.url
                    if (u.startswith("http")
                            and any(k in urlparse(u).netloc for k in CDN_KEYS)
                            and ".m4s" not in u and "/fmp4/" not in u):
                        media_urls.append(u)
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            browser.close()
            raise SystemExit(f"[STOP] 打开页面失败，停止不重试: {type(e).__name__}: {str(e)[:160]}")

        page.wait_for_timeout(4200)
        # 页面内嵌 RENDER_DATA 兜底
        try:
            raw = page.eval_on_selector("#RENDER_DATA", "e => e.textContent") if page.query_selector("#RENDER_DATA") else ""
            if raw:
                blobs.append(json.loads(unquote(raw)))
        except Exception:
            pass

        vids = page.eval_on_selector_all(
            "video",
            "els => els.map(e => ({src:e.currentSrc, d:e.duration, w:e.videoWidth, h:e.videoHeight}))",
        )
        good_vid = max([v for v in vids if (v.get("d") or 0) > 3],
                       key=lambda v: v["d"], default={})
        api_urls = play_urls_from_blobs(blobs)
        dom_src = good_vid.get("src") or ""
        if dom_src.startswith("blob:"):
            dom_src = ""

        # 优先级：显式直链 > API play_addr > network 真实媒体请求 > <video> CDN 直链
        media = [u for u in dict.fromkeys(media_urls) if u not in api_urls]
        src = (direct_src or (api_urls[0] if api_urls else "")
               or (media[0] if media else "") or dom_src)
        if not src:
            browser.close()
            raise SystemExit("[STOP] 未拿到播放直链（API 与 video.src 均无），按约定停止，不重试")

        aweme = re.search(r"/(?:video|note|slide)/(\d{15,25})", page.url)
        aid = aweme.group(1) if aweme else re.sub(r"\D", "", urlparse(page_url).path)[:20] or "video"
        meta = {
            "aweme_id": aid,
            "page_url": page.url,
            "title": page.title(),
            "duration": good_vid.get("d"),
            "width": good_vid.get("w"),
            "height": good_vid.get("h"),
            "video_src_host": urlparse(src).netloc,
            "src_kind": "direct" if direct_src else ("api" if api_urls and src == api_urls[0] else "dom"),
            "api_play_urls": len(api_urls),
            "input_url": page_url,
        }
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        try:
            ctx.storage_state(path=str(STATE))
        except Exception:
            pass
        browser.close()

    mp4 = out / f"dy_{aid}.mp4"
    if not mp4.exists() or mp4.stat().st_size < 100_000:
        try:
            with requests.get(
                src,
                headers={"Referer": "https://www.douyin.com/", "User-Agent": UA},
                cookies=cookies, stream=True, timeout=(15, 180),
            ) as r:
                r.raise_for_status()
                with mp4.open("wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        if chunk:
                            f.write(chunk)
        except Exception as e:
            if mp4.exists():
                mp4.unlink()
            raise SystemExit(f"[STOP] 下载失败，停止不重试: {type(e).__name__}: {str(e)[:160]}")
        print(f"    下载完成 {mp4.stat().st_size / 1024 / 1024:.1f} MB | src_kind={meta['src_kind']}")
    else:
        print(f"    复用已下载 {mp4.name}")

    cap = cv2.VideoCapture(str(mp4))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    meta.update({"fps": round(fps, 3), "frames": int(n),
                 "real_duration_s": round(n / fps, 2),
                 "real_width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                 "real_height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))})
    cap.release()
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return mp4, meta


# ---------------------------------------------------------------- 逐镜拉片
def fmt_tc(sec):
    m, s = divmod(max(0.0, sec), 60)
    return f"{int(m):02d}:{s:05.2f}"


def analyze(mp4: Path, out: Path, threshold: float, min_len: int):
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import ContentDetector

    try:
        video = open_video(str(mp4), backend="opencv")
        sm = SceneManager()
        sm.add_detector(ContentDetector(threshold=threshold, min_scene_len=min_len))
        sm.detect_scenes(video, show_progress=False)
        scenes = sm.get_scene_list()
    except Exception as e:
        print(f"    [WARN] 镜头检测异常 {type(e).__name__}，按 0 镜头兜底")
        scenes = []

    cap = cv2.VideoCapture(str(mp4))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    dur = total / fps
    # 0 镜头兜底：整片视为 1 个镜头，绝不让分镜表报错
    if not scenes:
        from scenedetect import FrameTimecode
        scenes = [(FrameTimecode(0, fps), FrameTimecode(max(0, total - 1), fps))]
        print("    0 镜头 -> 兜底为整片 1 镜")

    frames_dir = out / "frames"
    frames_dir.mkdir(exist_ok=True)
    rows, thumbs = [], []
    for i, (a, b) in enumerate(scenes, 1):
        s, e = a.seconds, min(b.seconds, dur)
        picks = {"head": s + 0.08, "mid": (s + e) / 2, "tail": max(s + 0.08, e - 0.15)}
        files = {}
        for key, t in picks.items():
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, fr = cap.read()
            if not ok:
                continue
            im = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
            im.thumbnail((480, 270))
            fn = f"shot_{i:03d}_{key}.jpg"
            im.save(frames_dir / fn, quality=86)
            files[key] = fn
        rows.append({
            "镜号": i, "入点": a.get_timecode(), "出点": b.get_timecode(),
            "时长(s)": round(e - s, 2), "起始帧": a.frame_num, "结束帧": b.frame_num,
            "fps": round(fps, 2),
            "首帧": f"frames/{files.get('head', '')}",
            "中间帧": f"frames/{files.get('mid', '')}",
            "尾帧": f"frames/{files.get('tail', '')}",
            "画面内容/景别/运镜": "",
        })
        if "head" in files:
            im = Image.open(frames_dir / files["head"]).convert("RGB")
            im.thumbnail((320, 180))
            thumbs.append((im, f"S{i:02d}  {fmt_tc(s)} - {fmt_tc(e)}  ({e - s:.1f}s)"))
    cap.release()

    if rows:
        with (out / "shots.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    cols, tw, th, lh = 4, 320, 180, 24
    lines = max(1, (len(thumbs) + cols - 1) // cols)
    sheet = Image.new("RGB", (cols * tw, lines * (th + lh)), (18, 18, 22))
    d = ImageDraw.Draw(sheet)
    for idx, (im, label) in enumerate(thumbs):
        x, y = (idx % cols) * tw, (idx // cols) * (th + lh)
        sheet.paste(im, (x + (tw - im.width) // 2, y))
        d.text((x + 8, y + th + 5), label, fill=(225, 225, 230))
    sheet.save(out / "拉片网格图.jpg", quality=88)
    print(f"    分镜 {len(rows)} 镜 | 首/中/尾帧已导出")
    return len(rows)


# ---------------------------------------------------------------- 逐帧分析
def frame_audit(mp4: Path, out: Path, step_sec: float):
    """全帧定量指标（CSV 一帧一行）＋全片采样帧联系表。"""
    cap = cv2.VideoCapture(str(mp4))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    dur = total / fps if fps else 0
    step = max(1, int(round(fps * step_sec)))

    fdir = out / "逐帧"
    fdir.mkdir(exist_ok=True)
    rows, samples = [], []
    prev_small = None
    idx = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        small = cv2.resize(fr, (160, 90))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev_small is None:
            diff = np.zeros_like(gray)
        else:
            diff = cv2.absdiff(gray, prev_small)
        bh, bw = gray.shape
        edge = np.concatenate([diff[:12].ravel(), diff[-12:].ravel(),
                               diff[:, :12].ravel(), diff[:, -12:].ravel()])
        center = diff[bh // 4: bh * 3 // 4, bw // 4: bw * 3 // 4]
        bottom = diff[bh * 2 // 3:, :]
        top = diff[: bh // 3, :]
        rec = {
            "帧号": idx,
            "时间码": fmt_tc(idx / fps),
            "秒": round(idx / fps, 3),
            "平均亮度": round(float(gray.mean()), 2),
            "亮度标准差": round(float(gray.std()), 2),
            "暗部占比%": round(float((gray < 40).mean() * 100), 2),
            "高光占比%": round(float((gray > 200).mean() * 100), 2),
            "全帧运动": round(float(diff.mean()), 3),
            "背景运动": round(float(edge.mean()), 3),
            "中心运动": round(float(center.mean()), 3),
            "下半区运动": round(float(bottom.mean()), 3),
            "上半区运动": round(float(top.mean()), 3),
        }
        rows.append(rec)
        if idx % step == 0:
            im = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
            im.thumbnail((320, 180))
            fn = f"f_{idx:06d}.jpg"
            im.save(fdir / fn, quality=84)
            samples.append((fn, rec["时间码"], rec["全帧运动"], rec["平均亮度"]))
        prev_small = gray
        idx += 1
    cap.release()

    if not rows:
        print("    [STOP] 无可解码帧，逐帧分析终止，不重试")
        return {}

    with (out / "逐帧指标.csv").open("w", encoding="utf-8-sig", newline="") as f:
        cw = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        cw.writeheader()
        cw.writerows(rows)

    # 峰值帧（跳跃/爆发动作候选）与静止帧（定格候选）
    mv = np.array([r["全帧运动"] for r in rows])
    peaks = np.argsort(mv)[-12:][::-1].tolist()
    stills = np.argsort(mv)[:8].tolist()
    bg = float(np.mean([r["背景运动"] for r in rows]))
    summary = {
        "width": w, "height": h, "fps": round(fps, 3), "frames": idx,
        "duration_s": round(dur, 2), "step_sec": step_sec, "sample_frames": len(samples),
        "avg_luma": round(float(np.mean([r["平均亮度"] for r in rows])), 2),
        "avg_luma_std": round(float(np.mean([r["亮度标准差"] for r in rows])), 2),
        "avg_dark_pct": round(float(np.mean([r["暗部占比%"] for r in rows])), 2),
        "avg_highlight_pct": round(float(np.mean([r["高光占比%"] for r in rows])), 2),
        "avg_bg_motion": round(bg, 3),
        "camera_stable_estimate": "固定/近似固定" if bg < 3.0 else ("轻微运动" if bg < 8 else "明显运动/手持"),
        "motion_peak_frames": [{"帧号": int(k), "时间码": rows[k]["时间码"],
                                 "全帧运动": rows[k]["全帧运动"]} for k in peaks[:8]],
        "still_frames": [{"帧号": int(k), "时间码": rows[k]["时间码"]} for k in stills[:5]],
    }
    (out / "逐帧汇总.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # 采样帧联系表：每页 5 列 × 4 行，带时间码与运动值
    cols, per_page, tw, th, lh = 5, 20, 320, 180, 26
    pages = (len(samples) + per_page - 1) // per_page
    for pg in range(pages):
        chunk = samples[pg * per_page:(pg + 1) * per_page]
        sheet = Image.new("RGB", (cols * tw, ((len(chunk) + cols - 1) // cols) * (th + lh)), (20, 20, 24))
        d = ImageDraw.Draw(sheet)
        for i, (fn, tc, motion, luma) in enumerate(chunk):
            im = Image.open(fdir / fn).convert("RGB")
            x, y = (i % cols) * tw, (i // cols) * (th + lh)
            sheet.paste(im, (x + (tw - im.width) // 2, y))
            d.text((x + 6, y + th + 5), f"{tc}  M={motion:.1f}  L={luma:.0f}", fill=(225, 225, 232))
        sheet.save(out / f"逐帧联系表_{pg + 1:02d}.jpg", quality=87)

    # 峰值帧单独拼一张，便于确认跳跃/落地
    if peaks:
        pk = Image.new("RGB", (4 * 320, 2 * (180 + 24)), (20, 20, 24))
        dp = ImageDraw.Draw(pk)
        for i, k in enumerate(peaks[:8]):
            fn = f"f_{(k // step) * step:06d}.jpg"
            p = fdir / fn
            if not p.exists():
                continue
            im = Image.open(p).convert("RGB")
            im.thumbnail((320, 180))
            x, y = (i % 4) * 320, (i // 4) * 204
            pk.paste(im, (x + (320 - im.width) // 2, y))
            dp.text((x + 6, y + 184), f"peak {rows[k]['时间码']} M={rows[k]['全帧运动']:.1f}", fill=(235, 200, 140))
        pk.save(out / "运动峰值帧.jpg", quality=88)

    print(f"    逐帧 {idx} 帧 -> 指标CSV / {pages} 张联系表 / 峰值帧 "
          f"| 背景运动={bg:.2f} ({summary['camera_stable_estimate']})")
    return summary


# ---------------------------------------------------------------- 主流程
def process_one(page_url, out_root, args, direct_src="", label=""):
    out = out_root / (label or re.sub(r"\D", "", page_url)[:20] or "video")
    out.mkdir(parents=True, exist_ok=True)
    done = out / "逐帧汇总.json"
    if done.exists() and not args.force:
        print(f"  [SKIP] {out.name} 已有逐帧结果")
        return {"id": out.name, "status": "skipped"}
    print(f"  --- {out.name} {label}")
    try:
        mp4, meta = resolve_and_download(page_url, out, direct_src)
        analyze(mp4, out, args.threshold, args.min_len)
        summary = frame_audit(mp4, out, args.step)
        return {"id": out.name, "status": "ok", "meta": meta, "audit": summary, "dir": str(out)}
    except SystemExit as e:
        print(f"  [STOP] {out.name}: {str(e)[:150]}")
        return {"id": out.name, "status": "stopped", "reason": str(e)[:200]}


def load_batch(path):
    """按 aweme_id 合并，永远保留 play_urls 最完整的那个对象（DOM 链接是残缺版本）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = {}
    def walk(node):
        if isinstance(node, dict):
            aid = node.get("aweme_id")
            if aid and str(aid).isdigit() and node.get("url"):
                key = str(aid)
                score = len(node.get("play_urls") or []) * 10 + len(node.get("desc") or "")
                if key not in items or score > items[key][0]:
                    items[key] = (score, node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(data)
    return [v[1] for v in items.values()]


def main():
    ap = argparse.ArgumentParser(description="抖音拉片 + 逐帧分析")
    ap.add_argument("share", nargs="?", help="整段分享口令、短链或长链")
    ap.add_argument("--out", default="./lapian_out")
    ap.add_argument("--threshold", type=float, default=27.0)
    ap.add_argument("--min-len", type=int, default=15)
    ap.add_argument("--step", type=float, default=0.5, help="联系表采样间隔（秒），指标仍逐全帧计算")
    ap.add_argument("--src", default="", help="显式播放直链（跳过页面解析）")
    ap.add_argument("--batch", default="", help="候选池 JSON，批量逐帧分析")
    ap.add_argument("--ids", default="", help="仅处理这些 aweme_id，逗号分隔")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out_root = Path(a.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    if a.batch:
        items = load_batch(a.batch)
        if a.ids:
            want = set(x.strip() for x in a.ids.split(",") if x.strip())
            items = [x for x in items if str(x.get("aweme_id")) in want]
        if a.limit:
            items = items[:a.limit]
        print(f"[批量逐帧] {len(items)} 条 | step={a.step}s | 失败即停、不重试")
        results = []
        for i, it in enumerate(items, 1):
            aid = str(it["aweme_id"])
            url = it.get("url") or f"https://www.douyin.com/video/{aid}"
            src = pick_best_src(it.get("play_urls") or [])
            print(f"[{i}/{len(items)}] {aid} | play_urls={len(it.get('play_urls') or [])} | {it.get('desc','')[:50]}")
            results.append(process_one(url, out_root, a, src, aid))
        (out_root / "逐帧批量汇总.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        ok = sum(1 for r in results if r["status"] == "ok")
        print(f"\n完成 {ok}/{len(results)} 条 -> {out_root}")
        return

    if not a.share and not a.src:
        raise SystemExit("需要 share 链接 / --src / --batch 之一")
    url = extract_url(a.share) if a.share else ""
    r = process_one(url, out_root, a, a.src)
    print(f"\n完成 -> {out_root} | {r['status']}")


if __name__ == "__main__":
    main()
