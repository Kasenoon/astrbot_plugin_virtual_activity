# 虚拟活动（Virtual Activity）设计文档 v0.1

> 目标 AstrBot 版本：v4.29.1（`<AstrBot 目录>/core`）
> 插件 id：`astrbot_plugin_virtual_activity`　显示名：虚拟活动

---

## 1. 一句话

给机器人一间**平面房子**，房子里的**家具就是互动按钮**：机器人按随机间隔自己挑一件家具、做一件绑定的活动，由 LLM 写一句"它现在在干嘛"发给管理员；管理员也能在 WebUI 的可视化房间里直接点家具，走完全相同的判定逻辑。

## 2. 范围

**本版做**

- 单机器人（结构预留多机器人同房间）、方块家具、坐标摆放
- 随机 tick（区间可配）+ 活动时段 + 静默时段
- 点击触发（WebUI 可视化房间 + QQ 文字指令）
- LLM 生成活动文案
- 状态 JSON 原子持久化，重启恢复
- 进行中互斥（tick / 点击都不触发）

**本版不做**

- 除 `rect` 外的家具形状
- 在 QQ 里发平面图 / 图片热区（协议层不支持）
- 群发、多目标广播
- 家具碰撞检测、寻路、多机器人同时调度
- 语音 / 表情包联动

## 3. 概念模型

```
Room 1 ── N Robot          一个机器人只属于一个房间；一个房间可以有多个机器人
Room 1 ── N Furniture      家具，带坐标、颜色、形状
Furniture 1 ── N Effect    绑定在家具上的活动（沙发：看书 / 休息）
```

- `robot.id` 当前固定 `default`；state 按机器人分文件，调度器只驱动 `default`。
- 坐标单位为画布 px，原点左上角。默认画布 `1000×700`。

## 4. 数据结构

### 4.1 `room.json`（房间布局，WebUI 可拖拽编辑）

```json
{
  "schema_version": 1,
  "room": {
    "name": "我的小屋",
    "canvas": { "width": 1000, "height": 700, "background": "#f5efe6" },
    "robots": [{ "id": "default", "name": "小鲸鱼", "avatar": "" }]
  },
  "furniture": [
    {
      "id": "sofa",
      "name": "沙发",
      "shape": "rect",
      "x": 120, "y": 360, "w": 220, "h": 110,
      "color": "#c8a27a",
      "emoji": "🛋️",
      "z": 10,
      "enabled": true,
      "effects": [
        {
          "id": "sofa_read",
          "name": "看书",
          "desc": "窝在沙发上看书",
          "prompt_hint": "随手翻一本旧书，可能停在某一页",
          "duration_sec": 600,
          "weight": 1
        },
        {
          "id": "sofa_rest",
          "name": "休息",
          "desc": "在沙发上躺一会儿",
          "prompt_hint": "眯一会儿，盖了条毯子",
          "duration_sec": 900,
          "weight": 1
        }
      ]
    }
  ]
}
```

字段约束：

| 字段 | 说明 |
|---|---|
| `id` | 房间内唯一，`[a-z0-9_]+` |
| `shape` | 枚举，本版仅 `rect`（预留 `circle` / `poly`） |
| `x,y,w,h` | 画布坐标与尺寸，px |
| `color` | 十六进制色，WebUI 和以后渲染用 |
| `emoji` | 可选，视图里画在方块中央 |
| `z` | 叠放顺序，数字大在上 |
| `enabled` | 关掉后不参与随机，也不可点击 |
| `effects` | 空数组 = 摆件（不参与随机，点击给出提示） |

### 4.2 `state.json`（每个机器人一份，原子写）

```json
{
  "schema_version": 1,
  "robot_id": "default",
  "behavior": {
    "furniture_id": "sofa",
    "effect_id": "sofa_rest",
    "name": "休息"
  },
  "location": { "furniture_id": "sofa", "x": 230, "y": 415 },
  "started_at": "2026-01-01T09:00:00+08:00",
  "ends_at":    "2026-01-01T09:15:00+08:00",
  "energy": 72.0,
  "social_hunger": 41.0,
  "next_tick_at": "2026-01-01T09:32:11+08:00",
  "recent_behaviors": [
    { "furniture_id": "sofa", "effect_id": "sofa_rest", "at": "2026-01-01T09:00:00+08:00" }
  ],
  "last_trigger_source": "tick",
  "last_error": null,
  "updated_at": "2026-01-01T09:00:00+08:00"
}
```

