# 隔离测试

测试固定连接 `127.0.0.1:55439`、用户 `danmuku_test`、库 `postgres`，不读取 `.env` 中的生产连接。只能使用专门初始化的临时 PostgreSQL；不要把端口转发到生产库。

```bash
python3 -m venv /tmp/danmuku-tests
/tmp/danmuku-tests/bin/pip install -r requirements-dev.txt
initdb -D /tmp/danmuku-pg-test -A trust -U danmuku_test
pg_ctl -D /tmp/danmuku-pg-test -l /tmp/danmuku-pg-test.log \
  -o '-h 127.0.0.1 -p 55439 -k /tmp' start
/tmp/danmuku-tests/bin/python tests/seed_database.py
/tmp/danmuku-tests/bin/python -m pytest -q
node --test tests/renderer.test.cjs
pg_ctl -D /tmp/danmuku-pg-test stop
```

种子脚本只创建本项目所需的表、两个合成群和通知触发器。测试覆盖 HTTP／WebSocket 角色及跨房间授权、Cookie、历史权限、注入过滤／去重、配置持久化、CLI 文件权限、真实数据库消息推送、连接终止后补发，以及处理失败后的游标重试。
