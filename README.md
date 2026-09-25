# 野生动物疫病监测与离线同步

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8305`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8305
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `observation`：现场观察；`sample`：样本与实验室结果；`cluster`：异常聚集事件。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `POST /api/batches`：巡护队离线观察整批回传。
- `GET /api/batches/<device_id>/<batch_no>`：查询某设备批次的首次应答。

### 离线批次回传规则

请求体：

```json
{
  "device_id": "D-001",
  "batch_no": "B-20260925-01",
  "observations": [
    {"event_id": "E-1", "species": "麋鹿", "location": "北湖岸",
     "observed_at": "2026-09-24", "lat": 30.12, "lon": 120.34}
  ]
}
```

- **幂等**：同一`device_id`+`batch_no`再次到达时，直接沿用第一次的应答
  （包括首次被退回时的409应答），不会重复落库。
- **同一条观察**：`event_id`相同且`observed_at`日期相同（忽略时分秒）。
- 批内完全重复的观察折叠为一条；批内同键但内容不同则请求校验失败。
- 与服务器已有观察内容相同：跳过（返回`skipped`及原因）。
- 与服务器已有观察内容不同（例如补正过的物种或坐标）：**整批退回**，
  HTTP 409，`conflicts`逐项列出上送版本与服务器版本（含`server_version`
  和`server_entity_id`），无冲突的条目也不会部分写入。
- 判定与写入在单个`BEGIN IMMEDIATE`事务内完成，退回时整批回滚；
  只有受理成功或整批退回的批次才会保存应答，畸形请求不落应答。
- 批次提交角色为`admin`/`field`。首页可直接提交批次JSON并查询结果。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

离线批次回传覆盖幂等、批内去重、与服务器版本比对和整批事务，但不包含真实野外通信协议、地图底图或完整空间索引。
