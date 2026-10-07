# wechat-draft-publisher-cloud（skill）

把 **markdown 文章 + 封面图** 推到 **公众号草稿箱** 的客户端 skill。需配合部署在微信云托管的 relay 服务端（[shinchen6/wechat-draft-relay](https://github.com/shinchen6/wechat-draft-relay)），利用官方「开放接口服务」免鉴权调用公众号接口。零三方依赖（仅 Python 标准库）。

## 配置

relay 地址与密钥按优先级解析：

1. 命令行 `--cloud-url` / `--cloud-key`
2. 环境变量 `WECHAT_DRAFT_RELAY_URL` / `WECHAT_DRAFT_RELAY_KEY`（兼容 `DRAFT_CLOUD_URL` / `DRAFT_API_KEY`）
3. `scripts/config.json`（已被 .gitignore 忽略）：
   ```json
   { "relay_url": "https://<服务名>.<地域>.run.tcloudbase.com", "relay_key": "你的 RELAY_API_KEY" }
   ```

## 使用

```bash
# dry-run（默认，只本地转 HTML + 校验，不真发）
python scripts/publish_script.py --article X.md --cover Y.png --titles-md T.md

# 真发
python scripts/publish_script.py --real --article X.md --cover Y.png --titles-md T.md
```

正文插图：在 `article.md` 写 `![](img/body1.png)`，脚本自动经 relay 上传替换。

## 贴图（图片消息 newspic）

首张图即封面，不需要 `--cover`；正文自动转纯文本（贴图不支持 HTML 与正文插图）。

```bash
# --images 可给目录（按文件名排序）、逗号分隔的多个路径，或多个参数；最多 20 张
python scripts/publish_script.py --real --article X.md --titles-md T.md \
  --type newspic --images img/
```

不传 `--type` 时默认 `news`（图文），行为与旧版一致。

## 删除草稿

```bash
python scripts/publish_script.py --delete <media_id> --real          # 单个
python scripts/publish_script.py --delete-batch ids.txt --real       # 批量（每行一个 media_id，# 开头注释）
```

## 公众号诊断（草稿箱）

```bash
python scripts/publish_script.py --diagnose drafts           # 草稿列表
python scripts/publish_script.py --diagnose draft-count      # 草稿总数
python scripts/publish_script.py --diagnose draft --diag-id <media_id>   # 回读单篇
# 加 --report-stdout 可同时打印微信原始 JSON
```

## 流程（--real 时）

图文（news）：
1. `md2wechat` 把 Markdown 转成微信图文 HTML（图片引用先留占位）
2. 每张正文插图经 relay `/material` 上传 → 换回 mmbiz 链接，回填 HTML
3. 封面经 relay `/material` → 拿到 `thumb_media_id`
4. 标题 + HTML + `thumb_media_id` 经 relay `/draft` 创建草稿

贴图（newspic）：
1. `--images` 的每张图经 relay `/material` 上传 → 按顺序拿到永久 `media_id`
2. 正文 Markdown 转纯文本（去图片/代码块/强调符号）
3. `article_type=newspic` + `image_media_ids` 经 relay `/draft` 创建草稿
