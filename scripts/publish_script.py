#!/usr/bin/env python3
"""wechat-draft-publisher 客户端：Markdown -> 微信 HTML -> relay -> 草稿箱。

零三方依赖：仅用 Python 标准库（urllib + base64 + json）+ 本地 Markdown 转换。
Markdown -> HTML 在本地完成（md2wechat.py，零依赖），relay 只负责把图片 / 草稿推给微信。

用法:
  python scripts/publish_script.py --article X.md --cover Y.png --titles-md T.md
  python scripts/publish_script.py --article X.md --cover Y.png --titles-md T.md --real

贴图（图片消息 newspic，首张图即封面，不需要 --cover）:
  python scripts/publish_script.py --article X.md --titles-md T.md --type newspic --images img/ --real
  # --images 接逗号分隔的多个路径，或直接给一个目录（按文件名排序，最多 20 张）

删除草稿（默认 dry-run 只列出，--real 才真删）:
  python scripts/publish_script.py --delete <media_id> --real
  python scripts/publish_script.py --delete-batch ids.txt --real

正文插图:
  在 X.md 里直接写 ![](img/body1.png) 这样的本地图片引用即可。
  脚本会在本地把 markdown 转成微信 HTML，并把每张插图 base64 后通过 relay /material
  上传到素材库、换回 mmbiz 链接；封面同样走 /material 拿到 thumb_media_id，
  最后用 /draft 创建草稿。relay 不解析 markdown。
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 支持 `python publish_script.py` 与 `python -m publish_script`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from md2wechat import convert  # 本地 Markdown -> 微信 HTML

# ── relay 默认值（发布版留空，要求通过 env / config.json / CLI 注入）──
DEFAULT_CLOUD_URL = os.environ.get("WECHAT_DRAFT_RELAY_URL", "") or os.environ.get("DRAFT_CLOUD_URL", "")
DEFAULT_CLOUD_KEY = os.environ.get("WECHAT_DRAFT_RELAY_KEY", "") or os.environ.get("DRAFT_API_KEY", "")
TITLE_BYTE_LIMIT = 64

_IMG_RE = re.compile(r'!\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')


def resolve_relay(cloud_url_arg, cloud_key_arg):
    """按优先级解析 relay 地址与密钥。"""
    if cloud_url_arg and cloud_key_arg:
        return cloud_url_arg, cloud_key_arg
    env_url = os.environ.get("WECHAT_DRAFT_RELAY_URL") or os.environ.get("DRAFT_CLOUD_URL")
    env_key = os.environ.get("WECHAT_DRAFT_RELAY_KEY") or os.environ.get("DRAFT_API_KEY")
    if env_url and env_key:
        return env_url, env_key
    cfg = _load_config_json()
    if cfg.get("relay_url") and cfg.get("relay_key"):
        return cfg["relay_url"], cfg["relay_key"]
    if DEFAULT_CLOUD_URL and DEFAULT_CLOUD_KEY:
        return DEFAULT_CLOUD_URL, DEFAULT_CLOUD_KEY
    raise RuntimeError(
        "未配置 relay 访问地址 / 密钥。请任选其一：\n"
        "  a) 环境变量 WECHAT_DRAFT_RELAY_URL + WECHAT_DRAFT_RELAY_KEY\n"
        "  b) 在 scripts/ 目录放 config.json: {\"relay_url\": \"...\", \"relay_key\": \"...\"}\n"
        "  c) 命令行 --cloud-url / --cloud-key\n"
    )


def _load_config_json():
    p = Path(__file__).resolve().parent / "config.json"
    if p.is_file():
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[warn] 读取 config.json 失败，忽略: {e}", file=sys.stderr)
    return {}


def pick_title(titles_md_path=None, title_path=None):
    """从 titles.md 解析第 1 个真候选，或直读 title.txt"""
    if title_path:
        with open(title_path, encoding="utf-8") as f:
            return f.read().strip()
    if not titles_md_path:
        raise RuntimeError("必须给 titles_md_path 或 title_path")
    with open(titles_md_path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or s.startswith(">"):
                continue
            m = re.match(r"^(\d+)[.\)\u3001]\s*(.+)$", s)
            if m:
                return m.group(2).strip()
    raise RuntimeError("titles.md 没解析到候选（检查是否有 `1.` 格式行）")


def collect_body_images(article_path):
    """扫描正文 markdown 的本地图片引用，返回 [(src, 本地绝对路径), ...]（去重）。"""
    base_dir = os.path.dirname(os.path.abspath(article_path))
    out = []
    with open(article_path, encoding="utf-8") as f:
        text = f.read()
    for m in _IMG_RE.finditer(text):
        src = m.group(1).strip()
        if src.startswith(("http://", "https://", "data:", "mmbiz")):
            continue
        p = src if os.path.isabs(src) else os.path.join(base_dir, src)
        if os.path.isfile(p) and (src, p) not in out:
            out.append((src, p))
    return out


def _post_json(url: str, key: str, payload: dict, timeout: int = 120):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-API-Key": key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _call_with_retry(fn, label: str):
    """发请求；对 503（云托管网关冷启动）自动 retry 1 次（8s 退避）。"""
    try:
        return fn(), None, None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if e.code == 503:
            print("[retry] 503 网关冷启动嫌疑，等 8s 后 retry 1 次...", file=sys.stderr)
            time.sleep(8)
            try:
                return fn(), None, None
            except Exception as e2:  # noqa: BLE001
                return None, "NETWORK", f"{label} 503 重试后仍失败: {e2}"
        return None, "NETWORK", f"HTTP {e.code}: {body[:300]}"
    except urllib.error.URLError as e:
        return None, "NETWORK", f"连接失败: {e.reason}"


def _upload_material(cloud_url, cloud_key, path):
    """上传单个本地图片到素材库，返回 relay 的 {'media_id','url'}。"""
    data = open(path, "rb").read()
    res, err, msg = _call_with_retry(
        lambda: _post_json(
            f"{cloud_url.rstrip('/')}/material", cloud_key,
            {"name": os.path.basename(path), "data_b64": base64.b64encode(data).decode("ascii")},
        ),
        f"上传素材 {os.path.basename(path)}",
    )
    if err:
        return None, err, msg
    return res, None, None


def _plain_text(article_path):
    """取正文纯文本（贴图 newspic 只支持纯文本，不放 HTML/图片）。"""
    with open(article_path, encoding="utf-8") as f:
        raw = f.read()
    raw = re.sub(r'!\[[^\]]*\]\([^)]*\)', "", raw)      # 去图片
    raw = re.sub(r'```.*?```', "", raw, flags=re.S)      # 去代码块
    raw = re.sub(r'^#{1,6}\s+', "", raw, flags=re.M)     # 去标题井号
    raw = re.sub(r'[*`>]+', "", raw)                     # 去强调/引用符号
    lines = [ln.strip() for ln in raw.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _expand_image_args(image_args):
    """把 --images 参数展开成图片路径列表。支持逗号分隔、多个参数、目录（按文件名排序）。"""
    specs = []
    for raw in image_args or []:
        specs.extend(s.strip() for s in str(raw).split(",") if s.strip())
    out = []
    for spec in specs:
        if os.path.isdir(spec):
            for n in sorted(os.listdir(spec)):
                p = os.path.join(spec, n)
                if os.path.isfile(p) and n.lower().endswith((".png", ".jpg", ".jpeg")):
                    out.append(p)
        elif os.path.isfile(spec):
            out.append(spec)
        else:
            raise FileNotFoundError(f"图片不存在: {spec}")
    return out


def publish(cloud_url, cloud_key, title, article_path, cover_path, image_pairs,
            article_type="news", image_paths=None):
    """--real 时：本地转 HTML + 上传插图/封面 + 建草稿。返回 (media_id, err, msg, elapsed)。

    article_type:
      news    图文（默认）——行为与旧版完全一致
      newspic 贴图——image_paths 里的图片依次上传，首张即封面，正文只送纯文本
    """
    start = time.time()

    # ── 贴图模式 ──
    if article_type == "newspic":
        if not image_paths:
            return None, "LOCAL_ERROR", "article_type=newspic 时 --images 不能为空", time.time() - start
        if len(image_paths) > 20:
            return None, "LOCAL_ERROR", f"贴图最多 20 张，当前 {len(image_paths)} 张", time.time() - start
        media_ids = []
        for p in image_paths:
            res, err, msg = _upload_material(cloud_url, cloud_key, p)
            if err:
                return None, err, msg, time.time() - start
            media_ids.append(res["media_id"])
        dres, derr, dmsg = _call_with_retry(
            lambda: _post_json(
                f"{cloud_url.rstrip('/')}/draft", cloud_key,
                {
                    "title": title,
                    "content_html": _plain_text(article_path),
                    "article_type": "newspic",
                    "image_media_ids": media_ids,
                    "author": "",
                    "digest": "",
                },
            ),
            "建贴图草稿",
        )
        if derr:
            return None, derr, dmsg, time.time() - start
        return dres["media_id"], None, None, time.time() - start

    # 1) 上传正文插图 -> url_map
    url_map = {}
    for src, p in image_pairs:
        res, err, msg = _upload_material(cloud_url, cloud_key, p)
        if err:
            return None, err, msg, time.time() - start
        url_map[src] = res["url"]
        url_map[os.path.basename(src)] = res["url"]

    # 2) 上传封面 -> thumb_media_id
    cres, cerr, cmsg = _upload_material(cloud_url, cloud_key, cover_path)
    if cerr:
        return None, cerr, cmsg, time.time() - start
    thumb_media_id = cres["media_id"]

    # 3) 本地转 HTML（用上面换回的 mmbiz 链接替换图片）
    html = convert(md_of(article_path), url_map)

    # 4) 建草稿
    dres, derr, dmsg = _call_with_retry(
        lambda: _post_json(
            f"{cloud_url.rstrip('/')}/draft", cloud_key,
            {
                "title": title,
                "content_html": html,
                "thumb_media_id": thumb_media_id,
                "author": "",
                "digest": "",
            },
        ),
        "建草稿",
    )
    if derr:
        return None, derr, dmsg, time.time() - start
    return dres["media_id"], None, None, time.time() - start


def md_of(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def append_covered(covered_md_path, covered_line):
    """幂等追加 covered_line，重复跳过"""
    key = covered_line.split("#")[0].strip() if "#" in covered_line else covered_line.strip()
    try:
        with open(covered_md_path, encoding="utf-8") as f:
            existing = f.read()
    except FileNotFoundError:
        Path(covered_md_path).parent.mkdir(parents=True, exist_ok=True)
        with open(covered_md_path, "w", encoding="utf-8") as f:
            f.write("# 已写过的 owner/repo，每行一条，用作自动去重\n")
            f.write("# owner/repo # YYYY-MM-DD [可选备注]\n")
            f.write(f"{covered_line}\n")
        return True
    if key in existing:
        return False
    with open(covered_md_path, "a", encoding="utf-8") as f:
        f.write(f"{covered_line}\n")
    return True


def build_result():
    return {
        "success": False, "media_id": None, "chosen_title": None,
        "chosen_title_bytes": None, "cloud_url": None,
        "body_images": 0, "html_len": 0,
        "article_type": "news", "image_files": 0,
        "covered_appended": False, "feishu_sent": False,
        "error_code": None, "error_msg": None, "elapsed_sec": None,
    }


def _run_delete(args):
    """--delete / --delete-batch：删草稿。默认 dry-run 只列出；--real 才真删。"""
    ids = []
    if args.delete:
        ids.append(args.delete.strip())
    if args.delete_batch:
        p = Path(args.delete_batch)
        if not p.is_file():
            print(f"[delete] 批处理文件不存在: {args.delete_batch}", file=sys.stderr)
            sys.exit(2)
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            ids.append(s)
    ids = [i for i in ids if i]
    if not ids:
        print("[delete] 没有可删除的 media_id", file=sys.stderr)
        sys.exit(2)

    result = {"action": "delete", "total": len(ids), "deleted": [], "failed": []}

    if not args.real:
        print(f"[dry-run] 将删除 {len(ids)} 篇草稿（加 --real 才真删）:")
        for i in ids:
            print(f"  - {i}")
        result_path = os.path.join(args.run_dir, "delete_result.json")
        os.makedirs(args.run_dir, exist_ok=True)
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        if args.report_stdout:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)

    try:
        cloud_url, cloud_key = resolve_relay(args.cloud_url, args.cloud_key)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    for i in ids:
        res, err, msg = _call_with_retry(
            lambda: _post_json(
                f"{cloud_url.rstrip('/')}/draft-delete", cloud_key,
                {"media_id": i},
            ),
            "删除草稿",
        )
        if err:
            result["failed"].append({"media_id": i, "error": msg})
            print(f"[delete] 失败 {i}: {msg}", file=sys.stderr)
            continue
        ok = (res or {}).get("errcode") == 0
        if ok:
            result["deleted"].append(i)
            print(f"[delete] 已删 {i}")
        else:
            result["failed"].append({"media_id": i, "error": str(res)})
            print(f"[delete] 微信返回非成功 {i}: {res}", file=sys.stderr)

    result_path = os.path.join(args.run_dir, "delete_result.json")
    os.makedirs(args.run_dir, exist_ok=True)
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    if args.report_stdout:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if not result["failed"] else 1)


def _ts(sec):
    try:
        import datetime
        return datetime.datetime.fromtimestamp(int(sec)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(sec)


_DIAG_ROUTES = {
    "drafts": ("/draft-list", lambda a: {"offset": 0, "count": a.diag_count, "no_content": 1}),
    "draft": ("/draft-get", lambda a: {"media_id": a.diag_id or ""}),
    "draft-count": ("/draft-count", lambda a: {}),
}


def _cloudcall_hint(res, msg):
    """云调用不支持的接口，给出 token 模式提示（让失败原因一目了然）。"""
    txt = msg or ""
    code = (res or {}).get("errcode")
    if code == 48001 or "48001" in txt:
        return ("\n  → 该接口云调用（开放接口服务）不支持（48001 未授权）。"
                "需 relay 切 token 模式：填 WX_APPID/WX_APPSECRET + 公众号 IP 白名单。")
    if "404" in txt or code == 404:
        return ("\n  → 该接口云调用代理层不路由（404）。"
                "需 relay 切 token 模式：填 WX_APPID/WX_APPSECRET。")
    return ""


def _run_diagnose(args):
    """--diagnose TYPE：拉取公众号数据做诊断。原样透传微信响应，打印可读摘要。"""
    ep = args.diagnose
    if ep == "draft" and not args.diag_id:
        print(f"[{ep}] 需要 --diag-id", file=sys.stderr)
        sys.exit(2)

    try:
        cloud_url, cloud_key = resolve_relay(args.cloud_url, args.cloud_key)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    path, build = _DIAG_ROUTES[ep]
    payload = build(args)
    res, err, msg = _call_with_retry(
        lambda: _post_json(f"{cloud_url.rstrip('/')}{path}", cloud_key, payload),
        f"诊断 {ep}",
    )
    if err:
        hint = _cloudcall_hint(None, msg)
        print(f"[diagnose] 调用失败: {msg}{hint}", file=sys.stderr)
        sys.exit(1)
    if (res or {}).get("errcode"):
        hint = _cloudcall_hint(res, None)
        print(f"[diagnose] 微信返回错误: {res}{hint}", file=sys.stderr)
        sys.exit(1)

    _print_diagnostic(ep, res)
    if args.report_stdout:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    sys.exit(0)


def _print_diagnostic(ep, res):
    if ep == "draft-count":
        print(f"草稿总数: {res.get('total_count')}")
        return
    if ep == "drafts":
        items = res.get("item", [])
        print(f"草稿列表（共 {res.get('total_count')} 篇，显示 {len(items)} 篇）:")
        for it in items:
            c = it.get("content", {})
            ni = (c.get("news_item") or [{}])
            title = (ni[0].get("title") or "?") if ni else "?"
            print(f"  - {it.get('media_id')} | {title} | 更新 {_ts(it.get('update_time'))}")
        return
    if ep == "draft":
        ni = (res.get("news_item") or [{}])
        a = ni[0] if ni else {}
        print(f"标题:      {a.get('title')}")
        print(f"media_id:  {a.get('media_id')}")
        print(f"摘要:      {a.get('digest') or '（空）'}")
        print(f"正文长度:  {len(a.get('content') or '')} 字符")
        print(f"封面:      {a.get('thumb_media_id') or '（空）'}")
        return


def main():
    parser = argparse.ArgumentParser(description="推公众号草稿 / 删草稿（支持正文插图）")
    parser.add_argument("--article")
    parser.add_argument("--cover")
    titles = parser.add_mutually_exclusive_group()
    titles.add_argument("--titles-md")
    titles.add_argument("--title-file")
    parser.add_argument("--cloud-url", default=None,
                        help="relay 地址（默认从 env/config.json/DEFAULT 解析）")
    parser.add_argument("--cloud-key", default=None,
                        help="relay 密钥（默认从 env/config.json/DEFAULT 解析）")
    parser.add_argument("--run-dir", default=".")
    parser.add_argument("--type", default="news", choices=["news", "newspic"],
                        help="草稿类型：news=图文（默认，行为同旧版）；newspic=贴图")
    parser.add_argument("--images", nargs="+", default=None,
                        help="贴图(newspic)用：本地图片路径，可逗号分隔多个，也可给目录（按文件名排序）")
    parser.add_argument("--covered-md")
    parser.add_argument("--covered-line")
    parser.add_argument("--feishu-chat-id")
    parser.add_argument("--report-stdout", action="store_true")
    parser.add_argument("--real", action="store_true",
                        help="真正执行（默认 dry-run：只列计划、不真删不真推）")
    # 删除模式（与发布互斥）
    parser.add_argument("--delete", default=None,
                        help="删除单个草稿，传 media_id")
    parser.add_argument("--delete-batch", default=None,
                        help="批量删除，传一个文件，每行一个 media_id（# 开头为注释）")
    # 诊断模式（拉取公众号全量数据，做问题诊断；与发布/删除互斥）
    parser.add_argument("--diagnose", default=None,
                        choices=["drafts", "draft", "draft-count"],
                        help="诊断类型（云调用✅ 已实测可用: drafts/draft/draft-count）")
    parser.add_argument("--diag-id", default=None,
                        help="--diagnose draft 用 media_id")
    parser.add_argument("--diag-count", type=int, default=20,
                        help="--diagnose drafts 的返回条数")
    args = parser.parse_args()

    # 删除模式优先
    if args.delete or args.delete_batch:
        _run_delete(args)
        return

    # 诊断模式
    if args.diagnose:
        _run_diagnose(args)
        return

    result = build_result()

    # dry-run 不联网、不需要 relay 配置；只有 --real 才解析地址/密钥
    if args.real:
        try:
            cloud_url, cloud_key = resolve_relay(args.cloud_url, args.cloud_key)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            sys.exit(2)
        args.cloud_url, args.cloud_key = cloud_url, cloud_key
        result["cloud_url"] = args.cloud_url
    else:
        args.cloud_url, args.cloud_key = None, None
        result["cloud_url"] = None

    title_txt = os.path.join(args.run_dir, "title.txt")

    try:
        os.makedirs(args.run_dir, exist_ok=True)
        # 贴图模式首张图即封面，不需要 --cover
        required = [("article", args.article)]
        if args.type != "newspic":
            required.append(("cover", args.cover))
        for name, p in required:
            if not p or not os.path.isfile(p):
                raise FileNotFoundError(f"{name} 不存在: {p}")
        ts = args.titles_md or args.title_file
        if not os.path.isfile(ts):
            raise FileNotFoundError(f"标题文件不存在: {ts}")

        image_paths = _expand_image_args(args.images) if args.images else []
        if args.type == "newspic":
            if not image_paths:
                raise RuntimeError("贴图模式需要 --images 指定至少一张本地图片")
            result["image_files"] = len(image_paths)
            result["article_type"] = "newspic"
            # 贴图不走正文插图扫描（正文只送纯文本，图片全走 image_media_ids）
            image_pairs = []
        else:
            image_pairs = collect_body_images(args.article)
        result["body_images"] = len(image_pairs)

        chosen = pick_title(args.titles_md, args.title_file)
        b = len(chosen.encode("utf-8"))
        if b > TITLE_BYTE_LIMIT:
            raise RuntimeError(
                f"标题字节数 {b} > {TITLE_BYTE_LIMIT}（平台上限），必须人工改短\n"
                f"当前: {chosen!r}"
            )
        result["chosen_title"] = chosen
        result["chosen_title_bytes"] = b

        with open(title_txt, "w", encoding="utf-8") as f:
            f.write(chosen)

        if args.real:
            media_id, err_code, err_msg, elapsed = publish(
                args.cloud_url, args.cloud_key, chosen, args.article, args.cover, image_pairs,
                article_type=args.type, image_paths=image_paths,
            )
        else:
            # dry-run：本地转 HTML 校验（不联网），用占位 url_map 验证转换不崩
            if args.type == "newspic":
                txt = _plain_text(args.article)
                result["html_len"] = len(txt)
                print(f"[dry-run] 贴图模式：{len(image_paths)} 张图，"
                      f"正文纯文本 {len(txt)} 字符，首张={os.path.basename(image_paths[0])}")
                for p in image_paths:
                    print(f"  - {p}")
            else:
                placeholder = {}
                for src, _ in image_pairs:
                    placeholder[src] = src
                    placeholder[os.path.basename(src)] = src
                result["html_len"] = len(convert(md_of(args.article), placeholder))
            media_id, err_code, err_msg, elapsed = "MOCK_MEDIA_ID_dryrun", None, None, 0.05
        result["elapsed_sec"] = elapsed

        if media_id:
            result["success"] = True
            result["media_id"] = media_id
            try:
                os.remove(title_txt)
            except OSError:
                pass
            if args.covered_md and args.covered_line:
                result["covered_appended"] = append_covered(args.covered_md, args.covered_line)
            if args.feishu_chat_id and args.real:
                try:
                    from hermes_tools import send_message
                    send_message(
                        target=f"feishu:{args.feishu_chat_id}",
                        message=f"✓ 公众号草稿已推送\n\n标题: {chosen} ({b}B)\nmedia_id: {media_id}",
                    )
                    result["feishu_sent"] = True
                except Exception as e:
                    print(f"[warn] feishu 汇报失败（不影响 publish）: {e}", file=sys.stderr)
        else:
            result["error_code"] = err_code
            result["error_msg"] = err_msg
    except Exception as e:
        result["error_code"] = "LOCAL_ERROR"
        result["error_msg"] = str(e)

    result_path = os.path.join(args.run_dir, "publish_result.json")
    try:
        os.makedirs(args.run_dir, exist_ok=True)
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[warn] 写 publish_result.json 失败: {e}", file=sys.stderr)

    if args.report_stdout:
        print(json.dumps(result, ensure_ascii=False, indent=2))

    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