- `behavior` / `location` / `started_at` / `ends_at` 为 `null` 表示空闲。
- `energy` / `social_hunger`：`0–100`，本版**只记录不参与判定**（每完成一次活动按效果改值，随时间缓慢恢复）。
- `next_tick_at`：单值 → 一个机器人只有一条 tick 时间线。
- 原子写：`state.json.tmp` 写完 `os.replace`；JSON `ensure_ascii=False, indent=2`。

## 5. 触发规则

1. **tick 调度**：单条 `asyncio` 任务，`next_tick_at = now + random(tick_min, tick_max)`；持久化，重启读取。若重启时已过期，跑一次再重新排。
2. **时间窗**：默认 `active_hours = 08:00–20:00`，窗外不触发（顺延到下一个窗口内的随机点）。
3. **静默时段**：默认 `23:00–08:00`（跨天区间支持），静默期不触发。
4. **进行中锁**：`behavior != null && now < ends_at` → tick / 点击都返回"正在忙"，不排队、不打断。
5. **候选集**：`enabled && effects 非空`，等权随机（`weight` 预留）。
6. **选效果**：`multi_effect_policy`：
   - `llm`（**默认**）：把该家具的候选效果交给 LLM 选，并与文案生成**合并为一次调用**
   - `first`：取 `effects[0]`
   - `random`：按权重随机
7. **执行**：写 state → LLM 文案 → 发送 → 追加 `recent_behaviors`（保留最近 20 条）。

## 6. 统一判定管线（核心）

```python
async def run_activity(
    self,
    robot_id: str,
    furniture_id: str,
    source: Literal["tick", "click", "test"],
    effect_id: str | None = None,
) -> ActivityResult
```

```python
@dataclass
class ActivityResult:
    ok: bool
    reason: str            # ok / busy / no_effect / disabled / not_found / llm_failed
    furniture: dict | None
    effect: dict | None
    text: str              # 最终发出的文案
    state: dict            # 写入后的状态
```

tick 与点击**只调这一个入口**，差异仅在 `source` 与是否指定 `effect_id`。验收即验证两条链路产出的 `ActivityResult` 结构一致、状态字段一致。

## 7. LLM 文案生成

**一次调用同时完成"选效果 + 写文案"**，避免 tick 时连打两枪。

- 输入：机器人名（可为人设名）、家具名、该家具的候选效果列表（id / name / desc / prompt_hint）、当前 `energy`/`social_hunger`、最近 5 条活动、当前时间段。
- 要求模型返回 JSON：

```json
{ "effect_id": "sofa_rest", "text": "我在沙发上躺会儿，盖着毯子。" }
```

- 校验：`effect_id` 必须在该家具的效果列表里；`text` 去引号、截断到 40 字。不合规则重试一次。
- 参数：`temperature=1.0`，`max_tokens=160`，超时 20s。
- 降级链：解析失败 → `random` 权重选效果 + `llm_fallback_template`（`它在{家具}那儿{效果}。`）；再失败 → 只写状态不发消息，记 `last_error`。
- 失败不阻断状态写入。

## 8. 发送

- 目标 umo：默认 `aiocqhttp:FriendMessage:3145821247`（管理员私聊），配置项 `platform_id` + `admin_qq`。
- 只发私聊，不发群。
- 发送失败记 `last_error`，重试 1 次。

## 9. QQ 侧入口（文字，无图片）

| 指令 | 作用 |
|---|---|
| `/回家` | 状态卡：在哪、在干嘛、还有多久结束、下次 tick、能量/社交 |
| `/点 <家具名\|id> [效果名]` | 手动触发，走 `run_activity(source="click")` |
| `/测试tick` | 强制一次随机 tick，**绕过时间窗、静默时段与进行中锁**，输出里标注绕过了哪几项 |
| `/活动列表` | 管理员查看家具与效果清单（辅助，不作为按钮） |

