-- ============================================================
-- PostgreSQL LISTEN/NOTIFY 触发器
-- 用于实时监听新消息插入事件
-- ============================================================

-- 创建通知函数
CREATE OR REPLACE FUNCTION notify_new_message()
RETURNS TRIGGER AS $$
DECLARE
    payload JSON;
BEGIN
    -- 构建通知负载
    -- 只发送必要的信息，完整数据由应用层查询
    payload := json_build_object(
        'id', NEW.id,
        'session_persist_id', NEW.session_persist_id,
        'message_id', NEW.message_id,
        'time', NEW.time::text
    );
    
    -- 发送通知到 'new_message' 频道
    PERFORM pg_notify('new_message', payload::text);
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 删除已存在的触发器（如果有）
DROP TRIGGER IF EXISTS message_insert_notify ON nonebot_plugin_chatrecorder_messagerecord;

-- 创建触发器：在插入新消息后触发
CREATE TRIGGER message_insert_notify
    AFTER INSERT ON nonebot_plugin_chatrecorder_messagerecord
    FOR EACH ROW
    EXECUTE FUNCTION notify_new_message();

-- 验证触发器是否创建成功
-- SELECT * FROM pg_trigger WHERE tgname = 'message_insert_notify';

-- ============================================================
-- 回滚脚本（如需删除）
-- ============================================================
-- DROP TRIGGER IF EXISTS message_insert_notify ON nonebot_plugin_chatrecorder_messagerecord;
-- DROP FUNCTION IF EXISTS notify_new_message();

