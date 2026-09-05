-- ─────────────────────────────────────────────────────────────────────────
-- 本地联调种子数据(M6 客服 agent 能力测试)—— 仅限本地 dev 库,禁止用于生产!
-- 账号统一:10245101666@stu.ecnu.edu.cn / 12345678(超级管理员,同时用于
--   管理端 3000 与官网客户端 3001 登录);种子候选人密码同为 12345678。
-- 幂等策略:先删本脚本自有的 9000+ 显式 ID 行,再插入;可重复执行。
-- 覆盖能力:开放周期/简历检索与详情(含 PII 脱敏样本)/可用场次/待分配/
--   调剂申请/统计/候选卡片(含获奖)。
-- ─────────────────────────────────────────────────────────────────────────
USE official;

SET @pwd = '$2a$10$Es9rKAAwSn8vKUMD/EnVUOyDAqb5bFeh6BrD1vzOFeXC2nAOubhFi'; -- 12345678
SET @svc = '$2a$10$HR6e7xI3k3L5fkA5xPM/JOZ0XFsUpfG26nagOoFpDRK4zX1Lz7d4e'; -- AgentTest#2026(服务账号)

-- ── 清理旧种子(仅 9000+ 显式段) ─────────────────────────────────────────
DELETE FROM interview_reschedule_request WHERE request_id >= 9000;
DELETE FROM interview_result       WHERE result_id   >= 9000;
DELETE FROM interview_schedule     WHERE schedule_id >= 9000;
DELETE FROM interview_session_dept WHERE id          >= 9000;
DELETE FROM interview_session      WHERE session_id  >= 9000;
DELETE FROM interview_slot         WHERE slot_id     >= 9000;
DELETE FROM interview_time_slot    WHERE time_slot_id>= 9000;
DELETE FROM resume_field_value     WHERE value_id    >= 9000;
DELETE FROM resume_field_definition WHERE field_id    >= 9000;
DELETE FROM resume                 WHERE resume_id   >= 9000;
DELETE FROM interview_preference_time WHERE id       >= 9000;
DELETE FROM interview_preference      WHERE preference_id >= 9000;
DELETE FROM award_experience       WHERE award_id    >= 9000;
DELETE FROM user_role              WHERE user_id     >= 9000;
DELETE FROM user                   WHERE user_id     >= 9000 OR username = 'svc-agent-local';

-- ── 用户(密码均为 12345678)─────────────────────────────────────────────
-- 9001 王调试=超级管理员(统一调试账号);9002-9005 候选人四种典型状态;9006 社员
INSERT INTO user (user_id, username, password, name, email, phone, major, status, is_deleted) VALUES
  (9001, '10245101666', @pwd, '王调试', '10245101666@stu.ecnu.edu.cn', '13800001666', '计算机科学', 1, 0),
  (9002, 'test_chenxm', @pwd, '陈小明', 'chenxiaoming@stu.ecnu.edu.cn', '13812340001', '软件工程',   1, 0),
  (9003, 'test_linxue', @pwd, '林雪',   'linxue@stu.ecnu.edu.cn',       '13812340002', '数据科学',   1, 0),
  (9004, 'test_zhangyuan', @pwd, '张远', 'zhangyuan@stu.ecnu.edu.cn',    '13812340003', '人工智能',   1, 0),
  (9005, 'test_liuqi',  @pwd, '刘淇',   'liuqi@stu.ecnu.edu.cn',        '13812340004', '统计学',     1, 0),
  (9006, 'test_zhaomember', @pwd, '赵社员', 'zhaosheyuan@stu.ecnu.edu.cn','13812340005', '电子信息', 1, 0);

INSERT INTO user (user_id, username, password, name, email, status, is_deleted) VALUES
  (8999, 'svc-agent-local', @svc, 'Agent服务账号', 'svc-agent-local@stu.ecnu.edu.cn', 1, 0);

INSERT INTO user_role (user_id, role_id) VALUES
  (8999, 1),
  (9001, 1),              -- 超级管理员:agent 装配全部 9 个只读工具
  (9002, 4), (9003, 4),   -- 申请人:候选人视角(get_open_cycle + get_my_interview)
  (9004, 4), (9005, 4),
  (9006, 3);              -- 社员:公共查询面工具集

