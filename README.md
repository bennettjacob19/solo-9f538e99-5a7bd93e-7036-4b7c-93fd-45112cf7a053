# OCI Image Unpacker

离线排查容器文件缺失时，从交付镜像还原最终根文件系统的服务。
上传仅含单个 manifest 的 OCI Image Layout tar，当次请求返回还原后的 `rootfs.tar`。

同时提供**离线换底**（`POST /rebase`）：基础镜像出补丁、无法重跑应用构建时，
上传应用镜像、旧基底、新基底三份同平台单 manifest OCI Image Layout tar，
服务校验后用新基底接上应用自己的层，产出一份可直接由 `/unpack` 展开的
自包含 OCI 归档（下载前已试展开）。

## 快速开始（docker compose）

```bash
docker compose up --build
```

服务监听 <http://localhost:8000>，打开 <http://localhost:8000/docs>：

1. 先调 `GET /sample-image` 下载样例镜像 `sample-image.tar`（Swagger 里点 "Download file"）；
2. 再在 `POST /unpack` 里上传该 tar，响应即为 `rootfs.tar`；
3. 在 `POST /rebase` 分别上传 `app_image`、`old_base`、`new_base` 三个 tar，
   响应即为换底后的 OCI Image Layout tar，可再传回 `POST /unpack` 验证。

命令行等价操作：

```bash
curl -OJ http://localhost:8000/sample-image          # 下载样例镜像
curl -F "file=@sample-image.tar" -OJ \
     http://localhost:8000/unpack                    # 得到 rootfs.tar
tar -tf rootfs.tar                                   # 查看还原结果

curl -F "app_image=@app.tar" \
     -F "old_base=@old-base.tar" \
     -F "new_base=@new-base.tar" -OJ \
     http://localhost:8000/rebase                    # 得到 rebased-image.tar
```

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest                 # 运行测试
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/unpack` | 上传 OCI Image Layout tar（单 manifest），返回 `rootfs.tar`；任何校验/解包失败返回 400，不返回残缺文件系统 |
| `POST` | `/rebase` | 上传 `app_image`/`old_base`/`new_base` 三份单 manifest OCI Image Layout tar，返回换底后的自包含 OCI 归档；校验或叠层失败只返回 400 错误 |
| `GET` | `/sample-image` | 下载样例镜像（含删除、opaque、硬链接覆盖） |
| `GET` | `/healthz` | 健康检查 |

## 换底（rebase）语义

- **三包校验（沿用 blob 与 diff_id 校验）**：每个包都必须是同平台
  （`os`/`architecture`/`variant` 一致）的单 manifest OCI Image Layout；
  所有引用 blob 核对存在性、字节大小与 SHA-256；每个 layer 解压后与 config
  的 `rootfs.diff_ids` 逐一核对。三份 config 都**必须带 history**；
  `empty_layer` 条目只占历史槽位，其余 history 条目与层一一对应。
- **前缀关系**：旧基底的 `diff_ids` 必须是应用镜像 `diff_ids` 的严格前缀，
  旧基底的非空 `history` 条目也必须逐项是应用镜像 history 的前缀；应用镜像
  至少要在基底之上多出一层。前缀比对基于 **diff_id（解压后摘要）**，因此
  同一层允许压缩方式不同（如旧基底是 gzip、新基底是 plain tar），不比较
  压缩 blob 摘要。
- **换底产物**：新基底全部层在前，应用镜像中位于旧基底前缀之后的各层
  原样接在后面。应用层 **blob 原字节保留**（硬链接或分块拷贝，mediaType
  与描述符原样沿用）；应用运行配置（`config` 等）保留，仅重写
  `rootfs.diff_ids` 与 `history`；config、各 layer、manifest、index
  各级描述符全部重新计算。
- **下载前试展开**：产物归档生成后立即在服务端按 `/unpack` 同一套逻辑试
  展开一遍，成功后才允许下载；试展开失败则整次请求报错，不返回归档。
- **叠层失败的错误定位**：`/unpack` 的条目级错误带**从 1 起的清单层号**
  和条目路径；`/rebase` 据此把试展开错误映射回**对应的输入包**
  （`app_image` 或 `new_base`）、该包内的**原层号**与**条目路径**，
  400 响应 `detail` 为结构化对象：

  ```json
  {
    "error": "layer_overlay_failed",
    "message": "app_image layer 3 entry '../escape.txt': ...",
    "source_package": "app_image",
    "original_layer_number": 3,
    "entry_path": "../escape.txt"
  }
  ```

  新基底与旧基层数不同时，应用层号按旧基底长度映射回应用镜像原始清单层号。
- **流式处理**：上传落盘、blob 哈希/gzip 解压/拷贝、输出 tar 均按
  1 MiB 块流式进行（层 tar 仅顺序读取成员头，不整包入内存），
  不会把整包或整层读入内存。

## 校验与还原语义（/unpack）

- **完整性校验**：index.json 引用的 manifest blob、manifest 引用的 config 与
  各 layer blob 都会核对**存在性、字节大小、SHA-256**；每个 layer 解压后
  （gzip 层先解压）再与 config 中 `rootfs.diff_ids` 逐一核对。层数与
  diff_ids 数量不一致、摘要不符、缺块、损坏，整次请求报错。
- **层格式**：支持未压缩层（`...layer.v1.tar`）与 gzip 层
  （`...layer.v1.tar+gzip`，含 docker 对应 mediaType）。
- **按清单层序还原**：普通文件、目录、符号链接、硬链接与权限位（含
  setuid/sgid/sticky）；目录权限与 mtime 在全部层应用完后统一设置，避免
  只读目录阻断同层/上层写入。
- **whiteout / opaque**：每层先扫描全部条目，把 `.wh.<name>` 与
  `.wh..wh..opq` 的删除动作**只作用于下层文件系统**，然后再写入本层新
  内容。因此删除标记在层内的位置（文件前/后）不会影响同层新增文件。
- **硬链接覆盖安全**：新文件先写临时文件再 `rename` 替换，绝不原地截断，
  所以覆盖硬链接的一端时，另一端保留原字节。输出 tar 中仍共享 inode 的
  硬链接会以硬链接条目保留。
- **路径安全**：条目路径拒绝绝对路径与 `..`；符号链接目标（绝对目标按
  rootfs 内解释）与硬链接目标经解析后必须落在隔离目录内，写入前再随
  符号链接逐层解析校验，任何逃逸都会让整次请求失败。
- **请求隔离**：每个请求使用独立临时目录，失败即清理，成功响应发送后
  清理；请求之间互不影响。

## 样例镜像内容

两层（第一层 gzip，第二层未压缩），用于验证上述语义：

- 下层文件 `app/config.txt`、`app/data/remove.txt` 被 whiteout 删除；
- `secret/` 被标记 opaque：下层的 `secret/token.txt` 消失，同层新增的
  `secret/public.txt` 保留；
- `app/data/rebuilt.txt` 在同层先写新内容、后出现其 whiteout —— 新内容
  必须保留；
- `bin/tool` 与 `bin/tool-link` 在下层是同一硬链接，上层只覆盖
  `bin/tool`（`tool-v2`），`bin/tool-link` 必须仍是 `tool-v1`；
- 相对符号链接 `bin/tool-alias -> tool` 与绝对符号链接
  `etc/motd-link -> /etc/motd`；
- 权限位：`bin/tool` 0755、`secret/` 0700。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `UNPACK_WORKDIR` | `$TMPDIR/oci-unpacker` | 请求临时目录与样例镜像存放位置 |
