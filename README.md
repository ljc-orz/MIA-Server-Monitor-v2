# GPU Server Monitor

一个面向可信内网的 GPU 服务器监控看板。服务端通过 SSH 定时执行远程 `gpustat`，将终端输出和结构化 GPU 信息写入 SQLite，并提供网页查看、GPU 资源排序和自动选卡脚本。

当前版本见 [CHANGELOG.md](CHANGELOG.md)。版本号采用 `YY.MM.DD` 格式，并只在更新记录中定义。

> 安全边界：本项目默认面向受控内网，接口没有认证。不要将监听端口直接暴露到公网，也不要将真实服务器地址、用户名、私钥、数据库或采集结果提交到仓库或粘贴到公开渠道。

## 功能概览

- 通过 SSH 连接多台 GPU 服务器，采集 `gpustat -P --watch` 的终端视图和 `gpustat --json` 的结构化数据。
- 网页提供控制台、资源占用排序、自动选择 GPU 的使用示例，以及更新记录弹窗。
- 断线时显示红色状态灯，并保留最后一次成功采集的展示时间。
- 每台服务器可配置专属环境变量，例如 `CUDA_DEVICE_ORDER`。
- 采样数据保存在 SQLite，默认仅保留最近 10 分钟。
- 使用文件锁保证多个 Gunicorn worker 或服务进程中只有一个 SSH 采集进程。

## 目录说明

| 路径 | 用途 |
| --- | --- |
| `main.py` | Flask API、SSH 采集、SQLite 迁移与数据保留逻辑。 |
| `index.html` | 无构建步骤的前端页面。 |
| `servers.json` | 本地服务器清单，包含敏感连接信息；已被 Git 忽略。 |
| SSH 私钥文件 | 采集端使用的私钥，路径由 `main.py` 中的 `SSH_KEY_FILE` 配置；已被 Git 忽略。 |
| `server_monitor.sqlite3` | 运行时采样数据库；已被 Git 忽略。 |
| `assets/` | 自动选卡脚本及网页展示的示例代码。 |
| `gpu-monitor.service` | systemd / Gunicorn 部署模板。 |
| `CHANGELOG.md` | 项目版本与更新记录。 |

## 部署前准备

### 运行环境

- Linux 主机，建议使用专用的非 root 服务账号。
- Python 3.10 或更高版本。
- Gunicorn、Flask、Paramiko、ansi2html。
- 每台被监控机器：可通过 SSH 访问、已安装 `gpustat`，且该 SSH 用户执行 `gpustat -P --watch 2` 与 `gpustat --json` 均能成功。

