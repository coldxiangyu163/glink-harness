# 0006 子代理进度观察失败只降频不停止

- 状态：已接受（GLINK-OBS-01 第 4 项）
- 范围：`runs/child_progress_observer.py`

## 背景

子代理进度观察线程曾在一次 `poll()` 失败后永久停止；E2B 面一次临时的沙箱超时就会让本 run 余下时间
的子代理进度全部失明。

## 决定

`poll()` 失败只按失败次数有界退避（上限 `_PROGRESS_MAX_BACKOFF_SECONDS`）并继续重试；线程只在
finalize 时停止。失败以 `kernel_status` 警告呈现，不参与终态判定。
