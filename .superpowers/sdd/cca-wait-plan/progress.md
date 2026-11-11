# SDD ledger — plan: docs/specs/cca-wait-plan.md

## Pre-flight（跨任务接口扫描）
- Task4 消费 Task2 的 wait_not_before() 与 Task3 的 wait_targets()：名字一致 ✓
- Task5 消费 Task1 的状态函数与 Task4 的 run_wait_entry()：一致 ✓
- Task2 的测试注入点 CCA_WAIT_SNAPSHOT_FILE 只在 Task2 定义、Task5 复用 ✓
- Pre-flight: no unresolved conflicts
- Task 1+2: Ruling: 合并成一个提交（两者在一次编辑里落地，拆开只会留下一个跑不起来的中间态）—— 代价：提交粒度比计划粗一档
- Task 1+2: Ruling: 新函数命名 wait_usage_snapshot() —— 文件里已有 usage_snapshot(name)（面板用），
  同名会把面板的调用打成 TypeError（全套用例当场抓住）—— 代价：多一个前缀
- Task 2: Ruling: `--window auto` 取"5 小时窗口的未来重置，没有就看 7 天"，**不看用量百分比**
  （实测 95% 就已被限流，百分比不是判据；安全性由触发时刻的判定保证：那时只推真正卡住的会话）
  —— 代价：健康号登记后会白等到重置时刻，但到点也不会乱推
- Task 4: Ruling: `skipped`（会话真在跑）也记为 pending 并写 retry_after —— 与 retry 同一条路径，
  下一轮再看；不然条目会立刻收尾成 done，漏掉那条会话 —— 代价：那条会话会被反复检查（可接受）
- Task 4 修正记录: 一次替换把 `else`（记 sent/failed）误删，用例当场抓住（results 为空）；
  已补回并全绿
- Task 5: Ruling: `--cancel` 是取值参数（`--cancel <id>`），不是布尔 —— 第一次写成 bool_flags，
  用例当场报"未知参数 --cancel" —— 代价：无
- Task 5 修正记录: "N 条等待中" 那行第一次插错函数（落在 cmd_sessions 的尾部），已挪进 cmd_list

## 真机 dry-run 后的修正批次（提交 2c9bfdf）
- 修正记录: 原判定按"最后一条记录 + 进程死活"→ 真机四条卡住的会话全被判"真在跑"跳过。
  根因两条：(a) 停着的后台会话进程仍活着；(b) 被额度卡住后监控任务每半分钟写一条
  `queue-operation`，文件 mtime 一直很新。
- Ruling: 判据换成"最后一条**对话**记录自己的时间戳"（跳过记账类；`need_ts=True` 顺带滤掉
  `relocated` 这类无时间戳的元数据），两分钟内 + 进程活着才判"真在跑" —— 代价：尾部 256KB 若被
  记账记录占满要多读一次（2MB 上限），只读得到记账记录时保守跳过
- Ruling: `need_ts` 作为 `last_transcript_entry()` 的可选参数而不是新函数 —— 代价：签名多一个开关
- 修正记录: `claude stop` 只认短 job id（实测 `stop <完整UUID>` 回 `No job matching`），
  原实现的 stop 一直是空转 → resume 会被 "running in the background" 拒掉，一条都推不动。
  已加 `wait_job_id()`，在面板里按会话 id 反查短 job id
- 修正记录: 拒绝字样实测是 `running in the background`，原桩里编的
  `running as a background session` 匹配不上 → 会把 skip 误记成 failed。已改成
  `WAIT_HELD_MARKERS`（两种都收），桩也换成实测原文
- Ruling: 动手前加 `wait_log_motion()`（`claude logs` 隔 8 秒比对两次，还在动就不 stop）——
  stop 会掐掉正在跑的长任务；实测四条卡住的会话 6 秒内输出逐字节相同，零误跳 ——
  代价：每条推之前多 8 秒
- Ruling: 推的原语保持 `-p --resume`（同步跑完这一轮，输出里能看出额度放开没有）。
  实测 `--bg --resume <完整id> '<提示>'` 也能重新挂回面板，但它立刻返回 `backgrounded`，
  失败要等下一轮才发现 —— 退避重试会假成功，故不做默认
- Task 6 真机验收: 判定与三条原语已记录进 spec §10；端到端待 01:39 重置后补记
- 修正记录: `--only` / 位置参数写短 id 会把推送打断（`--resume` 只认完整 UUID，实测报
  `--resume requires a valid session ID`），而 `--dry-run` 显示的正是短 id。加 `wait_full_id()`
  在面板里补全；`wait_session_cwd()` 顺带改成先读面板（免费），读不到再扫全部 transcript
