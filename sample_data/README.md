# 样例数据

计费期间一律 `[期间起, 期间止)` 半开区间，日期按订阅时区的本地日；金额单位元，入库 Decimal。

| 文件 | 编码 | 用途 |
|---|---|---|
| `subscriptions_dirty.csv` | UTF-8 带 BOM | 主样例：千分位、全角数字、三位小数、期间倒置、多一列、重复键冲突、未知时区、未知状态、缺价、空行 |
| `changes_dirty.csv` | UTF-8 | 变更主样例：Z 与 +08:00 混用、夏令时重叠段、同日两次变更、同一时刻两条、乱序迟到、期间外变更、非零点生效、未知订阅 |
| `subscriptions_renewal.csv` | UTF-8 | 续费样例：同一订阅连续三个账期、同账期重复导出（同值/异值）、账期重叠、同账单月两个账期 |
| `changes_renewal.csv` | UTF-8 | 续费样例变更：分别落入第 2/3 个账期的变更、落在所有账期之外的变更 |
| `subscriptions_gbk.csv` | GB18030 | 编码回退链：同一套代码路径读国标编码 |
| `subscriptions_header_only.csv` | UTF-8 | 空态：仅表头 |
| `subscriptions_blank.csv` | UTF-8 | 空态：全空行 |
| `subscriptions_quoted.csv` | UTF-8 | 引号内含换行：逻辑行数 = 记录数 |
| `subscriptions_truncated.csv` | UTF-8 | 截断尾行：引号未闭合 |

主样例里有 16 条订阅记录（其中 6 条会被隔离）、14 条变更记录（其中 1 条未知订阅被隔离）。
