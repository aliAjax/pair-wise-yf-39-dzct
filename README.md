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
- `POST /api/observation-batches`：巡护队离线观察**整批回传**。
- `GET /api/observation-batches?device_id=&batch_no=`：查看某批次的处理结果。

### 离线批次回传规则

请求体：`{"device_id","batch_no","observations":[{event_id, observed_at, species, location, lat, lon}, ...]}`，需 `field` 或 `admin` 角色。

- **批次幂等**：同一 `device_id + batch_no` 再次到达（网络抖动重发）时，原样返回第一次的应答；第一次之后的任何改动都不会重新求值或覆盖。第一次为退回的批次，重发也得到同一份退回应答。
- **同一观察判定**：`event_id` 与 `observed_at` 都相同即视为同一条观察，批内出现重复键直接 400 拒绝整批。
- **内容相同跳过**：服务器已有同键观察且 `species/location/lat/lon` 完全相同，计入 `skipped`，不重复建单。
- **内容不同整批退回**：任一观察内容不同（例如值班员已核对、补正过物种或坐标），**整批**回滚并返回 `409`，`conflicts` 中逐项列出请求内容与服务器版本（`server_entity_id/server_version/server_status/server`），批次内其他新观察也不会落库。
- 全程在单事务（`BEGIN IMMEDIATE`）内完成，冲突检测、写入和批次应答记录原子提交。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

离线同步使用批次和幂等键演示，批次覆盖观察的整批回传、重发回放与整批冲突退回，但不包含真实野外通信协议、地图底图或完整空间索引。
