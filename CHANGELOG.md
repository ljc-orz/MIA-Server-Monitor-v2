# 更新记录

当前版本：26.08.12

## 26.08.12（2026-08-12）

- 为每台服务器增加 `env` 配置，可在执行 `gpustat` 时注入服务器专属环境变量。
- 修改247的`gpustat`程序，使其为CUDA Runtime给出的device ordinal（CUDA_DEVICE_ORDER=FASTEST_FIRST）。
- 修复终端能力查询（DCS/XTGETTCAP）控制序列残留为 `+q...` 乱码的问题。
- 服务器掉线时保留最后一次成功采集时间，并以红色状态灯标识断线。
- 增加网页内更新记录入口。