权限：仅管理员（`admin_qq`）。其他人调用返回无权限。

## 10. WebUI 视图（`views/room/`）

打开插件视图即**全屏房间视图**（不做悬浮小地图）：

- 顶部状态条：正在干嘛、剩余时长、能量 / 社交、下次 tick 倒计时、「立即来一次」按钮
- 中间画布：按 `x/y/w/h/color/emoji/z` 画出家具方块；**拖动改 `x/y`**，点一下 = 触发 `run_activity`（与 QQ `/点` 同一接口）
- 右侧面板：选中家具后改名 / 颜色 / emoji / 尺寸 / `enabled`，以及效果列表的增删改（id、名字、desc、prompt_hint、时长、权重）
- 保存写回 `room.json`；状态轮询 5s
- 前端通过 `window.AstrBotPluginView` bridge 调后端 `register_web_api`

> QQ 客户端没有注入悬浮层的能力，所以"悬浮"这一步只在 WebUI 里以全屏视图呈现；QQ 侧保持文字指令。

## 11. 文件布局

```
astrbot_plugin_virtual_activity/
├─ main.py                  # Star 注册、指令、Web API
├─ metadata.yaml
├─ _conf_schema.json        # 插件配置
├─ requirements.txt
├─ core/
│  ├─ models.py             # dataclass + 校验
│  ├─ store.py              # 原子读写、路径
│  ├─ engine.py             # run_activity / tick 调度
│  ├─ llm.py                # 文案生成
│  └─ scheduler.py          # asyncio tick 循环
├─ views/room/
│  ├─ index.html
│  ├─ app.js
│  └─ style.css
└─ .astrbot-plugin/i18n/zh-CN.json
```

运行数据：`data/plugin_data/astrbot_plugin_virtual_activity/{room.json,state_default.json}`

## 12. 配置项草案（`_conf_schema.json`）

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `tick_min_minutes` | int | 20 | 随机间隔下限 |
| `tick_max_minutes` | int | 45 | 随机间隔上限 |
| `active_hours` | string | `08:00-20:00` | 允许活动的时间窗 |
| `quiet_hours` | string | `23:00-08:00` | 静默时段 |
| `multi_effect_policy` | string | `llm` | `llm/first/random` |
| `admin_qq` | string | `3145821247` | 管理员 / 发送目标 |
| `platform_id` | string | `aiocqhttp` | 平台 id |
| `llm_temperature` | float | 1.0 | 文案温度 |
| `llm_timeout_sec` | int | 20 | 生成超时 |
| `default_duration_sec` | int | 600 | 效果未填时长时的默认值 |
| `recent_limit` | int | 20 | 历史保留条数 |
| `debug` | bool | false | 打印细节日志 |

## 13. 验收清单

1. `/测试tick` → 状态写入、文案发出、`next_tick_at` 更新
2. `/点 沙发` → 与 tick 同一个 `run_activity`，返回结构一致
3. 点摆件 → 明确提示"这只是个摆件"
4. 活动进行中再 tick / 再点 → 拒绝，状态不变
5. 重启后 `next_tick_at` 保留，过期则补一次
6. 窗口外 / 静默时段不自动触发
7. WebUI 拖拽改坐标 → `room.json` 更新 → 刷新后位置正确
8. LLM 超时 → 降级文案，不崩、状态仍然写入

## 14. 已确认决策

| 议题 | 结论 |
|---|---|
| 交互面 | C：QQ 文字指令 + WebUI 可视房间 |
| 悬浮窗 | 不做悬浮，插件页直接是房间全屏视图 |
| 多效果选择 | 交给 LLM，与文案合并成一次调用；失败降级权重随机 + 模板 |
| `/测试tick` | 绕过时间窗 / 静默 / 进行中锁 |
| 发送对象 | 管理员私聊 `aiocqhttp:FriendMessage:3145821247` |
| 状态粒度 | 单机器人全局一份；按 `robot_id` 分文件 |
| 持久化 | `data/plugin_data/astrbot_plugin_virtual_activity/` |
| QQ 平面图 | 不发，纯文字 |
