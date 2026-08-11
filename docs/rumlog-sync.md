# RUMLogNG 双向同步 — 部署说明

## 安装 crontab

`crontab -e` 添加（每 5 分钟一轮；`flock` 防重入）：

```bash
*/5 * * * * cd /Users/cheenle/HAM/ft8 && flock -n data/rumlog-sync.lock venv/bin/python -m rumlog_sync >> data/rumlog-sync.log 2>&1
```

## 配置

`data/rumlog-sync.json`（缺失时用默认值；模板见 `rumlog_sync/config.example.json`）。
关键项：`rumlog_db`（RUMLogNG Core Data 路径）、`my_call`/`my_grid`、`confirm_retries`。
`udp_*` 字段保留为备用（WSJT-X UDP 2237 模块，实测 RUMLogNG 6.5 未激活 UDP 解析，推送走 AppleScript）。

## 首次运行

```bash
venv/bin/python -m rumlog_sync   # 自动全量合并两边历史（规格 D2）
```

首轮会：拉取 RUMLogNG 全部记录合并进 FT8 db（SSB/FT4 也并入，以 RUMLogNG 为准覆盖字段），
并把 FT8 db 独有的历史 QSO 经 AppleScript 推入 RUMLogNG（分批 15 条/次，171 条约 1–2 分钟）。

## 冒烟验证（可选）

```bash
venv/bin/python -m rumlog_sync --smoke   # 推一条标记 QSO（N0SMK, 1970 UTC）验证落库；验证后手动删除
```

## 退出码

`0` = 完成（含 RUMLogNG 打开失败的软错误）；`2` = 配置错误；`3` = 另一实例持锁。

## 已知注意事项

- **RUMLogNG 侧重复已清理（2026-08-11）**：首次同步期间 FT8 db 的历史重复行（JTDX 导入遗留）曾全量推入 RUMLogNG。已备份并直接清理 Core Data（重复 125 条 + 测试记录 4 条 → 14886 条，重复 0）；清理为一次性运维操作（用户指示），同步程序本身仍只读 RUMLogNG（AD-016）。后续新 QSO 不会再有此问题（推送前去重 + 拉取确认时全量确认）。
- **FT8 db 重复历史**：JTDX 导入遗留的重复行保留在 FT8 db（已全部确认，不会再推）；如需清理需在服务器侧处理（同步程序不删除服务器记录）。
- **AppleScript 依赖**：推送要求 RUMLogNG 正在运行且 macOS 允许 `osascript` 控制（首次可能弹出自动化权限确认，允许后即可）。
- **禁写 RUMLogNG 数据库**（AD-016）：同步程序只读 RUMLogNG 的 Core Data SQLite。
