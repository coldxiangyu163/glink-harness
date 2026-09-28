# 0007 报告只认宿主产物副本

- 状态：已接受（2026-09-28，架构重设计 S1）
- 范围：`store.py::report_location`、`web_routes.py::_send_report`

## 背景

早期版本的 `report_path` 指向 agent 工作区 `workspaces/<id>/report.md`。宿主改为在收尾时把安全
报告复制到 `runs/<id>/artifacts/report.md` 后，下载端仍把旧工作区位置作为第二个只读来源保留，
于是位置规则有两种布局，需要额外的越界、链接与他人工作区负例。

## 决定

报告下载与 `hasReport` 只认一个固定位置：宿主产物副本 `runs/<id>/artifacts/report.md`。
`report_path` 不是下载能力；工作区报告、外部路径与链接一律视为无报告（404）。旧布局的报告不再可下载，
不迁移。

## 后果

回归：`tests/test_runs_api.py::test_only_the_host_artifact_copy_is_served_as_the_report`、
`test_a_host_artifact_behind_a_directory_link_is_not_served`、`tests/test_redaction.py` 的越界与链接负例。