-- ── 简历字段定义:cycle 3 已有 1姓名/2专业/3年级/4意向部门/5自我介绍,
--    只补 phone(用于 agent 读取与 PII 脱敏测试)───────────────────────────
INSERT INTO resume_field_definition (field_id, cycle_id, field_key, field_label, field_type, is_required, sort_order) VALUES
  (9001, 3, 'phone', '联系电话', 'text', 0, 0);

-- ── 简历(四个候选人,状态/分数各不相同)─────────────────────────────────
-- status:1草稿 2已提交 3评审中 4通过 5未通过
INSERT INTO resume (resume_id, user_id, cycle_id, status, resume_score, submitted_at) VALUES
  (9001, 9002, 3, 2, 0,  NOW()),              -- 陈小明:已提交未评审 → 待分配
  (9002, 9003, 3, 2, 88, NOW()),              -- 林雪:已提交,已安排面试
  (9003, 9004, 3, 4, 92, NOW()),              -- 张远:已通过
  (9004, 9005, 3, 3, 65, NOW());              -- 刘淇:评审中 + 调剂申请

-- 字段映射(现有 def):1姓名 2专业 3年级 4意向部门 5自我介绍 9001联系电话
INSERT INTO resume_field_value (value_id, resume_id, field_id, field_value) VALUES
  (9001, 9001, 1,    '陈小明'), (9002, 9001, 2,    '软件工程'),
  (9003, 9001, 3,    '2025级'), (9004, 9001, 4,    '技术部'),
  (9005, 9001, 5,    '大一就想加入技术部,写过两年 Python,做过课表小程序'),
  (9006, 9001, 9001, '13812340001'),
  (9011, 9002, 1,    '林雪'),   (9012, 9002, 2,    '数据科学'),
  (9013, 9002, 3,    '2024级'), (9014, 9002, 4,    '技术部'),
  (9015, 9002, 5,    '校数据挖掘比赛二等奖,熟悉机器学习基础,希望做数据方向'),
  (9016, 9002, 9001, '13812340002'),
  (9021, 9003, 1,    '张远'),   (9022, 9003, 2,    '人工智能'),
  (9023, 9003, 3,    '2024级'), (9024, 9003, 4,    '技术部'),
  (9025, 9003, 5,    '写过社团官网后端接口,熟悉 Spring Boot,蓝桥杯省一等奖'),
  (9026, 9003, 9001, '13812340003'),
  (9031, 9004, 1,    '刘淇'),   (9032, 9004, 2,    '统计学'),
  (9033, 9004, 3,    '2025级'), (9034, 9004, 4,    '媒体部'),
  (9035, 9004, 5,    '会剪视频会写文案,运营过 2w 粉账号'),
  (9036, 9004, 9001, '13812340004');

-- ── 面试时间段与场次(未来日期,保证「可用场次」有货)────────────────────
INSERT INTO interview_time_slot (time_slot_id, cycle_id, slot_name, interview_date, start_time, end_time, status) VALUES
  (9001, 3, '9月12日上午场', '2026-09-12', '09:00', '12:00', 1),
  (9002, 3, '9月12日下午场', '2026-09-12', '14:00', '17:00', 1),
  (9003, 3, '9月13日上午场', '2026-09-13', '09:00', '12:00', 1);

-- interview_type:1 线下 / 2 线上
INSERT INTO interview_slot (slot_id, cycle_id, interview_date, start_time, end_time, location, interview_type, meeting_link, max_capacity, current_occupied, status) VALUES
  (9001, 3, '2026-09-12', '09:00', '10:00', '大学生活动中心 301', 1, NULL, 4, 1, 1),
  (9002, 3, '2026-09-12', '10:00', '11:00', '大学生活动中心 301', 1, NULL, 4, 0, 1),
  (9003, 3, '2026-09-12', '14:00', '15:00', '线上',              2, 'https://meeting.tencent.com/dm/r/test-agent', 6, 1, 1),
  (9004, 3, '2026-09-13', '09:00', '10:00', '大学生活动中心 302', 1, NULL, 4, 0, 1);