- 修正记录: `--detach` 原来起的是 PATH 里的 `cca`（本机指向主 checkout 0.19.0，**没有 `wait`**）
  → 登记成功、到点什么都不发生。改成当前脚本的绝对路径（`Path(__file__).resolve()`）

## 评审（fresh reviewer，df527b0..645ba4b + 新增提交）与处置
- 结论: **No（不可合）**，1 个 Critical + 8 个 Important；已在合并前修掉 Critical 与其中 4 个 Important
- Critical C1（**已修**）: `pending` 被当成"推过了"永久跳过 —— 退避重试形同虚设，
  `WAIT_RETRY_HORIZON` 成死代码，记录永远 waiting、`--run` 永不退出。
  真机主路径会踩到（重置时刻刚到点、窗口还没真放开那一次失败）。修法：`sent`/`failed` 才算终结，
  `pending` 下一轮接着推；单条按 `first_at` 算 1 小时 horizon；整条按 `started_at` 算 24 小时收尾；
  `results` 补 `tries`/`first_at`/`at`。用例：`test_run_wait_entry_pending_really_retries_next_round`、
  `test_run_wait_entry_horizon_and_give_up`（原用例第二轮设在 1 秒后、还在退避窗口里，等于没测）
- Critical C2（**已在 645ba4b 修**）: `--detach` 起 PATH 里的 `cca`（指向主 checkout，没有 `wait`）
  → 登记成功、到点什么都不发生
- Important I2（**已修**）: 单条结果不落盘 → 一轮中途被 kill 会重复推。改成每推完一条
  `save_waiting_merged()`（重新读盘再合并，顺带解决 I1 的三分之二：cancel 不被复活、并发登记不丢）
- Important I3（**已修**）: 缺字段的记录会让整轮 + 守护进程崩掉；现在只标注它自己
- Important I4（**已修**）: `claude logs` 两道闸加 120 秒超时；单条会话抛异常只记 failed
- Important I6（**已修**）: 额度字样原来在 `returncode` 之前判 —— 成功那轮只要回复里提到
  "429 / rate limit" 就被记成"额度还没放开"；现在只在失败形态下算数
- M1（**已修**）: 7 天清理改按 `finished_at`（等 7 天窗口时刚出结果就被删 = 看不见结果）
- M2（**已修**）: `cca --help` 里 `--only=<前缀|标题>` 续行本来属于 `cca carry`，被 `wait` 插在中间
- 记入待办（**没做**，今晚不影响，单守护进程单记录）:
  I1 剩余部分（`--run` 抢同一份状态文件的 O_EXCL 锁 / 登记查重）、I5（`--only` 不可重复且
  `--only a b` 报错措辞）、I7（触发规则与 spec 文本已统一，但"两个窗口都不像卡住"时没给提示）、
  M3（`cca wait 001 --run` 之类多余位置参数被静默忽略）、M4（快照来源区分）、M5（额度字样过宽）、
  M6（`cca doctor` 不加"N 条等待中"，已把 spec §7 改成只承诺 `cca ls`）、M7（两处测试强度）
- 评审自评（Declined to judge 10 条）已逐条过：均同意其"超出范围"的搁置理由，无异议

## 真机端到端（10-03 01:40 / 01:48）—— 第一次失败，修完第二次成功
- 第一次 w1790961478（01:39:59）：三条全 failed，原因 = **登记写短 id**，而 `--resume` 只认完整
  UUID（`run_wait_entry` 走 entry["sessions"] 那条路没补全）。记录以"推了 0 条"收尾。
- Ruling: 显式会话列表在推之前补全 id（面板 → 共享会话池），不再依赖使用者写完整 UUID ——
  代价：多一次面板/池查询；收益：`--dry-run` 抄下来的短 id 直接能用
- Ruling: `--window auto` 改成"只等又快满（≥95%）又还没重置的窗口"，5 小时优先、没有看 7 天；
  快到上限的那个已重置 → 立刻可跑。原规则"取最近一个未来重置"在"5 小时刚重置 + 7 天在两天后"
  时会白等两天（今晚只能用 `--at` 绕开）—— 代价：显式 `--window 5h|7d` 保留老语义，两种行为并存
- 第二次 w1790963148（01:48，完整 UUID + `--at` 立即执行）：三条全 sent，transcript 分别
  2509→2656 / 7079→7162 / 4732→4835 行，真在接着干活；逐条串行推三条花 47 分钟
- 实测补充: 一轮 `-p` 在后台任务还跑着时最多等 600 秒就收（`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`），
  会话自己起的后台任务继续跑；这条写进了 spec §10
