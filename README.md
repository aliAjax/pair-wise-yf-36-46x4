# 生物样本库知情同意与撤回

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8302`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8302
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `participant`：参与者；`consent`：同意版本；`sample`：样本；`withdrawal`：撤回申请。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`；动作可带`Idempotency-Key`请求头。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 撤回单一次执行

已批准（`approved`）的撤回单不再需要逐张同意、逐个样本手工处理，执行动作在**单个数据库事务**内完成级联收口：

1. 同一参与者所有**生效中（active）**的同意版本一起置为`withdrawn`；
2. 批准时关联的样本：在库（`stored`）的直接`destroyed`，借出中（`on_loan`）的先置为`return_pending`（待归还）；
3. 待归还样本归还后（`return`动作）直接销毁，不会回到在库。

配套动作：

- `plan_execute`（data: `{"executed_at": ...}`）：只读预览，返回影响范围——将撤回的同意、将销毁/待归还的样本，以及`expected_versions`版本快照。页面先展示该清单。
- `execute`（data: `{"executed_at": ..., "expected_versions": {...}}`）：按预览快照执行。若执行前同意版本/状态或样本状态已变化（例如同意被替代、样本被匿名化、出现了预览外新生效同意），整体拒绝并返回409，响应体`details`逐条说明冲突对象、当前版本与状态，撤回单与关联对象保持原状。
- 相同执行请求带上相同的`Idempotency-Key`重试，直接返回首次执行结果（即使撤回单此后已是`executed`）；不带幂等键的重复执行返回409。

执行结果返回处理后的各对象状态（`withdrawal`/`consents`/`samples`及汇总计数），撤回单data中记录`consent_ids`、`destroyed_sample_ids`、`pending_return_sample_ids`，每个对象的状态变化都写入审计时间线。

演示页面（`/`）提供：新建/激活同意、入库/借出/归还样本、批准撤回、**预览影响范围 → 一次执行 → 查看处理后状态**的完整操作，以及“生成演示数据”按钮。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

样本销毁和外部机构调用是流程演示，不会自动删除真实存储中的样本。
