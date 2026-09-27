# 客户兴趣画像模块

供上层客户数据平台直接调用的 Python 库（无接口层），状态持久化在本地 SQLite 文件中，进程重启后完整恢复。

## 用法

```python
from interest_profile import InterestProfileService, Event

svc = InterestProfileService("profiles.db")

# 批量上报事件（每批最多 5000 条；乱序、重复均可）
svc.ingest([
    Event("ev-1", "anon-1", "anonymous", "page_view",
          "2026-09-20T10:00:00+08:00", " Books "),
    Event("ev-2", "cust-1", "customer", "purchase",
          "2005-06-15T12:00:00+00:00", "books"),   # 历史补录
])

svc.link("anon-1", "cust-1")    # 匿名ID关联到客户
svc.merge("cust-b", "cust-a")   # B 所在画像并入 A 所在画像（仅客户ID，不可撤销）
svc.unlink("anon-1")            # 撤销关联，其全部事件回到独立匿名画像

profile = svc.query("cust-1", "2026-09-28T00:00:00+08:00")
for s in profile.scores:
    print(s.category, s.short_score, s.long_score, s.fused_score)
svc.close()
```

## 规则要点

- 权重：`purchase=5`、`page_view=1`、`email_open=0.5`，其他类型不计入；类别去首尾空白转小写，空/缺失不计入。
- 衰减：贡献 = 权重 × 0.5^((t−事件时间)/半衰期)；短期半衰期 7 天，长期 90 天；融合分 = 0.7×短期 + 0.3×长期。
- 同一事件 ID 只算第一次，归属按第一次上报的标识。
- 标识首次出现（事件、关联、合并中）自动建档。

## 设计

- 每个（标识, 类别）与（画像, 类别）维护增量聚合：以该类别最晚事件时间为参考点 anchor，存衰减权重和。查询时只做一次衰减换算，O(类别数)，与事件量无关。
- 画像聚合恒等于其成员标识聚合之和。关联/合并为加法合并；撤销关联时从成员标识聚合重算画像，全程无浮点对消，保证与从头重算的参考值相对误差 < 1e-9。
- 标识上记录最大事件时间，用于"查询时刻早于画像中事件时间报错"的精确判定。

## 测试

```bash
python3 -m pytest tests/                 # 全部（含完整规模耗时测试，约 2~3 分钟）
python3 -m pytest tests/ --ignore=tests/test_perf.py   # 只跑正确性
PERF_SCALE=0.02 python3 -m pytest tests/test_perf.py   # 缩小规模快速检查耗时
```

- `tests/test_rules.py`：逐条业务规则单元测试。
- `tests/test_randomized.py`：乱序/重复事件、关联、多级合并、撤销、重启随机交替，每步后与原始事件重算的参考模型比对（类别集合一致，分数相对误差 ≤1e-9，参考值 <1e-12 时按绝对误差 1e-12）。
- `tests/test_perf.py`：20 万标识 / 300 万事件 / 200 类别 / 单客户 50 匿名ID + 20 万事件规模下的耗时验证。
