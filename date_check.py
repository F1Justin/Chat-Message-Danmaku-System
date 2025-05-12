import datetime
from datetime import datetime, timedelta

# 检查数据库当前时间和现在时间的差距
db_latest_time_str = "2025-05-11 03:29:13"
db_latest_time = datetime.strptime(db_latest_time_str, "%Y-%m-%d %H:%M:%S")

# 显示时间
print(f"数据库最新消息时间: {db_latest_time}")
current_time = datetime.now()
print(f"当前系统时间: {current_time}")

# 时差检查
time_diff = db_latest_time - current_time
print(f"时差: {time_diff}")

# 关键问题：是否在查询未来的时间？
is_future = db_latest_time > current_time
print(f"数据库时间是否在未来: {is_future}")

if is_future:
    print("问题根源：最新消息时间在未来，这会导致新消息检测逻辑一直等待未来的时间点")
    print("建议：修正 app.py 中时间比较逻辑，或检查数据库中时间记录是否错误")

# 如果使用旧时间查询（跳回当前时间之前）
suggested_start_time = current_time - timedelta(hours=24)
print(f"\n建议查询起始时间: {suggested_start_time} (当前时间前24小时)") 