# 客户兴趣画像库（interest-profile）

本地 SQLite 持久化的客户兴趣画像库，由上层客户数据平台直接调用，无接口层。
调用方串行使用；进程重启后状态完整保留。

## 安装与使用

```python
from interest_profile import InterestProfileStore, Event, KIND_ANONYMOUS, KIND_CUSTOMER

store = InterestProfileStore("./profile.db")

store.ingest([
    Event(event_id="e1", identity="anon-7", identity_kind=KIND_ANONYMOUS,
          event_type="purchase", timestamp="2026-09-20T08:00:00+08:00",
          category=" Sports "),
])
store.associate("anon-7", "cust-100")
store.merge("cust-200", "cust-100")      # cust-200 所在画像并入 cust-100
store.dissociate("anon-7")               # 撤销匿名关联（可再关联）
result = store.query("cust-100", "2026-09-28T00:00:00Z")
for category, score in result.categories.items():
    print(category, score.short, score.long, score.fused)
```

事件也可以用 dict 传入：`event_id / identity / identity_kind / event_type /
timestamp / category`，时间为带时区的 ISO 8601 字符串或 `datetime`。

## 业务规则

- 权重：`purchase=5`、`page_view=1`、`email_open=0.5`，其他类型不计分；
  类别先 `strip()` 再转小写，为空/缺失不计分。
- 贡献 = 权重 × 0.5^((t−事件时间)/半衰期)；短期半衰期 7 天，长期 90 天；
  融合分 = 0.7×短期 + 0.3×长期。
- 同一事件 ID 重复上报只算第一次，归属以第一次上报的标识为准。
- 匿名 ID 关联客户后，其过去与将来的事件都计入客户画像；关联到别的画像
  再关联抛 `AssociationError`，关联到同一画像是无操作。
- 客户画像之间可合并（仅限客户 ID，不可撤销）；多级合并效果与一次合并一致。
- 撤销匿名关联后，该匿名 ID 上报过的全部事件随它回到只含它自己的匿名画像，
  之后可再关联到别的客户；对未关联的标识撤销抛 `DissociationError`。
- 用任一标识查询返回其当前所属画像；查询时刻早于画像中最晚事件时间时抛
  `QueryTimeError`（未来事件不应参与打分）；结果只包含真实出现过的类别。
- 事件时间最早支持 2005 年（锚点为相对量，内部聚合不会溢出）。

## 实现要点（为何操作耗时不随画像事件数增长）

每条事件只在入库时处理一次：按 (标识, 类别) 和 (画像, 类别) 维护
增量聚合。聚合行保存 `(anchor, S)`，`anchor` 为该类别已见事件的最大时间，

```
S = Σ w·2**((t_e - anchor)/H)
查询时刻 t 的分数 = S · 2**((anchor - t)/H)
```

- 查询：只扫画像的类别聚合行（≤200 行），不扫事件。
- 关联 / 合并 / 撤销：只移动标识成员关系，并从各成员的标识级聚合行重算
  画像聚合，O(成员数 × 类别数)（≤51×200），与历史事件条数无关。
- 事件表只用于按事件 ID 去重；乱序到达、重复上报（批内/跨批）幂等。

## 测试

```bash
python3 -m pytest tests/                 # 规则测试 + 随机模型对比（默认跳过 perf）
IP_PERF=1 python3 -m pytest tests/ -s    # 含 20 万标识/300 万事件规模测试
python3 tests/test_perf.py               # 单独跑规模测试
```

- `tests/test_rules.py`：业务规则单元测试。
- `tests/test_randomized.py`：随机交替执行乱序/重复上报、关联、多级合并、
  撤销、重启，每一步后把全部画像与「按当前归属从原始事件从头重算」的
  参考结果比对（类别集合一致；分数相对误差 1e-9，参考绝对值 <1e-12 时
  按绝对误差 1e-12）。
- `tests/test_perf.py`：规模与耗时测试。本机实测：300 万事件入库平均
  约 1.5 万条/秒，单批 5000 条最大约 0.5s（<2s）；查询中位数约 0.4ms
  （<10ms）；画像 1k→200k 事件，查询/关联/合并/撤销耗时基本不变（约 1ms）。
