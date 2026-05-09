# !!! 只能用于单机单卡程序，自动使用当前服务器上最空闲的GPU

# 基本用法
exec(__import__('urllib.request').request.urlopen('http://172.18.167.15:2223/set_gpu').read().decode())
import os
print(f"CUDA_VISIBLE_DEVICES: {os.environ.get('CUDA_VISIBLE_DEVICES')}", end="\n\n")

# 指定分数阈值、排除服务器 
exec(__import__('urllib.request').request.urlopen('http://172.18.167.15:2223/set_gpu?t1=5&t2=30&ex=server15&ex=server16').read().decode())
import os
print(f"CUDA_VISIBLE_DEVICES: {os.environ.get('CUDA_VISIBLE_DEVICES')}")

# 输出示例（在Server30上运行）
# MY_IP: 172.18.167.128
# Local GPU busy scores, low to high:
#   3.63  server30:7  172.18.167.128  NVIDIA A30  util=0%  mem=519/24576MiB(2.1%)  power=26/165W  temp=30C  proc=0
#   3.80  server30:2  172.18.167.128  NVIDIA A30  util=0%  mem=521/24576MiB(2.1%)  power=27/165W  temp=33C  proc=0
#   3.82  server30:1  172.18.167.128  NVIDIA A30  util=0%  mem=521/24576MiB(2.1%)  power=28/165W  temp=32C  proc=0 <-- SELECTED
#   3.85  server30:4  172.18.167.128  NVIDIA A30  util=0%  mem=519/24576MiB(2.1%)  power=29/165W  temp=31C  proc=0
#   3.88  server30:3  172.18.167.128  NVIDIA A30  util=0%  mem=519/24576MiB(2.1%)  power=29/165W  temp=32C  proc=0
#   3.88  server30:5  172.18.167.128  NVIDIA A30  util=0%  mem=519/24576MiB(2.1%)  power=29/165W  temp=32C  proc=0
#   4.00  server30:6  172.18.167.128  NVIDIA A30  util=0%  mem=519/24576MiB(2.1%)  power=31/165W  temp=32C  proc=0
#  77.37  server30:0  172.18.167.128  NVIDIA A30  util=100%  mem=15629/24576MiB(63.6%)  power=153/165W  temp=60C  proc=2
# Selected GPU: server30:1 score=3.82 (idle threshold=10.00, busy threshold=50.00)
# Using GPU: 1
# CUDA_VISIBLE_DEVICES: 1

# MY_IP: 172.18.167.128
# Local GPU busy scores, low to high:
#   3.63  server30:7  172.18.167.128  NVIDIA A30  util=0%  mem=519/24576MiB(2.1%)  power=26/165W  temp=30C  proc=0
#   3.80  server30:2  172.18.167.128  NVIDIA A30  util=0%  mem=521/24576MiB(2.1%)  power=27/165W  temp=33C  proc=0
#   3.82  server30:1  172.18.167.128  NVIDIA A30  util=0%  mem=521/24576MiB(2.1%)  power=28/165W  temp=32C  proc=0
#   3.85  server30:4  172.18.167.128  NVIDIA A30  util=0%  mem=519/24576MiB(2.1%)  power=29/165W  temp=31C  proc=0
#   3.88  server30:3  172.18.167.128  NVIDIA A30  util=0%  mem=519/24576MiB(2.1%)  power=29/165W  temp=32C  proc=0
#   3.88  server30:5  172.18.167.128  NVIDIA A30  util=0%  mem=519/24576MiB(2.1%)  power=29/165W  temp=32C  proc=0 <-- SELECTED
#   4.00  server30:6  172.18.167.128  NVIDIA A30  util=0%  mem=519/24576MiB(2.1%)  power=31/165W  temp=32C  proc=0
#  77.37  server30:0  172.18.167.128  NVIDIA A30  util=100%  mem=15629/24576MiB(63.6%)  power=153/165W  temp=60C  proc=2
# Selected GPU: server30:5 score=3.88 (idle threshold=5.00, busy threshold=30.00)
# Using GPU: 5
# CUDA_VISIBLE_DEVICES: 5