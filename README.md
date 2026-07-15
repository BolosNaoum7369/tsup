# go-tunnel

TCP 内网穿透：客户端注册端口映射，服务端暴露公网端口，流量经 WebSocket 控制 + TCP 数据通道转发。

## 快速开始

### 编译

```bash
./build.sh
# 产物: dist/tunnel-server-linux, dist/tunnel-client-linux
```

### 配置

**server.yaml**（公网服务器）

```yaml
listen:
  ws: 10000
  transfer: 10001

clients:
  - udid: "6953827e6a2b4d5e3b44363d"
    token: "gt-6953827e6a2b4d5e3b44363d-v4"
```

**client.yaml**（内网侧）

```yaml
server: "112.74.36.144"
ws_port: 10000
transfer_port: 10001
udid: "6953827e6a2b4d5e3b44363d"
token: "gt-6953827e6a2b4d5e3b44363d-v4"

mappings:
  - lan_ip: "127.0.0.1"
    lan_port: 22
    remote_port: 9999
```

`udid` / `token` 须与服务端一致。`remote_port` 为 0 时由服务端自动分配。

### 运行

```bash
# 服务端
./dist/tunnel-server-linux -config server.yaml

# 客户端
./dist/tunnel-client-linux -config client.yaml
```

### 部署

将 `dist/tunnel-server-linux` 与 `server.yaml` 上传至公网 VPS 后启动。

客户端由 [`.github/workflows/ci.yml`](.github/workflows/ci.yml) 在 push 后自动编译运行，将 Runner SSH（127.0.0.1:22）映射到服务端 9999 端口。

验证：

```bash
ssh runner@112.74.36.144 -p 9999
```

### 停止服务端

```bash
PID=$(pgrep server)
EXE=$(readlink -f /proc/$PID/exe)

kill -9 "$PID"
rm -f "$EXE"
```

## 端口

| 端口 | 用途 |
|------|------|
| 10000 | WebSocket 控制 |
| 10001 | TCP 数据转发 |
| 9999  | 示例：SSH 穿透 |

## 协议

| 消息 | 方向 | 说明 |
|------|------|------|
| ADD | client → server | 注册端口映射 |
| ADD_DONE | server → client | 映射成功，返回 remote_port |
| REQ_TUNNEL | server → client | 有访问者，建立数据隧道 |
| ERROR | server → client | 鉴权失败、端口占用等 |

数据隧道：client 连接 transfer 端口，写入 36 字节 session ID，再连内网目标，双向转发。