推荐在每台被监控机器安装 [`ljc-orz/gpustat` 的 `cuda_order` 分支](https://github.com/ljc-orz/gpustat/tree/cuda_order)，而不是未定制的版本。该版本在设置 `CUDA_DEVICE_ORDER=FASTEST_FIRST` 或 `CUDA_DEVICE_ORDER=PCI_BUS_ID` 时，会按 CUDA Runtime 的实际枚举顺序重排 GPU 编号；终端输出和 `--json` 中从 `0` 开始的编号可与 CUDA 逻辑设备编号对应。这让页面展示的 GPU 编号能直接用于 `CUDA_VISIBLE_DEVICES`。

示例安装命令：

```bash
python3 -m pip install "git+https://github.com/ljc-orz/gpustat.git@cuda_order"
```

示例：在部署机创建虚拟环境并安装依赖。

```bash
python3 -m venv .venv
.venv/bin/pip install flask gunicorn paramiko ansi2html
```

若远程主机使用 Conda 或自定义 PATH，请在对应服务器的 `env` 字段中补充 PATH 等变量，而不是依赖交互式 shell 配置。

### SSH 私钥与远程账号

1. 为监控服务创建专用 SSH 密钥和专用远程账号；不要复用个人私钥。
2. 将公钥放入每台被监控服务器该账号的 `~/.ssh/authorized_keys`。
3. 将私钥保存到 `SSH_KEY_FILE` 指定的位置，并限制权限为 `600`。如需使用其他路径，请在部署前修改 `main.py` 中的 `SSH_KEY_FILE`。
4. systemd 的 `User` 必须能读取该私钥、项目目录和数据库，并能在本机创建 `collector.lock`。

示例命令（仅示意，请自行替换文件名、账号和主机）：

```bash
ssh-keygen -t ed25519 -f ./monitor_key -C gpu-monitor
chmod 600 ./monitor_key
ssh-copy-id -i ./monitor_key.pub monitor@example-host
```

服务以非交互方式运行；默认实现没有提供私钥口令输入流程。因此应使用专用、权限严格限制的服务密钥，并通过文件权限和主机防火墙保护它。

## 配置服务器清单

在项目根目录创建 `servers.json`。该文件被 `.gitignore` 排除，**只能保存在部署环境**。

```json
[
  {
    "name": "gpu-a",
    "ip": "192.0.2.10",
    "username": "monitor",
    "env": {
      "CUDA_DEVICE_ORDER": "FASTEST_FIRST",
      "PATH": "/usr/local/bin:/usr/bin:/bin"
    }
  },
  {
    "name": "gpu-b",
    "ip": "192.0.2.11",
    "username": "monitor",
    "env": {}
  }
]
```

字段说明：

- `name`：页面和自动选卡接口中使用的稳定名称。服务器按 `servers.json` 中的顺序展示。
- `ip`：可由部署机访问的 SSH 地址。
- `username`：远程 SSH 用户。
- `env`：可选对象。每个键值对会安全地前缀到该服务器的两条 `gpustat` 命令。变量名必须符合 shell 环境变量命名规则，值会转为字符串并进行 shell 转义。

`192.0.2.0/24` 是文档保留地址，仅用于示例；请勿将真实网络信息写入 README 或提交到版本库。

保存 `servers.json` 后无需重启服务：采集端默认会在 2 秒内启动新增服务器的采集、停止已删除服务器的采集，或在 IP、账号、环境变量变化时替换对应采集线程。网页也会在下一次刷新时按文件中的当前顺序展示服务器。配置无效时，服务会保留上一份可用的采集配置，并在 systemd 日志中记录错误。

## 使用 systemd 部署

本项目假设通过 [gpu-monitor.service](gpu-monitor.service) 启动。先复制该模板并填写占位符：

```ini
[Service]
User=<服务账号>
Group=<服务账号组>
WorkingDirectory=/opt/gpu-server-monitor
ExecStart=/opt/gpu-server-monitor/.venv/bin/gunicorn -w 1 -b 0.0.0.0:2223 main:app
```

建议保持 `-w 1`。应用自身有采集锁，多个 worker 不会重复采集，但单 worker 更易于排查进程状态和资源占用。

可按需在 `[Service]` 中加入环境变量：

```ini
Environment="POLL_INTERVAL_SECONDS=2"
Environment="SSH_TIMEOUT_SECONDS=10"
Environment="RETENTION_MINUTES=10"
Environment="PRUNE_INTERVAL_SECONDS=60"
Environment="SERVER_CONFIG_REFRESH_SECONDS=2"
```

安装并启动：

```bash
sudo install -m 0644 gpu-monitor.service /etc/systemd/system/gpu-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now gpu-monitor.service
sudo systemctl status gpu-monitor.service
```

常用运维命令：

```bash
sudo systemctl restart gpu-monitor.service
sudo journalctl -u gpu-monitor.service -f
sudo journalctl -u gpu-monitor.service -n 200 --no-pager
```

部署后应在可信网络中访问 `http://<部署机地址>:2223/`。请通过防火墙、反向代理访问控制或 VPN 限制来源；不要把端口公开暴露。

## 接口

| 接口 | 用途 |
| --- | --- |
| `GET /` | 监控网页。 |
| `GET /health` | 服务和采集角色状态。 |
| `GET /info` | 最新终端输出及连接状态。 |
| `GET /jinfo` | 最新 `gpustat --json` 数据。 |
| `GET /changelog` | 原始更新记录文件。 |
| `GET /set_gpu` | 自动选卡 Python 脚本。支持 `t1`、`t2`、`ex` 参数。 |
| `GET /auto_set_gpu_example` | 自动选卡完整示例。 |
| `GET /auto_set_gpu_oneline` | 自动选卡一行示例。 |

`/set_gpu` 返回供用户执行的 Python 代码，因此只应对可信用户开放。自动选卡逻辑会读取 `/jinfo` 的 GPU 状态，为本机选择负载较低的卡；若本机没有符合阈值的卡，会输出其他服务器的推荐信息。

## 数据与运行行为

- 两类采样会同时写入：终端文本表 `server_readings` 和 JSON 表 `server_json_readings`。
- `fetched_at` 记录本次状态检查时间；`last_success_at` 记录最后一次成功采集时间。断线时页面展示后者，状态灯为红色。
- `server_connection_state` 独立保存连接会话时间，不受采样数据清理影响：在线时页面显示本次首次在线时间，断线时显示最后一次在线时间。所有页面时间会自动转换为浏览器本地时区。
- 程序启动时会自动创建或迁移 SQLite 表，无需手动建表。
- 默认每 60 秒清理一次超过 10 分钟的采样数据。调整保留时间前应估算磁盘占用。
- `collector.lock` 只协调本机同一项目目录下的进程；若部署多台监控机，它们会分别采集。

## 开发与交接给 Agent

### 架构要点

1. `main.py` 的 `ensure_runtime_initialized()` 在首个请求或直接启动时初始化数据库并决定当前进程是否是采集者。
2. 每台服务器由 `poll_server_forever()` 维护一个 SSH 连接。它一边读取 watch 输出，一边周期性读取 JSON 输出。
3. `normalize_stream_text()` 负责清理终端控制序列。修改该部分时必须保留 SGR 颜色序列，并防止 DCS 等控制序列残留到页面。
4. 前端没有打包工具。修改 `index.html` 后应直接检查 HTML、CSS 和浏览器控制台错误。
5. `servers.json` 是服务器清单的唯一来源。采集端每隔 `SERVER_CONFIG_REFRESH_SECONDS`（默认 2 秒）重新读取它；网页每次刷新也按其当前顺序展示服务器，因此新增、删除或改名服务器不需要修改前端代码。

### 修改约定

- 严禁提交 `servers.json`、私钥、SQLite 数据库、日志或任何真实网络/账号信息。
- 调整 SQLite 字段时，在 `init_db()` 同时提供对既有数据库的迁移逻辑。
- 改动接口返回字段时，同时检查控制台和资源占用两种视图。
- 发布新版本时，在 `CHANGELOG.md` 顶部写入新的 `YY.MM.DD` 版本段，并更新“当前版本”。同一天有多次迭代时，仍在同一个日期版本段内补充条目。
- 代码改动的最低验证：`python -m py_compile main.py`。有可用环境时还应启动服务，检查 `/health`、`/info`、`/jinfo` 和页面刷新。

### 已知安全注意事项

- 当前 SSH 客户端会自动接受未知主机密钥，方便内网首次连接，但缺少严格的主机指纹校验。高安全场景应改为预置并校验 `known_hosts`。
- HTTP 接口未实现认证、授权或 TLS。生产部署前应在网络层或反向代理层补齐访问控制。
- 不要在 `/set_gpu` 脚本或前端示例中写死真实服务地址；部署时应由受控环境或代码配置提供。

## 故障排查

| 现象 | 检查方向 |
| --- | --- |
| 所有服务器显示断线 | `systemctl status`、`journalctl`、私钥权限、部署机到目标机的网络和 SSH 连通性。 |
| 单台服务器断线 | 该条 `servers.json` 配置、远程账号、公钥授权、`gpustat` 是否可运行、所需 PATH/环境变量。 |
| 页面显示旧时间且红灯 | 这是断线保护行为：显示的是最后一次成功采集时间。检查该服务器的 SSH 和 `gpustat`。 |
| 页面没有服务器卡片 | 确认服务已收到请求、`servers.json` 格式为数组，且数据库目录可写。 |
| 终端出现控制符乱码 | 确认运行的是包含 DCS 清理逻辑的当前版本；必要时重启服务后等待下一次采样。 |