INSERT INTO interview_session (session_id, cycle_id, time_slot_id, dept_id, location, capacity, current_occupied, interview_duration_minutes, status) VALUES
  (9001, 3, 9001, 1, '大学生活动中心 301', 3, 1, 30, 1),  -- 技术部 上午场
  (9002, 3, 9001, 4, '大学生活动中心 302', 3, 0, 30, 1),  -- 媒体部 上午场
  (9003, 3, 9002, 1, '大学生活动中心 301', 3, 1, 30, 1);  -- 技术部 下午场

INSERT INTO interview_session_dept (id, session_id, dept_id) VALUES
  (9001, 9001, 1), (9002, 9002, 4), (9003, 9003, 1);

-- ── 面试安排(status:0 未安排 / 1 已安排 / 2 已取消)─────────────────────
-- 9001 陈小明:未安排 → list_unassigned 有数据
INSERT INTO interview_schedule (schedule_id, resume_id, user_id, cycle_id, dept_id, status, notes, sync_status) VALUES
  (9001, 9001, 9002, 3, NULL, 0, '简历待分配面试场次', 0);
-- 9002 林雪:已安排(9/12 上午,技术部,带场次+时段)
INSERT INTO interview_schedule (schedule_id, resume_id, user_id, cycle_id, dept_id, slot_id, session_id, interview_time, status, notes, sync_status) VALUES
  (9002, 9002, 9003, 3, 1, 9001, 9001, '2026-09-12 09:00:00', 1, '林雪 技术部面试', 0);
-- 9003 张远:已面完(9/5 下午,技术部)→ 挂结果
INSERT INTO interview_schedule (schedule_id, resume_id, user_id, cycle_id, dept_id, slot_id, session_id, interview_time, status, notes, sync_status) VALUES
  (9003, 9003, 9004, 3, 1, 9003, 9003, '2026-09-05 14:00:00', 1, '张远 技术部面试(已完成)', 0);
-- 9004 刘淇:已安排但申请调剂(9/12 下午 → 希望换上午)
INSERT INTO interview_schedule (schedule_id, resume_id, user_id, cycle_id, dept_id, slot_id, session_id, interview_time, status, notes, sync_status) VALUES
  (9004, 9004, 9005, 3, 4, 9003, 9003, '2026-09-12 14:00:00', 1, '刘淇 媒体部面试(申请调剂中)', 0);

-- ── 面试志愿(list_unassigned 语义 =「已填志愿未分到场次」)──────────────
-- 陈小明填了志愿但没分到场次 → 进待人工调剂名单;刘淇已分到场次 → 不在此列
INSERT INTO interview_preference (preference_id, resume_id, cycle_id, first_dept_id, second_dept_id) VALUES
  (9001, 9001, 3, 1, 2);

INSERT INTO interview_preference_time (id, resume_id, time_slot_id) VALUES
  (9001, 9001, 9001), (9002, 9001, 9003);

-- ── 面试结果(decision:0 待定 1 通过 2 不通过 3 待调剂)─────────────────
INSERT INTO interview_result (result_id, schedule_id, user_id, decision, assigned_dept_id, decision_by, resume_id, cycle_id) VALUES
  (9001, 9003, 9004, 1, 1, 1, 9003, 3);  -- 张远 通过 → 录取技术部

-- ── 调剂申请(status:0 待处理)───────────────────────────────────────────
INSERT INTO interview_reschedule_request (request_id, schedule_id, resume_id, user_id, cycle_id, reason, preferred_time_slot_ids, status) VALUES
  (9001, 9004, 9004, 9005, 3, '与《概率论》课程时间冲突,希望调到 9/12 上午或 9/13 上午', '9001,9003', 0);

-- ── 获奖经历(候选卡片数据)───────────────────────────────────────────────
INSERT INTO award_experience (award_id, user_id, award_name, award_time, description) VALUES
  (9001, 9003, '校级程序设计竞赛二等奖', '2025-11-20', 'ECNU 校赛,Team 前 10%'),
  (9002, 9004, '蓝桥杯上海赛区一等奖',   '2025-05-10', 'Java 程序设计大学 B 组');
